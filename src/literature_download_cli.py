#!/usr/bin/env python3
"""CLI-first journal full-text downloader.

Legal source cascade:
  1. Existing validated local file
  2. OpenAlex-declared open-access PDF
  3. Elsevier Article Retrieval API (PDF, AAM-PDF, XML reconstruction)
  4. Institutional access (campus network, WebVPN, EZProxy, EasyConnect, aTrust)

The JSON output is stable so a GUI, script or AI agent can invoke this file
as a subprocess without importing implementation details.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from providers import elsevier_fulltext, mdpi_resolver
from providers.app_paths import (
    app_dir,
    cleanup_stale_runtime,
    migrate_legacy_state,
    temporary_folder,
)
from providers.interactive_browser_download import (
    download_batch_after_manual_verification,
)
from providers.institutional_access import (
    DEFAULT_SCHOOLS_PATH,
    InstitutionalConfigError,
    apply_school_selection,
    cookie_file_for,
    download_via_institution,
    find_school,
    institution_session_status,
    load_school_catalog,
    login_institution,
    make_institution_client,
    selected_school,
    schools_path_from_config,
)
from providers.webvpn_hhu import (
    DEFAULT_BASE_URL,
    DEFAULT_KEY,
    DownloadError,
    doi_filename,
    normalize_doi,
    is_sitewide_pdf,
    valid_pdf,
)
from providers.webvpn_login_cdp import DEFAULT_COOKIE_FILE


APP_VERSION = "1.8.0"
APP_DIR = app_dir()
DEFAULT_CONFIG_PATH = APP_DIR / "config.local.json"
LOG = logging.getLogger("literature_download")
LOGIN_MARKERS = (
    "/authserver/login",
    "/cas/login",
    "/sso/login",
)
SOURCE_ORDER = ("openalex", "elsevier", "webvpn")
DEFAULT_PAPER_PROCESS_TIMEOUT_SECONDS = 120.0


class CredentialRedactingFilter(logging.Filter):
    """Keep query-string API credentials out of verbose HTTP diagnostics."""

    _pattern = re.compile(r"([?&](?:api_key|mailto|email)=)[^&\s]+", re.I)

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = self._pattern.sub(r"\1<redacted>", rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def configure_text_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except (OSError, ValueError):
                pass


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    output = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(output.get(key), dict):
            output[key] = deep_merge(output[key], value)
        else:
            output[key] = value
    return output


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "contact_email": "",
        "api_keys": {
            "elsevier": "",
            "elsevier_inst_token": "",
            "openalex": "",
        },
        "network": {
            "timeout_seconds": 25,
            "download_timeout_seconds": 90,
            "max_retries": 2,
            "retry_delay_seconds": 2,
        },
        "mdpi": {
            "cache_file": str(mdpi_resolver.DEFAULT_CACHE_FILE),
            "cdn_hosts": ["mdpi-res.com", "res.mdpi.com"],
            "version_suffixes": ["", "-v2", "-v3", "-v1", "-v4", "-v5"],
            "max_slug_candidates": 6,
            "slug_overrides": {},
        },
        "elsevier": {
            "use_aam": False,
            "use_xml_reconstruction": False,
            "figure_download_workers": 4,
        },
        "download": {
            "source_order": list(SOURCE_ORDER),
            "max_batch_papers": 60,
            "max_file_mb": 120,
            "per_paper_timeout_seconds": 120,
        },
        "webvpn": {
            "enabled": False,
            "base_url": DEFAULT_BASE_URL,
            "key": DEFAULT_KEY,
            "iv": DEFAULT_KEY,
            "cookie_file": str(DEFAULT_COOKIE_FILE),
            "request_delay_seconds": 2,
        },
        "institution": {
            "school_id": "campus-network-direct",
            "school_name": "校园网直连",
            "access_type": "direct",
            "schools_file": str(DEFAULT_SCHOOLS_PATH),
            "request_delay_seconds": 2,
            "connector_url": "",
        },
    }


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("配置文件顶层必须是 JSON 对象")
    return deep_merge(default_config(), value)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    try:
        part.write_bytes(content)
        os.replace(part, path)
    except Exception:
        part.unlink(missing_ok=True)
        raise


_SECRET_FIELD = re.compile(
    r"^(?:api[_-]?keys?|.*[_-]api[_-]?key|token|.*[_-]token|cookies?|"
    r"cookie[_-]header|password|secret|authorization|contact[_-]?email|"
    r"email|mailto)$",
    re.I,
)
_SECRET_QUERY = re.compile(
    r"([?&](?:api[_-]?key|token|access_token|mailto|email)=)[^&\s]+", re.I
)


def redact_diagnostics(value: Any, *, field: str = "") -> Any:
    """Recursively redact credentials while retaining complete diagnostics."""
    if _SECRET_FIELD.search(field):
        if value in (None, "", [], {}):
            return value
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(key): redact_diagnostics(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_diagnostics(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _SECRET_QUERY.sub(r"\1<redacted>", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return repr(value)


def failure_report_path_for(doi: str, output_dir: Path) -> Path:
    return output_dir / Path(doi_filename(doi)).with_suffix(".txt")


def write_failure_report(
    doi: str,
    output_dir: Path,
    result: dict[str, Any],
    *,
    output_format: str,
    sources: list[str],
    preflight: dict[str, Any],
) -> Path:
    """Persist a readable, structured, per-DOI failure report."""
    report_path = failure_report_path_for(doi, output_dir).resolve()
    payload = redact_diagnostics(
        {
            "report_type": "literature_download_failure",
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "application_version": APP_VERSION,
            "doi": doi,
            "requested_format": output_format,
            "enabled_sources": sources,
            "output_directory": str(output_dir),
            "report_path": str(report_path),
            "preflight": preflight,
            "result": result,
        }
    )
    content = (
        "SCI 全文下载失败诊断\n"
        "说明：保留各来源的完整结构化返回、HTTP 状态和异常信息；"
        "密钥、Cookie、令牌等凭据已脱敏。\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n"
    )
    atomic_write(report_path, content.encode("utf-8"))
    return report_path


def pdf_info(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {
        "valid": valid_pdf(path),
        "bytes": path.stat().st_size if path.exists() else 0,
        "pages": None,
        "parse_error": None,
    }
    if not info["valid"]:
        return info
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        info["pages"] = len(PdfReader(str(path)).pages)
    except Exception as exc:
        info["parse_error"] = f"{type(exc).__name__}: {exc}"
    return info


DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def _doi_match_text(value: str) -> str:
    """Normalize wrapping/spacing without erasing DOI-significant punctuation."""
    value = urllib.parse.unquote(str(value or "")).casefold()
    value = re.sub(r"https?://(?:dx\.)?doi\.org/", "", value)
    value = re.sub(r"\bdoi\s*:\s*", "", value)
    return re.sub(r"\s+", "", value)


def pdf_identity_info(path: Path, doi: str) -> dict[str, Any]:
    """Confirm a PDF belongs to the requested DOI when its text permits it.

    A positive DOI match is authoritative.  A strong mismatch (another DOI or
    a known site-wide help document) is rejected.  Image-only/legacy PDFs stay
    usable with an ``unconfirmed`` result instead of becoming false failures.
    """
    result: dict[str, Any] = {
        "status": "unconfirmed",
        "match": None,
        "requested_doi": doi,
        "detected_dois": [],
        "reason": "PDF text does not expose a decisive DOI",
    }
    if not valid_pdf(path):
        return {
            **result,
            "status": "invalid",
            "match": False,
            "reason": "file is not a structurally valid PDF",
        }
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(str(path))
        pieces: list[str] = []
        metadata = reader.metadata or {}
        pieces.extend(str(value) for value in metadata.values() if value)
        for page in reader.pages[: min(3, len(reader.pages))]:
            try:
                pieces.append(page.extract_text() or "")
            except Exception:
                continue
        visible_text = "\n".join(pieces)[:200_000]
    except Exception as exc:
        result["reason"] = f"PDF identity extraction unavailable: {type(exc).__name__}"
        return result

    normalized = _doi_match_text(visible_text)
    requested = _doi_match_text(doi)
    detected = list(
        dict.fromkeys(
            match.group(0).rstrip(".,;:)\]")
            for match in DOI_IN_TEXT_RE.finditer(visible_text)
        )
    )
    result["detected_dois"] = detected[:12]
    if requested and requested in normalized:
        return {
            **result,
            "status": "matched",
            "match": True,
            "reason": "requested DOI found in PDF metadata or first pages",
        }

    lower_text = re.sub(r"\s+", " ", visible_text.casefold())
    generic_document_markers = (
        "content platform user guide",
        "scitation user guide",
        "platform user guide",
        "doi trademark policy",
        "use of trademarks owned by the international doi",
        "130701trademark policy",
    )
    if any(marker in lower_text for marker in generic_document_markers):
        return {
            **result,
            "status": "mismatched",
            "match": False,
            "reason": "site-wide policy/help document detected instead of an article",
        }
    if detected and all(_doi_match_text(item) != requested for item in detected):
        return {
            **result,
            "status": "mismatched",
            "match": False,
            "reason": "PDF exposes DOI(s), but none matches the requested DOI",
        }
    return result


def xml_fulltext_info(path: Path) -> dict[str, Any]:
    try:
        content = path.read_bytes()
    except OSError as exc:
        return {"valid": False, "bytes": 0, "reason": str(exc)}
    info = elsevier_fulltext.xml_info(content)
    return {
        "valid": bool(info.get("fulltext_detected")),
        "bytes": len(content),
        "body_text_chars": info.get("body_text_chars"),
        "parse_error": info.get("parse_error"),
    }


def normalize_sources(value: str, config: dict[str, Any]) -> list[str]:
    if value.strip().casefold() == "auto":
        configured = (config.get("download") or {}).get("source_order") or SOURCE_ORDER
        values = [str(x).strip().casefold() for x in configured]
    else:
        values = [x.strip().casefold() for x in value.split(",")]
    allowed = {"openalex", "elsevier", "webvpn"}
    unknown = [x for x in values if x and x not in allowed]
    if unknown:
        raise ValueError(
            "未知或不支持的来源：" + ", ".join(unknown)
            + "。允许：openalex, elsevier, webvpn"
        )
    return list(dict.fromkeys(x for x in values if x))


def safe_url_host(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_pmc_repository_url(url: str) -> bool:
    """Return whether a URL belongs to PMC or Europe PMC."""
    return safe_url_host(url) in {
        "ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        "europepmc.org",
        "www.europepmc.org",
    }


def retryable_pmc_status(status_code: int) -> bool:
    return status_code in {408, 425, 429, 500, 502, 503, 504}


def response_requires_human_verification(response: requests.Response) -> bool:
    """Recognize an explicit browser/CAPTCHA challenge, not an ordinary denial."""
    response_url = response.url.casefold()
    explicit_url = any(
        marker in response_url
        for marker in ("/captcha", "/cdn-cgi/challenge", "challenge-platform")
    )
    if response.status_code not in {403, 418, 429, 503} and not explicit_url:
        return False
    content_type = response.headers.get("Content-Type", "").casefold()
    if "html" not in content_type and "text" not in content_type:
        return False
    preview = response.content[:128 * 1024].decode("utf-8", "ignore").casefold()
    markers = (
        "滑动验证",
        "人机验证",
        "captcha",
        "cf-chl-",
        "checking your browser",
        "just a moment",
        "challenge-platform",
    )
    return explicit_url or any(marker in preview for marker in markers)


def oa_candidate_urls(work: dict[str, Any]) -> list[dict[str, str]]:
    oa = work.get("open_access") or {}
    if not oa.get("is_oa"):
        return []
    locations = [
        work.get("best_oa_location"),
        work.get("primary_location"),
        *(work.get("locations") or []),
    ]
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        url = str(location.get("pdf_url") or "").strip()
        if not url.startswith(("https://", "http://")) or url in seen:
            continue
        if location.get("is_oa") is False:
            continue
        host = safe_url_host(url)
        if not host or host in {"localhost", "127.0.0.1", "::1"}:
            continue
        seen.add(url)
        output.append(
            {
                "url": url,
                "host": host,
                "license": str(location.get("license") or oa.get("oa_status") or ""),
                "version": str(location.get("version") or ""),
                "source": "openalex_oa",
            }
        )
    return output


def oa_landing_urls(work: dict[str, Any]) -> list[dict[str, str]]:
    """Return publisher/repository landing pages explicitly marked open access."""
    oa = work.get("open_access") or {}
    if not oa.get("is_oa"):
        return []
    locations = [
        work.get("best_oa_location"),
        work.get("primary_location"),
        *(work.get("locations") or []),
    ]
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for location in locations:
        if not isinstance(location, dict) or location.get("is_oa") is False:
            continue
        url = str(location.get("landing_page_url") or "").strip()
        host = safe_url_host(url)
        if (
            not url.startswith(("https://", "http://"))
            or not host
            or host in {"localhost", "127.0.0.1", "::1"}
            or url in seen
        ):
            continue
        seen.add(url)
        output.append(
            {
                "url": url,
                "host": host,
                "license": str(location.get("license") or oa.get("oa_status") or ""),
                "version": str(location.get("version") or ""),
                "source": "openalex_landing",
            }
        )
    return output


def openalex_repository_landing_urls(
    work: dict[str, Any],
) -> list[dict[str, str]]:
    """Keep public repository records even when OA aggregators lack file metadata.

    Older institutional repositories often expose a submitted manuscript from
    their landing page while OpenAlex has neither a PDF URL nor a normalized OA
    flag. We only probe locations whose source is explicitly a repository; a
    publisher's closed DOI landing page is never promoted by this fallback.
    """
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for location in work.get("locations") or []:
        if not isinstance(location, dict):
            continue
        source = location.get("source") or {}
        if not isinstance(source, dict) or source.get("type") != "repository":
            continue
        url = str(location.get("landing_page_url") or "").strip()
        host = safe_url_host(url)
        if (
            not url.startswith(("https://", "http://"))
            or not host
            or host in {"localhost", "127.0.0.1", "::1"}
            or url in seen
        ):
            continue
        seen.add(url)
        output.append(
            {
                "url": url,
                "host": host,
                "license": str(location.get("license") or ""),
                "version": str(location.get("version") or "submittedVersion"),
                "source": "openalex_repository",
            }
        )
    return output


def unpaywall_candidate_urls(
    record: dict[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Extract OA PDF and landing-page candidates from Unpaywall metadata."""
    if not record.get("is_oa"):
        return [], []
    locations = [
        record.get("best_oa_location"),
        record.get("first_oa_location"),
        *(record.get("oa_locations") or []),
    ]
    pdfs: list[dict[str, str]] = []
    landings: list[dict[str, str]] = []
    seen_pdf: set[str] = set()
    seen_landing: set[str] = set()
    for location in locations:
        if not isinstance(location, dict):
            continue
        common = {
            "license": str(location.get("license") or record.get("oa_status") or ""),
            "version": str(location.get("version") or ""),
        }
        pdf_url = str(location.get("url_for_pdf") or "").strip()
        pdf_host = safe_url_host(pdf_url)
        if (
            pdf_url.startswith(("https://", "http://"))
            and pdf_host
            and pdf_host not in {"localhost", "127.0.0.1", "::1"}
            and pdf_url not in seen_pdf
        ):
            seen_pdf.add(pdf_url)
            pdfs.append(
                {"url": pdf_url, "host": pdf_host, "source": "unpaywall_oa", **common}
            )
        landing_url = str(location.get("url_for_landing_page") or "").strip()
        landing_host = safe_url_host(landing_url)
        if (
            landing_url.startswith(("https://", "http://"))
            and landing_host
            and landing_host not in {"localhost", "127.0.0.1", "::1"}
            and landing_url not in seen_landing
        ):
            seen_landing.add(landing_url)
            landings.append(
                {
                    "url": landing_url,
                    "host": landing_host,
                    "source": "unpaywall_landing",
                    **common,
                }
            )
    return pdfs, landings


def crossref_open_candidates(
    record: dict[str, Any], *, already_declared_oa: bool
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    """Extract publisher links only when Crossref/OpenAlex declares OA rights.

    Crossref also exposes text-mining links for closed articles, so a PDF-shaped
    URL alone is not sufficient.  We accept the links only when an existing OA
    source says the work is open or Crossref supplies a Creative Commons licence.
    """
    licenses = [
        str(item.get("URL") or "").strip()
        for item in (record.get("license") or [])
        if isinstance(item, dict)
    ]
    has_open_license = any(
        "creativecommons.org/" in url.casefold() for url in licenses
    )
    if not (already_declared_oa or has_open_license):
        return [], [], licenses

    pdfs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in record.get("link") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("URL") or "").strip()
        host = safe_url_host(url)
        content_type = str(item.get("content-type") or "").casefold()
        if (
            not url.startswith(("https://", "http://"))
            or not host
            or host in {"localhost", "127.0.0.1", "::1"}
            or url in seen
            or ("pdf" not in content_type and "pdf" not in url.casefold())
        ):
            continue
        seen.add(url)
        pdfs.append(
            {
                "url": url,
                "host": host,
                "license": next((value for value in licenses if value), "open-access"),
                "version": str(item.get("content-version") or ""),
                "source": "crossref_oa",
            }
        )

    landing_url = str(record.get("URL") or "").strip()
    landing_host = safe_url_host(landing_url)
    landings = []
    if (
        landing_url.startswith(("https://", "http://"))
        and landing_host
        and landing_host not in {"localhost", "127.0.0.1", "::1"}
    ):
        landings.append(
            {
                "url": landing_url,
                "host": landing_host,
                "license": next((value for value in licenses if value), "open-access"),
                "version": "publishedVersion",
                "source": "crossref_landing",
            }
        )
    return pdfs, landings, licenses


class _PdfLinkParser(HTMLParser):
    """Collect standards-based and ordinary PDF links from an article page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._anchor_href = ""
        self._anchor_text: list[str] = []
        self._in_script = False
        self.script_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "") for key, value in attrs}
        tag = tag.casefold()
        candidate = ""
        if tag == "meta":
            marker = (values.get("name") or values.get("property") or "").casefold()
            if marker in {"citation_pdf_url", "eprints.document_url", "wkhealth_pdf_url"}:
                candidate = values.get("content", "")
        elif tag == "link" and "pdf" in values.get("type", "").casefold():
            candidate = values.get("href", "")
        elif tag in {"embed", "iframe"}:
            embedded = values.get("src", "")
            hint = f"{embedded} {values.get('type', '')}".casefold()
            if "pdf" in hint:
                candidate = embedded
        elif tag == "object":
            embedded = values.get("data", "")
            hint = f"{embedded} {values.get('type', '')}".casefold()
            if "pdf" in hint:
                candidate = embedded
        elif tag == "a":
            href = values.get("href", "")
            self._anchor_href = href
            self._anchor_text = []
            hint = " ".join(
                [
                    href,
                    values.get("title", ""),
                    values.get("aria-label", ""),
                    values.get("download", ""),
                ]
            ).casefold()
            if "pdf" in hint and not any(
                word in hint for word in ("supplement", "supporting", "citation", "bibtex")
            ):
                candidate = href
        if candidate:
            self.links.append(candidate.strip())

        if tag == "script":
            self._in_script = True

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)
        if self._in_script:
            self.script_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href:
            label = " ".join(self._anchor_text).casefold()
            if "pdf" in label and not any(
                word in label for word in ("supplement", "supporting", "citation", "bibtex")
            ):
                self.links.append(self._anchor_href)
            self._anchor_href = ""
            self._anchor_text = []
        elif tag == "script":
            self._in_script = False


def discover_pdf_urls(html: str, base_url: str) -> list[str]:
    parser = _PdfLinkParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    script_pattern = re.compile(
        r"(?:defaultUrl|pdfUrl|pdf_url)\s*(?:['\"]\s*)?[:=,]\s*['\"]([^'\"]+)",
        re.I,
    )
    for script in parser.script_text:
        parser.links.extend(match.group(1) for match in script_pattern.finditer(script))

    output: list[str] = []
    seen: set[str] = set()
    queue = list(parser.links)
    while queue:
        raw = queue.pop(0)
        url = urllib.parse.urljoin(base_url, raw)
        host = safe_url_host(url)
        if (
            not url.startswith(("https://", "http://"))
            or not host
            or host in {"localhost", "127.0.0.1", "::1"}
            or url in seen
        ):
            continue
        seen.add(url)
        output.append(url)
        parsed = urllib.parse.urlsplit(url)
        for key, values in urllib.parse.parse_qs(parsed.query).items():
            if key.casefold() not in {"file", "pdf", "src", "url"}:
                continue
            for nested in values:
                if "pdf" in nested.casefold():
                    queue.append(urllib.parse.urljoin(url, nested))
        if len(output) >= 12:
            break
    return output


def derive_publisher_pdf_urls(doi: str, source_url: str) -> list[str]:
    """Construct conservative, publisher-owned PDF routes from a landing URL."""
    parsed = urllib.parse.urlsplit(source_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/")
    output: list[str] = []

    def add(candidate_path_or_url: str) -> None:
        url = urllib.parse.urljoin(source_url, candidate_path_or_url)
        if safe_url_host(url) and url not in output:
            output.append(url)

    # PMC's current PDF link intentionally returns a small JavaScript/HTML
    # preparation page to non-browser clients. Europe PMC exposes the same
    # repository copy through a stable public PDF response.
    pmc_match = re.search(r"/(?:pmc/)?articles/(PMC)?(\d+)(?:/|$)", path, re.I)
    if pmc_match and host in {"pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"}:
        pmcid = "PMC" + pmc_match.group(2)
        add(f"https://europepmc.org/api/getPdf?pmcid={pmcid}")
        add(f"https://europepmc.org/articles/{pmcid}?pdf=render")

    # Open Journal Systems is widely used by small independent publishers.
    ojs_match = re.search(r"/article/view/(\d+)(?:/|$)", path, re.I)
    if ojs_match:
        prefix = path[: ojs_match.start()]
        add(f"{prefix}/article/download/{ojs_match.group(1)}/pdf")

    if host.endswith("journals.plos.org") and doi.casefold().startswith("10.1371/"):
        parts = [part for part in path.split("/") if part]
        journal = parts[0] if parts and parts[0] != "article" else "plosone"
        add(f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable")

    copernicus = re.fullmatch(
        r"10\.5194/([a-z0-9]+)-(\d+)-(.+)-(\d{4})", doi, re.I
    )
    if copernicus:
        journal, volume, page, year = copernicus.groups()
        target_host = host if host.endswith("copernicus.org") else f"{journal}.copernicus.org"
        suffix = doi.split("/", 1)[1]
        add(f"https://{target_host}/articles/{volume}/{page}/{year}/{suffix}.pdf")

    if host.endswith("journals.aps.org") and "/abstract/" in path:
        add(path.replace("/abstract/", "/pdf/", 1))
    return output


def download_openalex(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    overwrite: bool,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    network = config.get("network") or {}
    timeout = max(1, int(network.get("download_timeout_seconds") or 90))
    retries = max(0, int(network.get("max_retries") or 0))
    delay = max(0.0, float(network.get("retry_delay_seconds") or 0))
    keys = config.get("api_keys") or {}
    api_key = str(keys.get("openalex") or "").strip()
    contact = str(config.get("contact_email") or "").strip()
    params: dict[str, str] = {}
    if api_key:
        params["api_key"] = api_key
    if contact:
        params["mailto"] = contact

    endpoint = "https://api.openalex.org/works/doi:" + doi
    unpaywall_endpoint = (
        "https://api.unpaywall.org/v2/" + urllib.parse.quote(doi, safe="/()")
        if contact
        else ""
    )
    crossref_endpoint = "https://api.crossref.org/works/" + urllib.parse.quote(
        doi, safe=""
    )
    crossref_agent = "literature-downloader/1.8"
    if contact:
        crossref_agent += f" (mailto:{contact})"
    metadata_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="metadata")
    metadata_futures = {
        "openalex": metadata_executor.submit(
            requests.get,
            endpoint,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "literature-downloader/1.8"},
            timeout=min(timeout, 30),
        ),
        "crossref": metadata_executor.submit(
            requests.get,
            crossref_endpoint,
            headers={"Accept": "application/json", "User-Agent": crossref_agent},
            timeout=min(timeout, 30),
        ),
    }
    if contact:
        metadata_futures["unpaywall"] = metadata_executor.submit(
            requests.get,
            unpaywall_endpoint,
            params={"email": contact},
            headers={"Accept": "application/json", "User-Agent": "literature-downloader/1.8"},
            timeout=min(timeout, 12),
        )
    if progress:
        progress("openalex_metadata")
    attempts: list[dict[str, Any]] = []
    work: dict[str, Any] = {}
    metadata_error = ""
    try:
        metadata = metadata_futures["openalex"].result()
    except requests.RequestException as exc:
        metadata_error = f"OpenAlex 请求失败：{type(exc).__name__}: {exc}"
        attempts.append(
            {
                "stage": "metadata",
                "provider": "openalex",
                "url": endpoint,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            }
        )
    else:
        attempts.append(
            {
                "stage": "metadata",
                "provider": "openalex",
                "url": endpoint,
                "status": metadata.status_code,
                "content_type": metadata.headers.get("Content-Type", "").split(";", 1)[0],
                "bytes": len(metadata.content),
            }
        )
        if metadata.status_code != 200:
            metadata_error = f"OpenAlex 元数据 HTTP {metadata.status_code}"
        else:
            try:
                parsed = metadata.json()
                if isinstance(parsed, dict):
                    work = parsed
                else:
                    metadata_error = "OpenAlex 返回的不是 JSON 对象"
            except ValueError:
                metadata_error = "OpenAlex 返回的不是有效 JSON"

    candidates = oa_candidate_urls(work)
    landing_candidates = oa_landing_urls(work)
    landing_candidates.extend(openalex_repository_landing_urls(work))

    unpaywall_error = ""
    unpaywall: dict[str, Any] = {}
    if contact:
        try:
            response = metadata_futures["unpaywall"].result()
        except requests.RequestException as exc:
            unpaywall_error = f"Unpaywall 请求失败：{type(exc).__name__}: {exc}"
            attempts.append(
                {
                    "stage": "metadata",
                    "provider": "unpaywall",
                    "url": unpaywall_endpoint,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                }
            )
        else:
            attempts.append(
                {
                    "stage": "metadata",
                    "provider": "unpaywall",
                    "url": unpaywall_endpoint,
                    "status": response.status_code,
                    "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
                    "bytes": len(response.content),
                }
            )
            if response.status_code == 200:
                try:
                    parsed = response.json()
                    if isinstance(parsed, dict):
                        unpaywall = parsed
                    else:
                        unpaywall_error = "Unpaywall 返回的不是 JSON 对象"
                except ValueError:
                    unpaywall_error = "Unpaywall 返回的不是有效 JSON"
            else:
                unpaywall_error = f"Unpaywall 元数据 HTTP {response.status_code}"
        unpaywall_pdfs, unpaywall_landings = unpaywall_candidate_urls(unpaywall)
        candidates.extend(unpaywall_pdfs)
        landing_candidates.extend(unpaywall_landings)

    declared_oa = bool(
        (work.get("open_access") or {}).get("is_oa") or unpaywall.get("is_oa")
    )

    crossref_error = ""
    crossref: dict[str, Any] = {}
    try:
        response = metadata_futures["crossref"].result()
    except requests.RequestException as exc:
        crossref_error = f"Crossref 请求失败：{type(exc).__name__}: {exc}"
        attempts.append(
            {
                "stage": "metadata",
                "provider": "crossref",
                "url": crossref_endpoint,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            }
        )
    else:
        attempts.append(
            {
                "stage": "metadata",
                "provider": "crossref",
                "url": crossref_endpoint,
                "status": response.status_code,
                "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
                "bytes": len(response.content),
            }
        )
        if response.status_code == 200:
            try:
                message = response.json().get("message")
                if isinstance(message, dict):
                    crossref = message
                else:
                    crossref_error = "Crossref 返回中没有有效的 message 对象"
            except (AttributeError, ValueError):
                crossref_error = "Crossref 返回的不是有效 JSON"
        else:
            crossref_error = f"Crossref 元数据 HTTP {response.status_code}"
    metadata_executor.shutdown(wait=True)

    crossref_pdfs, crossref_landings, crossref_licenses = crossref_open_candidates(
        crossref, already_declared_oa=declared_oa
    )
    candidates.extend(crossref_pdfs)
    landing_candidates.extend(crossref_landings)
    known_mdpi_urls = [
        str(item.get("url") or "") for item in [*candidates, *landing_candidates]
    ]
    mdpi_extra_slugs: list[tuple[str, str]] = []
    openalex_source_error = ""
    if mdpi_resolver.is_mdpi_doi(doi):
        mdpi_parts = mdpi_resolver.bibliographic_parts(crossref, known_mdpi_urls)
        mdpi_issn = str(mdpi_parts.get("issn") or "")
        if mdpi_issn:
            mdpi_extra_slugs, openalex_source_error, source_attempt = (
                mdpi_resolver.fetch_openalex_source_slugs(
                    mdpi_issn,
                    timeout=min(timeout, 20),
                    contact_email=contact,
                    api_key=api_key,
                )
            )
            attempts.append(source_attempt)
    mdpi_candidates, mdpi_resolution = mdpi_resolver.official_candidate_urls(
        doi,
        crossref,
        config,
        known_urls=known_mdpi_urls,
        extra_slugs=mdpi_extra_slugs,
    )
    if mdpi_resolution:
        mdpi_resolution["openalex_source_error"] = openalex_source_error
    # MDPI's public article host frequently returns 403 to non-browser clients;
    # probe validated official CDN candidates before repeating those URLs.
    if mdpi_candidates:
        candidates = mdpi_candidates + candidates
    repository_candidates: list[dict[str, str]] = []
    for item in [*candidates, *landing_candidates]:
        for derived_url in derive_publisher_pdf_urls(doi, str(item.get("url") or "")):
            if safe_url_host(derived_url) != "europepmc.org":
                continue
            repository_candidates.append(
                {
                    "url": derived_url,
                    "host": "europepmc.org",
                    "license": str(item.get("license") or "open-access"),
                    "version": str(item.get("version") or ""),
                    "source": "europepmc_oa",
                }
            )
    if mdpi_candidates:
        mdpi_urls = {str(item.get("url") or "") for item in mdpi_candidates}
        candidates = mdpi_candidates + repository_candidates + [
            item
            for item in candidates
            if str(item.get("url") or "") not in mdpi_urls
        ]
    else:
        candidates = repository_candidates + candidates
    if declared_oa:
        landing_candidates.append(
            {
                "url": "https://doi.org/" + urllib.parse.quote(doi, safe="/()"),
                "host": "doi.org",
                "license": str(
                    (work.get("open_access") or {}).get("oa_status")
                    or unpaywall.get("oa_status")
                    or "open-access"
                ),
                "version": "publishedVersion",
                "source": "doi_landing",
            }
        )
    candidates = list(
        {
            str(candidate.get("url") or ""): candidate
            for candidate in candidates
            if candidate.get("url")
        }.values()
    )
    landing_candidates = list(
        {
            str(candidate.get("url") or ""): candidate
            for candidate in landing_candidates
            if candidate.get("url")
        }.values()
    )
    if not candidates and not landing_candidates:
        return {
            "ok": False,
            "source": "openalex",
            "reason": metadata_error
            or unpaywall_error
            or crossref_error
            or "OpenAlex/Unpaywall/Crossref 未声明可用的开放 PDF",
            "oa_status": (work.get("open_access") or {}).get("oa_status"),
            "unpaywall_oa_status": unpaywall.get("oa_status"),
            "crossref_licenses": crossref_licenses,
            "mdpi_resolution": mdpi_resolution,
            "attempts": attempts,
        }

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            ),
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        }
    )

    def candidate_headers(candidate: dict[str, Any]) -> dict[str, str] | None:
        referer = str(candidate.get("referer") or "").strip()
        host = safe_url_host(str(candidate.get("url") or ""))
        # SciEngine's public PDF redirect returns HTTP 418 unless it is reached
        # as navigation from the official site. The PDF itself remains public.
        if host.endswith("sciengine.com") or host.endswith("scichina.com"):
            referer = "https://www.sciengine.com/"
        return {"Referer": referer} if referer else None

    def try_pdf_candidates(
        candidate_batch: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for candidate in candidate_batch:
            if is_sitewide_pdf(candidate["url"]):
                attempts.append({"stage": "pdf_candidate", "url": candidate["url"],
                                 "skipped": True, "reason": "site-wide policy/help PDF is not an article"})
                continue
            response: requests.Response | None = None
            for retry in range(retries + 1):
                try:
                    response = session.get(
                        candidate["url"],
                        timeout=timeout,
                        allow_redirects=True,
                        headers=candidate_headers(candidate),
                    )
                    if (
                        is_pmc_repository_url(candidate["url"])
                        and retryable_pmc_status(response.status_code)
                        and retry < retries
                    ):
                        response.close()
                        response = None
                        if delay:
                            time.sleep(delay)
                        continue
                    break
                except requests.RequestException as exc:
                    if retry >= retries:
                        attempts.append(
                            {
                                "stage": "pdf_candidate",
                                "source": candidate.get("source"),
                                "url": candidate["url"],
                                "host": candidate.get("host") or safe_url_host(candidate["url"]),
                                "exception_type": type(exc).__name__,
                                "exception_message": str(exc),
                            }
                        )
                    elif delay:
                        time.sleep(delay)
            if response is None:
                continue
            content = response.content
            attempts.append(
                {
                    "stage": "pdf_candidate",
                    "source": candidate.get("source"),
                    "url": candidate["url"],
                    "final_url": response.url,
                    "host": candidate.get("host") or safe_url_host(candidate["url"]),
                    "status": response.status_code,
                    "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
                    "bytes": len(content),
                    "human_verification": response_requires_human_verification(response),
                }
            )
            if response.status_code != 200 or not content.startswith(b"%PDF-"):
                continue
            if output_path.exists() and not overwrite:
                return {
                    "ok": False,
                    "source": "openalex",
                    "reason": f"目标文件已存在：{output_path}",
                    "attempts": attempts,
                }
            atomic_write(output_path, content)
            info = pdf_info(output_path)
            if info["valid"] and (info["pages"] is None or info["pages"] >= 1):
                if candidate.get("source") == "mdpi_oa":
                    mdpi_resolver.remember_success(config, candidate)
                return {
                    "ok": True,
                    "source": candidate.get("source") or "openalex_oa",
                    "path": str(output_path.resolve()),
                    "bytes": info["bytes"],
                    "pages": info["pages"],
                    "license": candidate.get("license", ""),
                    "version": candidate.get("version", ""),
                    "resolved_host": safe_url_host(response.url),
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "mdpi_resolution": mdpi_resolution,
                    "attempts": attempts,
                }
            output_path.unlink(missing_ok=True)
        return None

    attempted_urls = {str(candidate.get("url") or "") for candidate in candidates}
    if progress:
        progress("openalex_pdf_candidates")
    initial_result = try_pdf_candidates(candidates)
    if initial_result is not None:
        return initial_result

    discovered_candidates: list[dict[str, Any]] = []
    if progress:
        progress("openalex_landing_pages")
    for landing in landing_candidates[:12]:
        response: requests.Response | None = None
        landing_retries = retries if is_pmc_repository_url(landing["url"]) else 0
        for retry in range(landing_retries + 1):
            try:
                response = session.get(
                    landing["url"],
                    timeout=timeout,
                    allow_redirects=True,
                    headers={"Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"},
                )
                if (
                    retryable_pmc_status(response.status_code)
                    and retry < landing_retries
                ):
                    response.close()
                    response = None
                    if delay:
                        time.sleep(delay)
                    continue
                break
            except requests.RequestException as exc:
                if retry >= landing_retries:
                    attempts.append(
                        {
                            "stage": "landing_page",
                            "source": landing.get("source"),
                            "url": landing["url"],
                            "host": landing["host"],
                            "retry_count": retry,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        }
                    )
                elif delay:
                    time.sleep(delay)
        if response is None:
            continue
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        discovered: list[str] = []
        if response.status_code == 200 and response.content.startswith(b"%PDF-"):
            discovered = [response.url]
        elif (
            response.status_code == 200
            and "html" in content_type.casefold()
            and len(response.content) <= 8 * 1024 * 1024
        ):
            discovered = discover_pdf_urls(response.text, response.url)
        for derived in derive_publisher_pdf_urls(doi, response.url):
            if derived not in discovered:
                discovered.append(derived)
        attempts.append(
            {
                "stage": "landing_page",
                "source": landing.get("source"),
                "license": landing.get("license", ""),
                "version": landing.get("version", ""),
                "url": landing["url"],
                "final_url": response.url,
                "host": landing["host"],
                "status": response.status_code,
                "content_type": content_type,
                "bytes": len(response.content),
                "human_verification": response_requires_human_verification(response),
                "discovered_pdf_urls": discovered,
            }
        )
        for url in discovered:
            discovered_candidates.append(
                {
                    "url": url,
                    "host": safe_url_host(url),
                    "license": landing.get("license", ""),
                    "version": landing.get("version", ""),
                    "source": "publisher_oa",
                    "referer": response.url,
                }
            )
    discovered_candidates = list(
        {
            str(candidate.get("url") or ""): candidate
            for candidate in discovered_candidates
            if candidate.get("url") and candidate.get("url") not in attempted_urls
        }.values()
    )
    discovered_result = try_pdf_candidates(discovered_candidates)
    if discovered_result is not None:
        return discovered_result
    return {
        "ok": False,
        "source": "openalex",
        "reason": "开放获取候选地址及出版商落地页均未返回有效 PDF",
        "oa_status": (work.get("open_access") or {}).get("oa_status"),
        "unpaywall_oa_status": unpaywall.get("oa_status"),
        "crossref_licenses": crossref_licenses,
        "mdpi_resolution": mdpi_resolution,
        "metadata_errors": [
            error for error in (metadata_error, unpaywall_error, crossref_error) if error
        ],
        "attempts": attempts,
    }


def find_chromium() -> Path | None:
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates[0:0] = [
            Path(local_app_data)
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe",
            Path(local_app_data)
            / "Microsoft"
            / "Edge"
            / "Application"
            / "msedge.exe",
        ]
    return next((path for path in candidates if path.exists()), None)


def download_elsevier(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    overwrite: bool,
    output_format: str,
    phase: str = "all",
) -> dict[str, Any]:
    if not doi.casefold().startswith("10.1016/"):
        return {
            "ok": False,
            "source": "elsevier",
            "reason": "DOI 不是 Elsevier 10.1016 前缀",
            "skipped": True,
            "attempts": [],
        }
    keys = config.get("api_keys") or {}
    api_key = str(keys.get("elsevier") or "").strip()
    inst_token = str(keys.get("elsevier_inst_token") or "").strip()
    if not api_key:
        return {
            "ok": False,
            "source": "elsevier",
            "reason": "未配置 Elsevier API Key",
            "skipped": True,
            "attempts": [],
        }
    if output_path.exists() and not overwrite:
        return {
            "ok": False,
            "source": "elsevier",
            "reason": f"目标文件已存在：{output_path}",
            "attempts": [],
        }
    timeout = max(
        1,
        int((config.get("network") or {}).get("download_timeout_seconds") or 90),
    )
    elsevier_options = config.get("elsevier") or {}
    use_aam = bool(elsevier_options.get("use_aam", False))
    use_xml_reconstruction = bool(
        elsevier_options.get("use_xml_reconstruction", False)
    )
    if phase not in {"all", "publisher_only", "fallbacks_only"}:
        raise ValueError(f"Unsupported Elsevier phase: {phase}")
    if phase == "publisher_only":
        use_aam = False
        use_xml_reconstruction = False
    skip_publisher_pdf = phase == "fallbacks_only"
    figure_download_workers = max(
        1, min(8, int(elsevier_options.get("figure_download_workers") or 4))
    )
    proxy = dict(elsevier_fulltext.DEFAULT_PROXY)
    http = elsevier_fulltext.session(proxy)
    chromium = find_chromium()
    started = time.monotonic()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with temporary_folder("elsevier-fulltext-") as temporary:
        try:
            result = elsevier_fulltext.test_doi(
                http,
                doi,
                api_key,
                inst_token,
                proxy,
                timeout,
                Path(temporary),
                True,
                chromium,
                use_aam=use_aam,
                use_xml_reconstruction=use_xml_reconstruction,
                want_xml_output=output_format == "xml",
                figure_download_workers=figure_download_workers,
                skip_publisher_pdf=skip_publisher_pdf,
            )
        except Exception as exc:
            return {
                "ok": False,
                "source": "elsevier",
                "reason": f"Elsevier API 异常：{type(exc).__name__}: {exc}",
                "attempts": getattr(exc, "literature_transfer_attempts", []),
            }
        saved = result.get("saved") or {}
        if output_format == "xml":
            selected = saved.get("xml")
            xml_meta = result.get("xml") or {}
            if selected and xml_meta.get("fulltext_detected"):
                shutil.copy2(selected, output_path)
                info = xml_fulltext_info(output_path)
                if info["valid"]:
                    return {
                        "ok": True,
                        "source": "elsevier_api_xml",
                        "classification": result.get("classification"),
                        "path": str(output_path.resolve()),
                        "bytes": info["bytes"],
                        "body_text_chars": info["body_text_chars"],
                        "elapsed_s": round(time.monotonic() - started, 2),
                        "attempts": [
                            {
                                "phase": phase,
                                "pdf_status": (result.get("pdf") or {}).get("status_code"),
                                "main_pdf_object": result.get("main_pdf_object"),
                                "xml_status": xml_meta.get("status_code"),
                            }
                        ],
                    }
        else:
            selected = saved.get("fulltext_pdf")
            if selected:
                shutil.copy2(selected, output_path)
                info = pdf_info(output_path)
                if info["valid"] and (info["pages"] is None or info["pages"] > 1):
                    return {
                        "ok": True,
                        "source": "elsevier_api",
                        "classification": result.get("classification"),
                        "path": str(output_path.resolve()),
                        "bytes": info["bytes"],
                        "pages": info["pages"],
                        "elapsed_s": round(time.monotonic() - started, 2),
                        "attempts": [
                            {
                                "phase": phase,
                                "pdf_status": (result.get("pdf") or {}).get("status_code"),
                                "xml_status": (result.get("xml") or {}).get("status_code"),
                                "object_status": (
                                    result.get("xml_pdf_object") or {}
                                ).get("status"),
                            }
                        ],
                    }
        output_path.unlink(missing_ok=True)
        classification = result.get("classification")
        pdf_result = result.get("pdf") or {}
        els_status = str(pdf_result.get("els_status") or "")
        if (
            classification == "single_page_metadata_only"
            or "not entitled" in els_status.casefold()
        ):
            if phase == "publisher_only":
                reason = (
                    "Elsevier API 出版版 PDF 仅返回 1 页受限预览"
                )
                error_type = "elsevier_publisher_pdf_unavailable"
            else:
                reason = (
                    "Elsevier API 仅返回 1 页受限预览，且已启用的 AAM/XML "
                    "后备未产生可验证全文。"
                )
                error_type = "elsevier_not_entitled"
            return {
                "ok": False,
                "source": "elsevier",
                "error_type": error_type,
                "reason": reason,
                "classification": classification,
                "attempts": [
                    {
                        "phase": phase,
                        "pdf_status": pdf_result.get("status_code"),
                        "pdf_pages": pdf_result.get("pages"),
                        "els_status": els_status,
                        "transfer_attempts": pdf_result.get("transfer_attempts", []),
                        "xml_status": (result.get("xml") or {}).get("status_code"),
                        "xml_els_status": (result.get("xml") or {}).get("els_status"),
                        "xml_fulltext": bool(
                            (result.get("xml") or {}).get("fulltext_detected")
                        ),
                        "object_catalog_status": (
                            result.get("object_retrieval") or {}
                        ).get("status_code"),
                        "aam_pdf_candidates": (
                            result.get("object_retrieval") or {}
                        ).get("aam_pdf_candidates"),
                        "object_status": (
                            result.get("xml_pdf_object") or {}
                        ).get("status"),
                        "aam_redirect_status": (
                            result.get("aam_redirect") or {}
                        ).get("status_code"),
                        "object_attempts": (
                            result.get("xml_pdf_object") or {}
                        ).get("attempts", []),
                    }
                ],
            }
        return {
            "ok": False,
            "source": "elsevier",
            "reason": "Elsevier API 未返回可验证的完整全文",
            "classification": result.get("classification"),
            "attempts": [
                {
                    "phase": phase,
                    "pdf": result.get("pdf"),
                    "xml": result.get("xml"),
                        "xml_pdf_object": result.get("xml_pdf_object"),
                        "aam_redirect": result.get("aam_redirect"),
                }
            ],
        }


def make_webvpn_client(config: dict[str, Any]) -> Any:
    """Compatibility wrapper around the selected institution adapter."""
    client, _entry = make_institution_client(config)
    return client


def webvpn_session_status(config: dict[str, Any]) -> dict[str, Any]:
    return institution_session_status(config)


def download_webvpn(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    overwrite: bool,
    output_format: str,
    progress=None,
) -> dict[str, Any]:
    return download_via_institution(
        doi,
        output_path,
        config,
        overwrite=overwrite,
        output_format=output_format,
        progress=progress,
    )


def output_path_for(doi: str, output_dir: Path, output_format: str) -> Path:
    name = doi_filename(doi)
    if output_format == "xml":
        name = str(Path(name).with_suffix(".xml"))
    return output_dir / name


def actionable_failure_reason(attempts: list[dict[str, Any]]) -> str:
    """Turn provider diagnostics into a useful one-line GUI explanation."""
    institution_attempted = any(
        attempt.get("source") in {"institution", "webvpn"}
        for attempt in attempts
    )
    if not institution_attempted:
        for attempt in attempts:
            if attempt.get("source") != "openalex":
                continue
            for detail in attempt.get("details") or []:
                if detail.get("host") == "ojs.s-p.sg" and detail.get("status") == 403:
                    return (
                        "开放期刊官网要求滑块人机验证（HTTP 403），后台脚本不能代替人工完成；"
                        "请在浏览器完成验证后从期刊页面下载。"
                    )

    for attempt in attempts:
        if attempt.get("source") != "webvpn":
            continue
        for detail in attempt.get("details") or []:
            if (
                detail.get("target_host") == "asmedigitalcollection.asme.org"
                and detail.get("redirected_from_pdf")
            ):
                return (
                    "所选机构接入未被 ASME 识别为拥有该文的下载权限："
                    "PDF 请求被重定向回摘要页；校园网直连时请确认当前出口属于校园订阅 IP，"
                    "其他接入方式请检查图书馆 ASME 订阅范围或登录状态。"
                )

    for attempt in attempts:
        if attempt.get("source") != "webvpn":
            continue
        for detail in attempt.get("details") or []:
            if (
                detail.get("target_host") == "asmedigitalcollection.asme.org"
                and detail.get("status") == 200
                and detail.get("detail") == "HTML/non-PDF response"
            ):
                return (
                    "所选机构接入已打开 ASME 页面，但 PDF 下载仍返回动态 HTML，"
                    "不是 PDF 文件；请在相同网络出口的浏览器中确认权限并点击下载。"
                )

    useful = []
    for attempt in reversed(attempts):
        reason = str(attempt.get("reason") or "").strip()
        if not reason or reason == "DOI 不是 Elsevier 10.1016 前缀":
            continue
        if reason == "OpenAlex/Unpaywall/Crossref 未声明可用的开放 PDF":
            reason = "未找到可用 PDF"
        elif reason.startswith("No validated PDF found through WebVPN HTTP"):
            reason = "未取得可验证的 PDF"
        source_name = str(attempt.get("source") or "")
        label = {
            "openalex": "开放获取来源",
            "elsevier": "Elsevier",
            "elsevier_publisher_api": "Elsevier API",
            "elsevier_aam_xml": "Elsevier AAM/XML 后备",
            "webvpn": "学校机构订阅",
            "institution": "学校机构订阅",
        }.get(source_name, source_name or "来源")
        if label == "Elsevier API" and reason.startswith("Elsevier API "):
            reason = reason.removeprefix("Elsevier API ")
        if label == "学校机构订阅" and reason.startswith("学校机构订阅"):
            reason = reason.removeprefix("学校机构订阅")
        useful.append(f"{label}：{reason}")
    return "；".join(useful) or "所有已启用的合法来源均未返回完整全文"


def _institution_browser_cookies(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load selected WebVPN/EZProxy cookies for a temporary assisted browser."""
    try:
        entry, _catalog_path, _warnings = selected_school(config)
        if entry.access_type not in {"webvpn", "ezproxy"}:
            return []
        payload = json.loads(cookie_file_for(entry, config).read_text(encoding="utf-8"))
    except (InstitutionalConfigError, OSError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("cookies") or []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def verification_item_from_result(
    result: dict[str, Any], output_dir: Path, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Build a second-pass browser task for an explicit CAPTCHA response."""
    attempt_map = {
        str(attempt.get("source") or ""): [
            item for item in (attempt.get("details") or []) if isinstance(item, dict)
        ]
        for attempt in (result.get("attempts") or [])
    }

    # Public OA and institutional-repository challenges.
    details = attempt_map.get("openalex", [])
    challenge_details = [item for item in details if item.get("human_verification")]
    if challenge_details:
        challenge_hosts = {
            safe_url_host(str(item.get("final_url") or item.get("url") or ""))
            for item in challenge_details
        }
        challenge_hosts.discard("")
        pdf_urls: list[str] = []
        landing_url = ""
        selected_detail: dict[str, Any] = challenge_details[0]
        for item in details:
            final_url = str(item.get("final_url") or item.get("url") or "")
            actual_host = safe_url_host(final_url)
            if item.get("stage") == "landing_page" and actual_host in challenge_hosts:
                landing_url = landing_url or final_url
                selected_detail = item
            if item.get("stage") == "pdf_candidate" and actual_host in challenge_hosts:
                candidate_url = str(item.get("url") or "").strip()
                if candidate_url:
                    pdf_urls.append(candidate_url)
            for discovered in item.get("discovered_pdf_urls") or []:
                discovered_url = str(discovered or "").strip()
                if safe_url_host(discovered_url) in challenge_hosts:
                    pdf_urls.append(discovered_url)
        pdf_urls = list(dict.fromkeys(pdf_urls))
        if not landing_url:
            landing_url = str(
                challenge_details[0].get("final_url")
                or challenge_details[0].get("url")
                or (pdf_urls[0] if pdf_urls else "")
            )
            ojs = re.search(
                r"/article/(?:viewFile|download)/(\d+)(?:/.*)?$", landing_url, re.I
            )
            if ojs:
                parsed = urllib.parse.urlsplit(landing_url)
                prefix = parsed.path[: ojs.start()]
                landing_url = urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        f"{prefix}/article/view/{ojs.group(1)}",
                        "",
                        "",
                    )
                )
        host = safe_url_host(landing_url)
        if host:
            doi = str(result.get("doi") or "")
            source = str(selected_detail.get("source") or "")
            return {
                "doi": doi,
                "host": host,
                "landing_url": landing_url,
                "pdf_urls": pdf_urls,
                "referer": landing_url,
                "output_path": output_path_for(doi, output_dir, "pdf"),
                "access_kind": (
                    "repository" if source == "openalex_repository" else "open_access"
                ),
                "license": str(selected_detail.get("license") or ""),
                "version": str(selected_detail.get("version") or ""),
            }

    # Subscription publisher challenge returned through the selected adapter.
    details = attempt_map.get("webvpn", [])
    if any(item.get("entitlement_state") == "not_entitled" for item in details):
        return None
    challenge_details = [item for item in details if item.get("human_verification")]
    if not challenge_details:
        return None
    pdf_urls = []
    for item in challenge_details:
        response_url = str(item.get("response_url") or "").strip()
        target_url = str(item.get("target_url") or "").strip()
        candidate = response_url or target_url
        if candidate.startswith(("https://", "http://")) and any(
            hint in (target_url or candidate).casefold()
            for hint in ("/pdf", ".pdf", "pdfdirect", "download")
        ):
            pdf_urls.append(candidate)
    landing_url = ""
    for item in details:
        if "landing" not in str(item.get("label") or "").casefold():
            continue
        landing_url = str(item.get("response_url") or item.get("target_url") or "")
        if landing_url:
            break
    if not landing_url:
        landing_url = str(
            challenge_details[0].get("response_url")
            or challenge_details[0].get("target_url")
            or ""
        )
    host = safe_url_host(landing_url)
    if not host:
        return None
    doi = str(result.get("doi") or "")
    return {
        "doi": doi,
        "host": host,
        "landing_url": landing_url,
        "pdf_urls": list(dict.fromkeys(pdf_urls)),
        "referer": landing_url,
        "output_path": output_path_for(doi, output_dir, "pdf"),
        "access_kind": "institution",
        "browser_cookies": _institution_browser_cookies(config),
        "license": "institutional-access",
        "version": "publishedVersion",
    }


def download_record(
    doi: str,
    output_dir: Path,
    config: dict[str, Any],
    sources: list[str],
    *,
    overwrite: bool,
    output_format: str,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    output_path = output_path_for(doi, output_dir, output_format)
    if output_path.exists() and not overwrite:
        info = (
            pdf_info(output_path)
            if output_format == "pdf"
            else xml_fulltext_info(output_path)
        )
        if info.get("valid"):
            identity = (
                pdf_identity_info(output_path, doi)
                if output_format == "pdf"
                else {"status": "not_applicable", "match": None}
            )
            if identity.get("match") is False:
                LOG.warning(
                    "[%s] cached PDF identity mismatch: %s",
                    doi,
                    identity.get("reason"),
                )
                output_path.unlink(missing_ok=True)
            else:
                return {
                    "ok": True,
                    "doi": doi,
                    "source": "cache",
                    "path": str(output_path.resolve()),
                    "identity_validation": identity,
                    **info,
                    "attempts": [],
                }

    all_attempts: list[dict[str, Any]] = []
    if (
        output_format == "pdf"
        and doi.casefold().startswith("10.1016/")
        and "elsevier" in sources
        and "webvpn" in sources
    ):
        # Elsevier has a four-level priority that cannot be represented by a
        # flat provider list: publisher API -> AAM -> XML reconstruction ->
        # institution browser. Open-access discovery remains the fast first
        # source when enabled.
        source_steps: list[tuple[str, str, str]] = []
        if "openalex" in sources:
            source_steps.append(("openalex", "all", "openalex"))
        source_steps.append(
            ("elsevier", "publisher_only", "elsevier_publisher_api")
        )
        elsevier_options = config.get("elsevier") or {}
        if bool(elsevier_options.get("use_aam", False)) or bool(
            elsevier_options.get("use_xml_reconstruction", False)
        ):
            source_steps.append(
                ("elsevier", "fallbacks_only", "elsevier_aam_xml")
            )
        source_steps.append(("webvpn", "all", "institution"))
    else:
        source_steps = [(source, "all", source) for source in sources]

    for source, elsevier_phase, step_label in source_steps:
        if progress:
            progress(source, "source_start")
        try:
            if source == "openalex":
                if output_format != "pdf":
                    result = {
                        "ok": False,
                        "source": source,
                        "reason": "OpenAlex 来源只下载开放 PDF",
                        "skipped": True,
                        "attempts": [],
                    }
                else:
                    result = download_openalex(
                        doi,
                        output_path,
                        config,
                        overwrite=overwrite,
                        progress=(
                            (lambda stage: progress("openalex", stage))
                            if progress
                            else None
                        ),
                    )
            elif source == "elsevier":
                result = download_elsevier(
                    doi,
                    output_path,
                    config,
                    overwrite=overwrite,
                    output_format=output_format,
                    phase=elsevier_phase,
                )
            elif source == "webvpn":
                if progress:
                    progress(source, "institution_download")
                result = download_webvpn(
                    doi,
                    output_path,
                    config,
                    overwrite=overwrite,
                    output_format=output_format,
                    progress=(lambda stage: progress(source, stage)) if progress else None,
                )
            else:
                continue
        except Exception as exc:
            # A flaky publisher or institution gateway must not abort the DOI
            # record (or the rest of a batch). Preserve the exception as a
            # provider-scoped diagnostic and continue to the next source.
            result = {
                "ok": False,
                "source": source,
                "error_type": "provider_exception",
                "reason": f"{type(exc).__name__}: {exc}",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "attempts": [],
            }
        attempt_record = {
            "source": step_label,
            "ok": bool(result.get("ok")),
            "error_type": result.get("error_type"),
            "reason": result.get("reason", ""),
            "details": result.get("attempts", []),
        }
        if not result.get("ok"):
            attempt_record["provider_return"] = result
        all_attempts.append(attempt_record)
        if progress:
            progress(source, "source_complete")
        if result.get("ok"):
            if output_format == "pdf":
                identity = pdf_identity_info(output_path, doi)
                result["identity_validation"] = identity
                if identity.get("match") is False:
                    output_path.unlink(missing_ok=True)
                    result = {
                        "ok": False,
                        "source": source,
                        "error_type": "pdf_identity_mismatch",
                        "reason": (
                            "下载结果不是目标 DOI 对应的论文："
                            + str(identity.get("reason") or "PDF identity mismatch")
                        ),
                        "identity_validation": identity,
                        "attempts": result.get("attempts", []),
                    }
                    attempt_record.update(
                        {
                            "ok": False,
                            "error_type": result["error_type"],
                            "reason": result["reason"],
                            "details": result["attempts"],
                            "provider_return": result,
                        }
                    )
                    all_attempts[-1] = attempt_record
                    continue
            return {
                **result,
                "doi": doi,
                "attempts": all_attempts,
            }
    institution_attempted = any(
        attempt.get("source") in {"institution", "webvpn"}
        for attempt in all_attempts
    )
    entitlement_failure = next(
        (
            attempt
            for attempt in reversed(all_attempts)
            if str(attempt.get("error_type") or "")
            == "institution_not_entitled"
        ),
        None,
    )
    if entitlement_failure is None and not institution_attempted:
        entitlement_failure = next(
            (
                attempt
                for attempt in reversed(all_attempts)
                if str(attempt.get("error_type") or "").endswith(
                    "_not_entitled"
                )
            ),
            None,
        )
    terminal_failure = next(
        (
            attempt
            for attempt in reversed(all_attempts)
            if attempt.get("error_type")
            and str(attempt.get("reason") or "").strip()
        ),
        None,
    )
    return {
        "ok": False,
        "doi": doi,
        "source": "none",
        "error_type": (
            str(entitlement_failure.get("error_type"))
            if entitlement_failure
            else (
                str(terminal_failure.get("error_type"))
                if terminal_failure
                else "all_sources_failed"
            )
        ),
        "reason": (
            entitlement_failure.get("reason")
            if entitlement_failure
            else actionable_failure_reason(all_attempts)
        ),
        "attempts": all_attempts,
    }


def _terminate_worker_tree(process: subprocess.Popen[str]) -> None:
    """Stop the DOI worker and any browser/renderer processes it launched."""
    if process.pid is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=8,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _worker_command():
    from suite_paths import entry_command
    return entry_command() + ["_download-kernel"]


def _last_worker_stage(stdout: str) -> tuple[str, str]:
    source = ""
    stage = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("worker_event") == "stage":
            source = str(event.get("source") or "")
            stage = str(event.get("stage") or "")
    return source, stage


def _communicate_with_human_wait(process, timeout_seconds):
    try:
        return process.communicate(timeout=max(0.1, float(timeout_seconds)))
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        _source, stage = _last_worker_stage(partial)
        if stage != "campus_human_verification":
            raise
        # One bounded allowance for user verification, not another download retry.
        return process.communicate(timeout=180)


def download_record_with_timeout(
    doi: str,
    output_dir: Path,
    config_path: Path,
    sources: list[str],
    *,
    overwrite: bool,
    output_format: str,
    timeout_seconds: float = DEFAULT_PAPER_PROCESS_TIMEOUT_SECONDS,
    force_campus_direct: bool = False,
) -> dict[str, Any]:
    """Run one DOI as a child CLI and enforce a hard wall-clock timeout."""
    command = [
        *_worker_command(),
        "_worker-download",
        doi,
        "--config",
        str(config_path),
        "--output-dir",
        str(output_dir),
        "--sources",
        ",".join(sources),
        "--format",
        output_format,
    ]
    if overwrite:
        command.append("--overwrite")
    process = subprocess.Popen(
        command,
        cwd=str(APP_DIR),
        env={**os.environ, "LITERATURE_FORCE_CAMPUS_DIRECT": "1" if force_campus_direct else "0"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        ),
    )
    try:
        stdout, stderr = _communicate_with_human_wait(process, timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_worker_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        source, stage = _last_worker_stage(stdout)
        stage_labels = {
            "source_start": "来源初始化",
            "openalex_metadata": "OpenAlex/Unpaywall/Crossref 元数据",
            "openalex_pdf_candidates": "开放 PDF 候选下载",
            "openalex_landing_pages": "开放仓储或出版商页面",
            "institution_download": "机构 PDF 下载（含浏览器）",
            "campus_direct_download": "校园网直连 PDF 下载",
            "source_complete": "来源收尾",
        }
        stage_text = stage_labels.get(stage, stage or "未知阶段")
        source_text = source or "internal"
        source_display = {
            "openalex": "开放获取来源",
            "elsevier": "Elsevier",
            "webvpn": "学校机构订阅",
            "internal": "内部处理",
        }.get(source_text, source_text)
        failure = {
            "ok": False,
            "doi": doi,
            "source": source_text,
            "error_type": "paper_timeout",
            "reason": (
                f"单篇处理超过 {int(timeout_seconds)} 秒，已终止并继续下一条；"
                f"最后阶段：{source_display} / {stage_text}"
            ),
            "last_source": source,
            "last_stage": stage,
            "attempts": [],
        }
        # A killed worker cannot run its in-process fallback. Give direct access
        # a separate bounded attempt, unless it was already the timed-out stage.
        if (not force_campus_direct and output_format == "pdf" and "webvpn" in sources
                and stage != "campus_direct_download"):
            config = load_config(config_path)
            entry, _, _ = selected_school(config)
            if entry.access_type == "webvpn":
                LOG.warning("[%s] 下载进程超时，切换校园网直连重试", doi)
                direct = download_record_with_timeout(
                    doi, output_dir, config_path, ["webvpn"], overwrite=overwrite,
                    output_format=output_format, timeout_seconds=timeout_seconds,
                    force_campus_direct=True,
                )
                direct["webvpn_failure"] = failure
                direct["fallback_from"] = "webvpn"
                return direct
        return failure
    for line in reversed(stdout.splitlines()):
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    return {
        "ok": False,
        "doi": doi,
        "source": "internal",
        "error_type": "worker_exited",
        "reason": (stderr.strip() or f"单篇下载工作进程退出码 {process.returncode}")[-1000:],
        "attempts": [],
    }


def run_worker_download(args: argparse.Namespace) -> int:
    """Internal one-DOI command used by the parent timeout supervisor."""
    config = load_config(args.config)
    if os.environ.get("LITERATURE_FORCE_CAMPUS_DIRECT") == "1":
        config.setdefault("_runtime", {})["force_campus_direct"] = True
    try:
        sources = normalize_sources(args.sources, config)
        doi = normalize_doi(args.doi)
        def report_progress(source: str, stage: str) -> None:
            emit_event(
                {
                    "worker_event": "stage",
                    "doi": doi,
                    "source": source,
                    "stage": stage,
                }
            )

        result = download_record(
            doi,
            args.output_dir.expanduser().resolve(),
            config,
            sources,
            overwrite=args.overwrite,
            output_format=args.format,
            progress=report_progress,
        )
    except BaseException as exc:
        result = {
            "ok": False,
            "doi": str(args.doi),
            "source": "internal",
            "error_type": "worker_exception",
            "reason": f"{type(exc).__name__}: {exc}",
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
            "attempts": [],
        }
    emit_event(result)
    return 0


def run_webvpn_login(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    return login_institution(config, timeout)


def redacted_readiness(config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    keys = config.get("api_keys") or {}
    institution = institution_session_status(config)
    return {
        "config_path": str(config_path.resolve()),
        "config_exists": config_path.exists(),
        "providers": {
            "openalex": {
                "enabled": True,
                "has_api_key": bool(str(keys.get("openalex") or "").strip()),
                "note": (
                    "API Key 可选；下载明确 OA PDF，并补查 OpenAlex "
                    "机构仓储库记录"
                ),
            },
            "elsevier": {
                "enabled": True,
                "has_api_key": bool(str(keys.get("elsevier") or "").strip()),
                "has_inst_token": bool(
                    str(keys.get("elsevier_inst_token") or "").strip()
                ),
                "note": "PDF → AAM-PDF → XML 重建",
            },
            "institution": institution,
            # Kept for older callers that still read providers.webvpn.
            "webvpn": institution,
        },
    }


def run_schools(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    catalog_path = schools_path_from_config(config).resolve()
    try:
        entries, warnings = load_school_catalog(catalog_path)
    except InstitutionalConfigError as exc:
        emit(
            {
                "ok": False,
                "error_type": "school_catalog_error",
                "reason": str(exc),
                "catalog_path": str(catalog_path),
            },
            args.json,
        )
        return 2
    query = str(args.query or "").strip().casefold()
    if query:
        entries = [
            entry
            for entry in entries
            if query in entry.name.casefold()
            or query in entry.province.casefold()
            or query in entry.type_label.casefold()
            or query in entry.base_url.casefold()
            or query in entry.gateway.casefold()
        ]
    settings = config.get("institution") or {}
    payload = {
        "ok": True,
        "catalog_path": str(catalog_path),
        "count": len(entries),
        "warnings": warnings,
        "selected_school_id": str(settings.get("school_id") or "campus-network-direct"),
        "selected_school_name": str(settings.get("school_name") or "校园网直连"),
        "schools": [entry.public_dict() for entry in entries],
    }
    emit(payload, args.json)
    return 0


def run_select_school(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    catalog_path = schools_path_from_config(config).resolve()
    try:
        entries, warnings = load_school_catalog(catalog_path)
        entry = find_school(entries, args.school)
    except InstitutionalConfigError as exc:
        emit(
            {
                "ok": False,
                "error_type": "school_not_found",
                "reason": str(exc),
                "catalog_path": str(catalog_path),
            },
            args.json,
        )
        return 2
    apply_school_selection(config, entry, catalog_path)
    atomic_write(
        args.config.expanduser().resolve(),
        (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    emit(
        {
            "ok": True,
            "selected": entry.public_dict(),
            "catalog_path": str(catalog_path),
            "warnings": warnings,
            "config_path": str(args.config.expanduser().resolve()),
        },
        args.json,
    )
    return 0


def run_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    payload = {
        "ok": True,
        "version": APP_VERSION,
        **redacted_readiness(args.config, config),
    }
    payload["ok"] = bool(
        payload["providers"]["openalex"]["enabled"]
        or payload["providers"]["elsevier"]["has_api_key"]
        or payload["providers"]["webvpn"].get("valid")
    )
    emit(payload, args.json)
    return 0 if payload["ok"] else 2


def run_download(args: argparse.Namespace) -> int:
    batch_started = time.monotonic()
    json_lines = bool(getattr(args, "json_lines", False))
    # Enable the visible second-pass browser by default so the user can finish
    # publisher CAPTCHA or other human-verification challenges when required.
    args.interactive_browser = bool(getattr(args, "interactive_browser", False))
    config = load_config(args.config)
    raw_paper_timeout = (config.get("download") or {}).get(
        "per_paper_timeout_seconds"
    )
    try:
        configured_paper_timeout = float(raw_paper_timeout)
    except (TypeError, ValueError):
        configured_paper_timeout = DEFAULT_PAPER_PROCESS_TIMEOUT_SECONDS
    paper_timeout_seconds = max(120.0, configured_paper_timeout)
    try:
        sources = normalize_sources(args.sources, config)
        dois = [normalize_doi(value) for value in args.doi]
    except ValueError as exc:
        error = {"ok": False, "error_type": "invalid_argument", "reason": str(exc)}
        if json_lines:
            emit_event({"event": "error", **error})
        else:
            emit(error, args.json)
        return 2
    limit = max(1, int((config.get("download") or {}).get("max_batch_papers") or 60))
    if len(dois) > limit:
        error = {
            "ok": False,
            "error_type": "batch_limit",
            "reason": f"单次最多下载 {limit} 篇",
        }
        if json_lines:
            emit_event({"event": "error", **error})
        else:
            emit(error, args.json)
        return 2
    output_dir = args.output_dir.expanduser().resolve()
    preflight: dict[str, Any] = {}
    if "webvpn" in sources:
        preflight["webvpn"] = webvpn_session_status(config)
        config.setdefault("_runtime", {})["institution_status"] = preflight[
            "webvpn"
        ]
        if (
            not preflight["webvpn"].get("valid")
            and not preflight["webvpn"].get("can_attempt")
        ):
            LOG.warning(
                "学校机构会话不可用：%s；将先尝试其他来源。需要时运行 login 命令刷新。",
                preflight["webvpn"].get("message"),
            )
    if json_lines:
        emit_event(
            {
                "event": "start",
                "version": APP_VERSION,
                "total": len(dois),
                "format": args.format,
                "sources": sources,
                "interactive_browser": bool(args.interactive_browser),
                "preflight": preflight,
                "output_dir": str(output_dir),
            }
        )
    results: list[dict[str, Any]] = []
    record_started_by_doi: dict[str, float] = {}
    for index, doi in enumerate(dois, start=1):
        record_started = time.monotonic()
        record_started_by_doi[doi] = record_started
        LOG.info("[%s/%s] %s", index, len(dois), doi)
        if json_lines:
            emit_event(
                {
                    "event": "item_start",
                    "index": index,
                    "completed": index - 1,
                    "total": len(dois),
                    "doi": doi,
                    "timeout_seconds": int(paper_timeout_seconds),
                }
            )
        try:
            result = download_record_with_timeout(
                doi,
                output_dir,
                args.config,
                sources,
                overwrite=args.overwrite,
                output_format=args.format,
                timeout_seconds=paper_timeout_seconds,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "doi": doi,
                "source": "internal",
                "error_type": "unhandled_exception",
                "reason": f"{type(exc).__name__}: {exc}",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
                "traceback": traceback.format_exc(),
                "attempts": [],
            }
            LOG.exception("下载 %s 时出现未处理异常", doi)
        result["elapsed_s"] = round(time.monotonic() - record_started, 2)
        if not result.get("ok") and args.format == "pdf":
            manual_item = verification_item_from_result(result, output_dir, config)
            if manual_item is not None:
                manual_url = str(
                    manual_item.get("landing_url")
                    or next(iter(manual_item.get("pdf_urls") or []), "")
                ).strip()
                if manual_url.startswith(("https://", "http://")):
                    result["manual_download_url"] = manual_url
        result = redact_diagnostics(result)
        failure_path = failure_report_path_for(doi, output_dir)
        if result.get("ok"):
            try:
                failure_path.unlink(missing_ok=True)
            except OSError as exc:
                LOG.warning("无法清理旧失败诊断文件 %s：%s", failure_path, exc)
        else:
            try:
                report_path = write_failure_report(
                    doi,
                    output_dir,
                    result,
                    output_format=args.format,
                    sources=sources,
                    preflight=preflight,
                )
                result["error_log_path"] = str(report_path)
            except Exception as exc:
                result["error_log_error"] = f"{type(exc).__name__}: {exc}"
                LOG.exception("无法写入失败诊断文件：%s", failure_path)
        results.append(result)
        if json_lines:
            emit_event(
                {
                    "event": "result",
                    "index": index,
                    "completed": index,
                    "total": len(dois),
                    "result": compact_progress_result(result),
                }
            )

    # Human verification is deliberately a second pass. Normal providers run
    # for every DOI first, so a CAPTCHA never serially blocks the batch.
    if args.interactive_browser and args.format == "pdf":
        groups: dict[str, list[dict[str, Any]]] = {}
        result_positions = {
            str(result.get("doi") or ""): position
            for position, result in enumerate(results)
        }
        for result in results:
            if result.get("ok"):
                continue
            item = verification_item_from_result(result, output_dir, config)
            if item is not None:
                groups.setdefault(item["host"], []).append(item)

        for host, items in groups.items():
            verification_started = time.monotonic()
            if json_lines:
                emit_event(
                    {
                        "event": "challenge",
                        "host": host,
                        "dois": [item["doi"] for item in items],
                        "count": len(items),
                        "timeout": args.challenge_timeout,
                        "message": (
                            f"普通下载已全部完成；请在弹出的浏览器中完成 {host} 人工验证。"
                        ),
                    }
                )
            try:
                assisted_results = download_batch_after_manual_verification(
                    items,
                    timeout=float(args.challenge_timeout),
                    overwrite=args.overwrite,
                )
            except Exception as exc:
                assisted_results = [
                    {
                        "ok": False,
                        "doi": item["doi"],
                        "reason": f"{type(exc).__name__}: {exc}",
                        "attempts": [],
                    }
                    for item in items
                ]
            assisted_by_doi = {
                str(item.get("doi") or ""): item for item in assisted_results
            }
            for item in items:
                doi = item["doi"]
                position = result_positions[doi]
                previous = results[position]
                assisted = assisted_by_doi.get(
                    doi,
                    {
                        "ok": False,
                        "doi": doi,
                        "reason": "人工辅助浏览器没有返回该 DOI 的处理结果",
                        "attempts": [],
                    },
                )
                human_attempt = {
                    "source": "human_browser",
                    "ok": bool(assisted.get("ok")),
                    "error_type": None if assisted.get("ok") else "human_verification_failed",
                    "reason": str(assisted.get("reason") or ""),
                    "details": assisted.get("attempts") or [],
                    "provider_return": assisted,
                }
                combined_attempts = [*(previous.get("attempts") or []), human_attempt]
                output_path = Path(item["output_path"])
                info = pdf_info(output_path) if assisted.get("ok") else {"valid": False}
                if assisted.get("ok") and info.get("valid"):
                    access_kind = str(item.get("access_kind") or "open_access")
                    assisted_source = {
                        "institution": "human_browser_institution",
                        "repository": "human_browser_repository",
                    }.get(access_kind, "human_browser_oa")
                    updated = {
                        "ok": True,
                        "doi": doi,
                        "source": assisted_source,
                        "path": str(output_path.resolve()),
                        "bytes": info.get("bytes"),
                        "pages": info.get("pages"),
                        "license": str(
                            item.get("license")
                            or (
                                "publisher-declared-open-access"
                                if access_kind == "open_access"
                                else ""
                            )
                        ),
                        "version": str(item.get("version") or "publishedVersion"),
                        "resolved_host": host,
                        "human_verification": True,
                        "method": assisted.get("method"),
                        "browser": assisted.get("browser"),
                        "attempts": combined_attempts,
                    }
                else:
                    if assisted.get("ok"):
                        output_path.unlink(missing_ok=True)
                        assisted["reason"] = "浏览器返回的文件未通过 PDF 结构校验"
                        human_attempt["ok"] = False
                        human_attempt["error_type"] = "invalid_pdf"
                        human_attempt["reason"] = assisted["reason"]
                    updated = {
                        **previous,
                        "reason": "人工辅助验证未完成："
                        + str(assisted.get("reason") or "未检测到有效 PDF"),
                        "attempts": combined_attempts,
                    }
                # A second-pass browser download is a new processing phase for
                # the DOI. Do not include time spent on unrelated DOI records.
                updated["elapsed_s"] = round(
                    time.monotonic() - verification_started, 2
                )
                updated = redact_diagnostics(updated)
                failure_path = failure_report_path_for(doi, output_dir)
                if updated.get("ok"):
                    failure_path.unlink(missing_ok=True)
                    updated.pop("error_log_path", None)
                else:
                    report_path = write_failure_report(
                        doi,
                        output_dir,
                        updated,
                        output_format=args.format,
                        sources=sources,
                        preflight=preflight,
                    )
                    updated["error_log_path"] = str(report_path)
                results[position] = updated
                if json_lines:
                    emit_event(
                        {
                            "event": "result_update",
                            "index": position + 1,
                            "completed": len(dois),
                            "total": len(dois),
                            "result": compact_progress_result(updated),
                        }
                    )
    payload = {
        "ok": all(item.get("ok") for item in results),
        "version": APP_VERSION,
        "format": args.format,
        "sources": sources,
        "preflight": preflight,
        "output_dir": str(output_dir),
        "results": results,
        "elapsed_s": round(time.monotonic() - batch_started, 2),
    }
    if json_lines:
        success_count = sum(bool(item.get("ok")) for item in results)
        emit_event(
            {
                "event": "complete",
                "ok": payload["ok"],
                "version": APP_VERSION,
                "total": len(results),
                "completed": len(results),
                "successes": success_count,
                "failures": len(results) - success_count,
                "output_dir": str(output_dir),
                "elapsed_s": payload["elapsed_s"],
            }
        )
    else:
        emit(payload, args.json)
    return 0 if payload["ok"] else 2


def run_login(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    result = run_webvpn_login(config, args.timeout)
    emit(result, args.json)
    return 0 if result.get("ok") else 2


def emit(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload.get("results") is not None:
        for result in payload["results"]:
            if result.get("ok"):
                print(
                    f"OK {result['doi']} [{result.get('source')}] "
                    f"-> {result.get('path')}"
                )
            else:
                print(f"FAIL {result['doi']}: {result.get('reason')}")
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def emit_event(payload: dict[str, Any]) -> None:
    """Write one compact JSON event and make it visible to callers immediately."""
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def compact_progress_result(result: dict[str, Any]) -> dict[str, Any]:
    """Keep GUI events small; full provider diagnostics remain in failure TXT files."""
    fields = (
        "ok",
        "doi",
        "source",
        "error_type",
        "reason",
        "path",
        "error_log_path",
        "bytes",
        "pages",
        "elapsed_s",
        "manual_download_url",
        "last_source",
        "last_stage",
    )
    return {field: result[field] for field in fields if field in result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="期刊全文下载 CLI：OpenAlex OA、Elsevier API、学校机构订阅",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="显示脱敏后的来源配置和 WebVPN 状态")
    check.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    check.add_argument("--json", action="store_true")

    schools = sub.add_parser("schools", help="列出或搜索学校机构接入配置")
    schools.add_argument("query", nargs="?", default="")
    schools.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    schools.add_argument("--json", action="store_true")

    select_school = sub.add_parser("select-school", help="选择学校机构接入配置")
    select_school.add_argument("school", help="学校 ID、全名或唯一的部分名称")
    select_school.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    select_school.add_argument("--json", action="store_true")

    download = sub.add_parser("download", help="下载一个或多个 DOI")
    download.add_argument("doi", nargs="+")
    download.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    download.add_argument("--output-dir", type=Path, default=APP_DIR / "downloads")
    download.add_argument(
        "--sources",
        default="auto",
        help="auto 或逗号分隔：openalex,elsevier,webvpn",
    )
    download.add_argument("--format", choices=("pdf", "xml"), default="pdf")
    download.add_argument("--overwrite", action="store_true")
    download.add_argument(
        "--interactive-browser",
        action="store_true",
        help="普通下载全部结束后，对明确的人机验证失败项启动一次人工浏览器辅助",
    )
    download.add_argument(
        "--challenge-timeout",
        type=float,
        default=300,
        help="每个出版社人工验证批次的等待秒数",
    )
    download_output = download.add_mutually_exclusive_group()
    download_output.add_argument("--json", action="store_true")
    download_output.add_argument(
        "--json-lines",
        action="store_true",
        help="逐篇输出 start/result/complete JSON 事件",
    )

    worker = sub.add_parser("_worker-download", help=argparse.SUPPRESS)
    worker.add_argument("doi")
    worker.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    worker.add_argument("--output-dir", type=Path, required=True)
    worker.add_argument("--sources", default="auto")
    worker.add_argument("--format", choices=("pdf", "xml"), default="pdf")
    worker.add_argument("--overwrite", action="store_true")

    login = sub.add_parser("login", help="刷新所选学校的机构登录")
    login.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    login.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="等待人工登录的秒数",
    )
    login.add_argument("--json", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    configure_text_streams()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(CredentialRedactingFilter())
    if args.command != "_worker-download":
        # Keep everything beside the program: move old %LOCALAPPDATA% state
        # and drop per-run folders left by killed or crashed runs.
        try:
            migrate_legacy_state()
            cleanup_stale_runtime()
        except OSError as exc:
            LOG.warning("清理旧运行目录失败：%s", exc)
    if args.command == "check":
        return run_check(args)
    if args.command == "schools":
        return run_schools(args)
    if args.command == "select-school":
        return run_select_school(args)
    if args.command == "download":
        return run_download(args)
    if args.command == "_worker-download":
        return run_worker_download(args)
    if args.command == "login":
        return run_login(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
