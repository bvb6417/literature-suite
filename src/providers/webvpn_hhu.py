#!/usr/bin/env python3
"""
Download subscription PDFs through an authenticated WEngine WebVPN session.

This is an HTTP-only, legal institutional-access reproducer. It does not use a
browser, Sci-Hub, or an OA fallback for article downloads. The companion CDP
login helper produces the standard browser cookie JSON used here.

Dependencies:
    python -m pip install requests pycryptodome
"""

from __future__ import annotations

import argparse
import binascii
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

try:
    from . import elsevier_fulltext, mdpi_resolver
    from .app_paths import state_dir
except ImportError:  # pragma: no cover - direct script execution
    import elsevier_fulltext  # type: ignore
    import mdpi_resolver  # type: ignore
    from app_paths import state_dir  # type: ignore

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: pip install requests pycryptodome") from exc


DEFAULT_BASE_URL = "https://webvpn.hhu.edu.cn"
DEFAULT_KEY = "wrdvpnisthebest!"
DEFAULT_COOKIE_FILE = state_dir() / "webvpn_cookies.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 LiteratureDownloader/1"
)
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
PDF_HINTS = (
    ".pdf",
    "/pdf",
    "/stamppdf/getpdf.jsp",
    "pdfdirect",
    "download/pdf",
    "downloadpdf",
    "format=pdf",
    "type=pdf",
)
LOG = logging.getLogger("hhu_webvpn_pdf")


class DownloadError(RuntimeError):
    pass


def normalize_doi(value: str) -> str:
    value = value.strip()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.I)
    if not DOI_RE.match(value):
        raise ValueError(f"Invalid DOI: {value!r}")
    return value


def doi_filename(doi: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("._")
    return f"{safe}.pdf"


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def validate_aes_material(key: bytes, iv: bytes) -> None:
    if len(key) not in (16, 24, 32):
        raise ValueError("AES key must contain 16, 24, or 32 bytes")
    if len(iv) != 16:
        raise ValueError("AES-CFB IV must contain exactly 16 bytes")


def webvpn_url(target_url: str, base_url: str, key: bytes, iv: bytes) -> str:
    """Convert a normal URL to a WEngine WebVPN URL.

    Only the target hostname is AES-CFB encrypted. The IV is prefixed to the
    ciphertext as hex; scheme, port, path, and query stay visible.
    """
    parsed = urllib.parse.urlsplit(target_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Unsupported target URL: {target_url!r}")
    if parsed.username or parsed.password:
        raise ValueError("Credentials embedded in URLs are not supported")

    validate_aes_material(key, iv)
    try:
        from Crypto.Cipher import AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES
        except ImportError as exc:  # pragma: no cover
            raise DownloadError(
                "Missing AES implementation: pip install pycryptodome"
            ) from exc

    hostname = parsed.hostname.encode("idna")
    cipher = AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128)
    encrypted = cipher.encrypt(hostname)
    encrypted_hex = binascii.hexlify(iv).decode("ascii") + encrypted.hex()

    scheme_part = parsed.scheme.lower()
    if parsed.port:
        scheme_part += f"-{parsed.port}"
    path = parsed.path or "/"
    result = f"{base_url.rstrip('/')}/{scheme_part}/{encrypted_hex}{path}"
    if parsed.query:
        result += "?" + parsed.query
    return result


def load_cookie_jar(path: Path, webvpn_host: str) -> tuple[requests.cookies.RequestsCookieJar, dict]:
    if not path.exists():
        raise DownloadError(f"Cookie file does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"Cannot read cookie JSON: {path}") from exc

    if isinstance(payload, dict):
        payload = payload.get("cookies", [])
    if not isinstance(payload, list):
        raise DownloadError("Cookie JSON must be a list or an object containing 'cookies'")

    jar = requests.cookies.RequestsCookieJar()
    loaded = 0
    matching = 0
    names: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        domain = str(item.get("domain") or webvpn_host)
        cookie_path = str(item.get("path") or "/")
        jar.set(str(name), str(value), domain=domain, path=cookie_path)
        loaded += 1
        names.add(str(name))
        normalized_domain = domain.lstrip(".").lower()
        normalized_host = webvpn_host.lower()
        if (
            normalized_domain == normalized_host
            or normalized_host.endswith("." + normalized_domain)
        ):
            matching += 1

    if loaded == 0:
        raise DownloadError("Cookie file contains no usable cookies")
    if matching == 0:
        raise DownloadError(
            f"Cookie file has {loaded} cookies, but none match WebVPN host {webvpn_host}"
        )
    return jar, {"loaded": loaded, "matching_host": matching, "names": sorted(names)}


def valid_pdf(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 1000:
            return False
        with path.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                return False
            fh.seek(max(0, size - 8192))
            return b"%%EOF" in fh.read()
    except OSError:
        return False


def has_pdf_hint(value: str) -> bool:
    return any(x in value.lower() for x in PDF_HINTS)


def pdf_like_url(url: str) -> bool:
    return url.startswith(("http://", "https://")) and has_pdf_hint(url)


def infer_publisher(doi: str, resolved_url: str) -> str:
    host = host_of(resolved_url)
    mapping = (
        (("ascelibrary.org",), "ASCE"),
        (("asmedigitalcollection.asme.org",), "ASME"),
        (("pubs.acs.org",), "ACS"),
        (("onlinelibrary.wiley.com",), "Wiley"),
        (("tandfonline.com",), "Taylor & Francis"),
        (("nature.com",), "Nature"),
        (("link.springer.com",), "Springer"),
        (("pubs.rsc.org",), "RSC"),
        (("pnas.org",), "PNAS"),
        (("science.org", "sciencemag.org"), "AAAS"),
        (("sciencedirect.com", "elsevier.com"), "Elsevier"),
        (("ieeexplore.ieee.org",), "IEEE"),
        (("pubs.aip.org", "aip.scitation.org"), "AIP"),
        (("sagepub.com",), "SAGE"),
        (("mdpi.com", "mdpi-res.com"), "MDPI"),
    )
    for hosts, name in mapping:
        if any(x in host for x in hosts):
            return name

    prefix_mapping = (
        ("10.1061/", "ASCE"),
        ("10.1115/", "ASME"),
        ("10.1021/", "ACS"),
        ("10.1111/", "Wiley"),
        ("10.1002/", "Wiley"),
        ("10.1080/", "Taylor & Francis"),
        ("10.1038/", "Nature"),
        ("10.1007/", "Springer"),
        ("10.1039/", "RSC"),
        ("10.1073/", "PNAS"),
        ("10.1126/", "AAAS"),
        ("10.1016/", "Elsevier"),
        ("10.1109/", "IEEE"),
        ("10.1063/", "AIP"),
        ("10.1177/", "SAGE"),
        ("10.3390/", "MDPI"),
    )
    lower = doi.lower()
    for prefix, name in prefix_mapping:
        if lower.startswith(prefix):
            return name
    return host or "unknown"


def publisher_pdf_candidates(doi: str, resolved_url: str) -> list[str]:
    host = host_of(resolved_url)
    suffix = doi.split("/", 1)[1]
    candidates: list[str] = []

    if "ascelibrary.org" in host or doi.lower().startswith("10.1061/"):
        candidates.append(f"https://ascelibrary.org/doi/pdf/{doi}?download=true")
    if "pubs.acs.org" in host or doi.lower().startswith("10.1021/"):
        candidates.append(f"https://pubs.acs.org/doi/pdf/{doi}")
    if "onlinelibrary.wiley.com" in host or doi.lower().startswith(("10.1002/", "10.1111/")):
        candidates.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")
    if "tandfonline.com" in host or doi.lower().startswith("10.1080/"):
        candidates.append(f"https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true")
    if "nature.com" in host or doi.lower().startswith("10.1038/"):
        candidates.append(f"https://www.nature.com/articles/{suffix}.pdf")
    if "link.springer.com" in host or doi.lower().startswith("10.1007/"):
        candidates.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
    if "pubs.rsc.org" in host and "/articlelanding/" in resolved_url:
        candidates.append(resolved_url.replace("/articlelanding/", "/articlepdf/"))
    if "pnas.org" in host or doi.lower().startswith("10.1073/"):
        candidates.append(f"https://www.pnas.org/doi/pdf/{doi}")
    if "science.org" in host or "sciencemag.org" in host or doi.lower().startswith("10.1126/"):
        candidates.append(f"https://www.science.org/doi/pdf/{doi}")
    if "sagepub.com" in host or doi.lower().startswith("10.1177/"):
        candidates.append(f"https://journals.sagepub.com/doi/pdf/{doi}")
    if "mdpi.com" in host or doi.lower().startswith("10.3390/"):
        # DOI resolution supplies the canonical /ISSN/volume/issue/article
        # landing URL. WebVPN can fetch its /pdf route even when direct
        # requests from the local network receive MDPI's HTTP 403 response.
        parsed = urllib.parse.urlsplit(resolved_url)
        if "mdpi.com" in (parsed.hostname or "").casefold() and parsed.path:
            article_path = parsed.path.rstrip("/")
            if not article_path.casefold().endswith("/pdf"):
                candidates.append(
                    urllib.parse.urlunsplit(
                        (parsed.scheme, parsed.netloc, article_path + "/pdf", "", "")
                    )
                )

    if "elsevier.com" in host or "sciencedirect.com" in host or doi.lower().startswith("10.1016/"):
        pii = re.search(r"(?:/pii/|/retrieve/pii/)([A-Z0-9]+)", resolved_url, re.I)
        if pii:
            candidates.append(
                f"https://www.sciencedirect.com/science/article/pii/{pii.group(1)}/pdfft"
            )

    if "ieeexplore.ieee.org" in host:
        article_number = re.search(r"/document/(\d+)", resolved_url)
        if article_number:
            candidates.append(
                "https://ieeexplore.ieee.org/stamp/stamp.jsp"
                f"?tp=&arnumber={article_number.group(1)}"
            )

    return list(dict.fromkeys(candidates))


def is_sitewide_pdf(url: str) -> bool:
    """DOI resolver documentation and known platform manuals are not papers."""
    decoded = urllib.parse.unquote(html.unescape(url)).casefold()
    return (host_of(url) in {"doi.org", "www.doi.org", "doi.foundation", "www.doi.foundation"}
            or any(marker in decoded for marker in (
                "130701trademark", "doi_handbook", "contentplatform_userguide",
                "contentplatform-userguide")))


def relevant_pdf_candidate(url: str, doi: str, publisher: str) -> bool:
    """Reject site-wide help PDFs while retaining article-scoped candidates."""
    decoded = urllib.parse.unquote(html.unescape(url)).casefold()
    if is_sitewide_pdf(url):
        return False
    if publisher != "AIP" and not doi.casefold().startswith("10.1063/"):
        return True
    blocked = (
        "contentplatform_userguide",
        "contentplatform-userguide",
        "content platform user guide",
        "/wp-content/uploads/",
    )
    if any(marker in decoded for marker in blocked):
        return False
    requested = doi.casefold()
    return "/article-pdf/doi/" in decoded and requested in decoded


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pdf_links: list[str] = []
        self.refresh_links: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {str(k).lower(): str(v or "") for k, v in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = self._attrs(attrs)
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content", "").strip()
            if name == "citation_pdf_url" and content:
                self.pdf_links.append(content)
            if values.get("http-equiv", "").lower() == "refresh" and content:
                match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)$", content, re.I)
                if match:
                    self.refresh_links.append(match.group(1).strip().strip("'\""))
        elif tag == "link":
            href = values.get("href", "")
            if href and (
                "pdf" in values.get("type", "").lower()
                or "pdf" in values.get("rel", "").lower()
                or has_pdf_hint(href)
            ):
                self.pdf_links.append(href)
        elif tag in ("iframe", "embed", "object"):
            candidate = values.get("src") or values.get("data") or ""
            if candidate and has_pdf_hint(candidate):
                self.pdf_links.append(candidate)
        elif tag == "a":
            self._anchor_href = values.get("href") or None
            self._anchor_text = []
            attr_hint = " ".join(
                (
                    values.get("class", ""),
                    values.get("title", ""),
                    values.get("aria-label", ""),
                )
            ).lower()
            if self._anchor_href and (
                has_pdf_hint(self._anchor_href)
                or "pdf" in attr_hint
                or "download" in attr_hint
            ):
                self.pdf_links.append(self._anchor_href)

    def handle_data(self, data: str) -> None:
        if self._anchor_href:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._anchor_href:
            return
        text = " ".join(self._anchor_text).strip().lower()
        if pdf_like_url(self._anchor_href) or any(
            hint in text for hint in ("pdf", "download pdf", "full text pdf", "view pdf")
        ):
            self.pdf_links.append(self._anchor_href)
            self._anchor_href = None
            self._anchor_text = []


def discover_mdpi_cdn_urls(html_text: str) -> list[str]:
    """Extract official mdpi-res PDF URLs exposed by a proxied article page."""
    decoded = html.unescape(html_text).replace("\\/", "/")
    candidates = re.findall(
        r"(?:https?:)?//(?:www\.)?mdpi-res\.com/[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?",
        decoded,
        flags=re.I,
    )
    output: list[str] = []
    for candidate in candidates:
        url = "https:" + candidate if candidate.startswith("//") else candidate
        if url not in output:
            output.append(url)
    return output[:12]


def resolve_discovered_link(
    value: str,
    original_url: str,
    response_url: str,
    webvpn_base: str,
) -> str | None:
    value = html.unescape(value.strip()).strip("'\"")
    if not value or value.lower().startswith(("javascript:", "data:", "mailto:")):
        return None
    if value.startswith("//"):
        return "https:" + value
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith(("/http/", "/https/")):
        return urllib.parse.urljoin(webvpn_base.rstrip("/") + "/", value)

    if host_of(original_url) == host_of(webvpn_base):
        base = response_url
    else:
        base = original_url
    return urllib.parse.urljoin(base, value)


@dataclass
class FetchOutcome:
    saved: bool = False
    bytes_written: int = 0
    html_text: str | None = None
    response_url: str = ""
    status: int | None = None
    content_type: str = ""
    detail: str = ""
    human_verification: bool = False
    entitlement_state: str = ""


def html_requires_human_verification(
    status: int | None, response_url: str, html_text: str
) -> bool:
    """Recognize an explicit CAPTCHA/browser challenge returned via a proxy."""
    lower_url = response_url.casefold()
    lower_html = html_text[:256 * 1024].casefold()
    explicit_url = any(
        marker in lower_url
        for marker in ("/captcha", "/cdn-cgi/challenge", "challenge-platform")
    )
    markers = (
        "<title>just a moment",
        "checking your browser",
        "cf-chl-",
        "challenge-platform",
        "captcha",
        "人机验证",
        "滑动验证",
    )
    marker_match = any(marker in lower_html for marker in markers)
    pdf_shaped_html_challenge = has_pdf_hint(response_url) and marker_match
    return explicit_url or pdf_shaped_html_challenge or (
        status in {403, 418, 429, 503} and marker_match
    )


def html_entitlement_state(request_url: str, html_text: str) -> str:
    """Recognize a publisher's explicit purchase wall on its article page."""
    if "ascelibrary.org" not in host_of(request_url):
        return ""
    lower_html = re.sub(r"\s+", " ", html_text.casefold())
    free_signals = (
        "free access" in lower_html
        and "go to purchase options" not in lower_html
    )
    purchase_signals = all(
        marker in lower_html
        for marker in (
            "go to purchase options",
            "get access",
            "already a subscriber?",
        )
    )
    if free_signals:
        return "free"
    if purchase_signals:
        return "not_entitled"
    return ""


def redirected_pdf_entitlement_state(
    request_url: str, response_url: str
) -> str:
    """Recognize publisher PDF requests explicitly returned to an abstract page."""
    request_host = host_of(request_url)
    request_path = urllib.parse.urlsplit(request_url).path.casefold()
    response_lower = response_url.casefold()
    if (
        request_host in {"pubs.aip.org", "aip.scitation.org"}
        and "/article-pdf/" in request_path
        and "/article-abstract/" in response_lower
        and "redirectedfrom=pdf" in response_lower
    ):
        return "not_entitled"
    return ""


@dataclass
class WebVPNClient:
    base_url: str
    key: bytes
    iv: bytes
    cookie_file: Path
    timeout: float = 30.0
    browser_timeout: float | None = None
    delay: float = 2.0
    max_bytes: int = 100 * 1024 * 1024
    verify_tls: bool = True
    elsevier_api_key: str | None = None
    elsevier_insttoken: str | None = None
    session: requests.Session = field(init=False)
    cookie_info: dict = field(init=False)
    _last_request: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise DownloadError("WebVPN base URL must be an https:// URL")
        validate_aes_material(self.key, self.iv)
        jar, info = load_cookie_jar(self.cookie_file, parsed.hostname)
        self.cookie_info = info
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            }
        )
        self.session.cookies.update(jar)

    def proxify(self, url: str) -> str:
        if host_of(url) == host_of(self.base_url):
            return url
        return webvpn_url(url, self.base_url, self.key, self.iv)

    def get(
        self,
        url: str,
        *,
        stream: bool = True,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        proxied = self.proxify(url)
        response = self.session.get(
            proxied,
            timeout=self.timeout,
            allow_redirects=True,
            stream=stream,
            verify=self.verify_tls,
            headers=headers,
        )
        self._last_request = time.monotonic()
        return response

    def fetch(
        self,
        url: str,
        output_path: Path,
        *,
        overwrite: bool,
        headers: dict[str, str] | None = None,
    ) -> FetchOutcome:
        response: requests.Response | None = None
        try:
            response = self.get(url, stream=True, headers=headers)
            outcome = FetchOutcome(
                response_url=response.url,
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
            )
            final_lower = response.url.lower()
            if any(
                marker in final_lower
                for marker in (
                    "/authserver/login",
                    "/cas/login",
                    "/sso/login",
                    "/users/sign_in",
                )
            ):
                outcome.detail = (
                    "WebVPN login expired; redirected to the institutional login page"
                )
                return outcome
            if response.status_code >= 400:
                preview = bytearray()
                preview_limit = 256 * 1024
                for chunk in response.iter_content(chunk_size=32 * 1024):
                    if not chunk:
                        continue
                    remaining = preview_limit - len(preview)
                    if remaining <= 0:
                        break
                    preview.extend(chunk[:remaining])
                encoding = response.encoding or "utf-8"
                outcome.html_text = bytes(preview).decode(encoding, errors="replace")
                outcome.human_verification = html_requires_human_verification(
                    response.status_code,
                    response.url,
                    outcome.html_text,
                )
                outcome.detail = f"HTTP {response.status_code}"
                if outcome.human_verification:
                    outcome.detail += " (browser human verification required)"
                return outcome

            iterator = response.iter_content(chunk_size=64 * 1024)
            first = next(iterator, b"")
            if first.startswith(b"%PDF-"):
                if output_path.exists() and not overwrite:
                    raise DownloadError(f"Output already exists: {output_path}")
                output_path.parent.mkdir(parents=True, exist_ok=True)
                part = output_path.with_suffix(output_path.suffix + ".part")
                total = 0
                try:
                    with part.open("wb") as fh:
                        fh.write(first)
                        total += len(first)
                        for chunk in iterator:
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > self.max_bytes:
                                raise DownloadError(
                                    f"PDF exceeds size limit ({self.max_bytes // 1024 // 1024} MiB)"
                                )
                            fh.write(chunk)
                    os.replace(part, output_path)
                except Exception:
                    part.unlink(missing_ok=True)
                    raise
                if not valid_pdf(output_path):
                    output_path.unlink(missing_ok=True)
                    raise DownloadError("Response began with %PDF but failed EOF/size validation")
                outcome.saved = True
                outcome.bytes_written = total
                outcome.detail = "validated PDF"
                return outcome

            collected = bytearray(first)
            html_limit = min(self.max_bytes, 2 * 1024 * 1024)
            for chunk in iterator:
                if not chunk:
                    continue
                remaining = html_limit - len(collected)
                if remaining <= 0:
                    break
                collected.extend(chunk[:remaining])
            encoding = response.encoding or "utf-8"
            outcome.html_text = bytes(collected).decode(encoding, errors="replace")
            outcome.human_verification = html_requires_human_verification(
                response.status_code,
                response.url,
                outcome.html_text,
            )
            outcome.detail = "HTML/non-PDF response"
            outcome.entitlement_state = html_entitlement_state(
                url, outcome.html_text
            )
            if not outcome.entitlement_state:
                outcome.entitlement_state = redirected_pdf_entitlement_state(
                    url, response.url
                )
            if outcome.human_verification:
                outcome.detail += " (browser human verification required)"
            return outcome
        except requests.RequestException as exc:
            return FetchOutcome(detail=f"{type(exc).__name__}: {exc}")
        finally:
            if response is not None:
                response.close()


class ElsevierWebVPNSession:
    """Expose WebVPNClient as the requests-like transport used by Elsevier."""

    def __init__(self, client: WebVPNClient) -> None:
        self.client = client

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        proxies: dict[str, str | None] | None = None,
        timeout: int | float | None = None,
        allow_redirects: bool = True,
        stream: bool = False,
    ) -> requests.Response:
        # WebVPNClient owns routing, cookies, throttling, TLS and redirect policy.
        # The extra arguments are accepted for requests.Session compatibility.
        del proxies, timeout, allow_redirects
        return self.client.get(url, stream=stream, headers=headers)


def download_elsevier_via_webvpn(
    doi: str,
    output_path: Path,
    client: WebVPNClient,
    *,
    overwrite: bool,
    config: dict[str, Any] | None = None,
) -> tuple[dict | None, dict]:
    """Run the complete Elsevier PDF/XML/object/reconstruction cascade."""
    if not client.elsevier_api_key:
        return None, {
            "label": "Elsevier official full-text API via WebVPN",
            "status": None,
            "detail": "Elsevier API key is not configured",
        }
    if output_path.exists() and not overwrite:
        raise DownloadError(f"Output already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    transport = ElsevierWebVPNSession(client)
    try:
        chromium = elsevier_fulltext.find_chromium()
    except RuntimeError:
        chromium = None
    with tempfile.TemporaryDirectory(
        prefix="elsevier-webvpn-",
        dir=str(output_path.parent),
    ) as temporary:
        elsevier_options = (config or {}).get("elsevier") or {}
        result = elsevier_fulltext.test_doi(
            transport,  # type: ignore[arg-type]
            doi,
            client.elsevier_api_key,
            client.elsevier_insttoken or "",
            {},
            max(1, int(client.timeout)),
            Path(temporary),
            True,
            chromium,
            use_aam=bool(elsevier_options.get("use_aam", False)),
            use_xml_reconstruction=bool(
                elsevier_options.get("use_xml_reconstruction", False)
            ),
            # Keep institutional gateway traffic serialized.  Direct Elsevier
            # API downloads use the configurable parallel figure workers.
            figure_download_workers=1,
        )
        saved = result.get("saved") or {}
        selected = saved.get("fulltext_pdf")
        classification = str(result.get("classification") or "")
        pdf_result = result.get("pdf") or {}
        xml_result = result.get("xml") or {}
        object_result = result.get("xml_pdf_object") or {}
        attempt = {
            "label": "Elsevier full-text cascade via WebVPN",
            "target_host": "api.elsevier.com",
            "target_url": "https://api.elsevier.com/content/article/doi/" + urllib.parse.quote(doi, safe="/"),
            "status": pdf_result.get("status_code"),
            "content_type": pdf_result.get("content_type", ""),
            "detail": classification or "full text not confirmed",
            "pdf_pages": pdf_result.get("pages"),
            "xml_status": xml_result.get("status_code"),
            "xml_fulltext": bool(xml_result.get("fulltext_detected")),
            "object_status": object_result.get("status"),
        }
        if not selected:
            return None, attempt
        shutil.copy2(selected, output_path)
        if not valid_pdf(output_path):
            output_path.unlink(missing_ok=True)
            attempt["detail"] = "cascade produced an invalid PDF"
            return None, attempt
        page_count = elsevier_fulltext.pdf_info(output_path.read_bytes()).get("pages")
        if isinstance(page_count, int) and page_count <= 1:
            output_path.unlink(missing_ok=True)
            attempt["detail"] = "Elsevier returned only a one-page entitlement preview"
            return None, attempt
        method_labels = {
            "publisher_pdf": "Elsevier publisher PDF via WebVPN",
            "aam_pdf": "Elsevier accepted-manuscript PDF via WebVPN",
            "xml_reconstructed_pdf": "Elsevier FULL XML reconstruction via WebVPN",
        }
        return {
            "method": method_labels.get(classification, "Elsevier full-text cascade via WebVPN"),
            "classification": classification,
            "path": str(output_path.resolve()),
            "bytes": output_path.stat().st_size,
            "pages": page_count,
        }, attempt


def resolve_doi(doi: str, timeout: float, verify_tls: bool) -> tuple[str, str]:
    url = f"https://doi.org/{doi}"
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            allow_redirects=True,
            stream=True,
            verify=verify_tls,
        )
        final = response.url or url
        status = f"HTTP {response.status_code}"
        response.close()
        return final, status
    except requests.RequestException as exc:
        return url, f"{type(exc).__name__}: {exc}"
    finally:
        session.close()


def attempt_summary(
    url: str, outcome: FetchOutcome, label: str, *, elapsed_s: float | None = None
) -> dict:
    response_lower = outcome.response_url.casefold()
    entitlement_state = outcome.entitlement_state or redirected_pdf_entitlement_state(
        url, outcome.response_url
    )
    return {
        "label": label,
        "target_host": host_of(url),
        "target_url": url,
        "response_url": outcome.response_url,
        "status": outcome.status,
        "content_type": outcome.content_type.split(";", 1)[0],
        "detail": outcome.detail,
        "redirected_from_pdf": "redirectedfrom=pdf" in response_lower,
        "human_verification": outcome.human_verification,
        "entitlement_state": entitlement_state,
        "elapsed_s": round(elapsed_s, 3) if elapsed_s is not None else None,
    }


def elsevier_content_error(text: str) -> bool:
    # The error may arrive with HTTP 200 or 403; status alone is insufficient.
    visible = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    visible = re.sub(r"\s+", " ", visible)
    return bool(re.search(r"\bCPE00001\b|there was a problem providing the content you requested", visible, re.I))


def download_one(
    doi: str,
    output_path: Path,
    client: WebVPNClient,
    *,
    overwrite: bool,
    max_steps: int,
    config: dict[str, Any] | None = None,
) -> dict:
    started = time.monotonic()
    if output_path.exists() and valid_pdf(output_path) and not overwrite:
        return {
            "ok": True,
            "doi": doi,
            "publisher": "cache",
            "method": "existing validated PDF",
            "path": str(output_path.resolve()),
            "bytes": output_path.stat().st_size,
            "elapsed_s": 0.0,
            "attempts": [],
        }

    config = config or {}
    publisher = infer_publisher(doi, f"https://doi.org/{doi}")
    attempts: list[dict] = []
    mdpi_candidates: list[dict[str, Any]] = []
    mdpi_resolution: dict[str, Any] = {}
    if publisher == "MDPI":
        crossref, crossref_error, crossref_attempt = mdpi_resolver.fetch_crossref_record(
            doi,
            timeout=min(client.timeout, 30),
            contact_email=str(config.get("contact_email") or "").strip(),
        )
        attempts.append(crossref_attempt)
        known_urls = [
            str(item.get("URL") or "")
            for item in crossref.get("link") or []
            if isinstance(item, dict)
        ]
        mdpi_parts = mdpi_resolver.bibliographic_parts(crossref, known_urls)
        mdpi_issn = str(mdpi_parts.get("issn") or "")
        source_slugs: list[tuple[str, str]] = []
        source_error = ""
        if mdpi_issn:
            source_slugs, source_error, source_attempt = (
                mdpi_resolver.fetch_openalex_source_slugs(
                    mdpi_issn,
                    timeout=min(client.timeout, 20),
                    contact_email=str(config.get("contact_email") or "").strip(),
                    api_key=str((config.get("api_keys") or {}).get("openalex") or "").strip(),
                )
            )
            attempts.append(source_attempt)
        mdpi_candidates, mdpi_resolution = mdpi_resolver.official_candidate_urls(
            doi,
            crossref,
            config,
            known_urls=known_urls,
            extra_slugs=source_slugs,
        )
        mdpi_resolution["crossref_error"] = crossref_error
        mdpi_resolution["openalex_source_error"] = source_error
        canonical_pdf = next((url for url in known_urls if "mdpi.com/" in url), "")
        resolved_url = re.sub(r"/pdf/?$", "", canonical_pdf) if canonical_pdf else f"https://doi.org/{doi}"
        resolution = "Crossref canonical URL" if canonical_pdf else crossref_error
    else:
        resolved_url, resolution = resolve_doi(doi, client.timeout, client.verify_tls)
        publisher = infer_publisher(doi, resolved_url)
    direct = [str(item["url"]) for item in mdpi_candidates]
    direct.extend(publisher_pdf_candidates(doi, resolved_url))
    mdpi_by_url = {str(item["url"]): item for item in mdpi_candidates}

    queue: deque[tuple[str, str]] = deque()
    for candidate in direct:
        label = "MDPI official CDN candidate" if candidate in mdpi_by_url else "publisher PDF rule"
        queue.append((candidate, label))
    if resolved_url != f"https://doi.org/{doi}":
        queue.append((resolved_url, "publisher landing page"))
    queue.append((f"https://doi.org/{doi}", "DOI via WebVPN"))

    visited: set[str] = set()
    elsevier_browser_attempted = False
    steps = 0
    while queue and steps < max_steps:
        target, label = queue.popleft()
        target = target.strip()
        if not target.startswith(("http://", "https://")) or target in visited:
            continue
        visited.add(target)
        steps += 1
        LOG.info("[%s] %s: %s", doi, label, host_of(target))
        fetch_started = time.monotonic()
        if (publisher == "Elsevier" and not elsevier_browser_attempted
                and "/pdfft" in urllib.parse.urlsplit(target).path.casefold()):
            # The generated candidate identifies the article only. Let its
            # browser page generate the authenticated PDF request by clicking.
            outcome = FetchOutcome(content_type="text/html",
                response_url=client.proxify(resolved_url),
                detail="打开文章页并点击实际 PDF 入口；未请求拼接的 PDF 地址")
        else:
            outcome = client.fetch(target, output_path, overwrite=overwrite)
        attempts.append(
            attempt_summary(
                target,
                outcome,
                label,
                elapsed_s=time.monotonic() - fetch_started,
            )
        )

        if publisher == "Elsevier" and elsevier_content_error(outcome.html_text):
            return {"ok": False, "doi": doi, "publisher": publisher,
                    "error_type": "elsevier_content_error",
                    "reason": "Elsevier 返回 CPE00001/内容提供错误页，立即结束当前访问路线",
                    "attempts": attempts}

        if outcome.detail.startswith("WebVPN login expired"):
            return {
                "ok": False,
                "doi": doi,
                "publisher": publisher,
                "error_type": "session_expired",
                "reason": outcome.detail,
                "elapsed_s": round(time.monotonic() - started, 2),
                "resolved_host": host_of(resolved_url),
                "doi_resolution": resolution,
                "attempts": attempts,
            }

        if outcome.saved:
            if target in mdpi_by_url:
                mdpi_resolver.remember_success(config, mdpi_by_url[target])
            return {
                "ok": True,
                "doi": doi,
                "publisher": publisher,
                "method": "WebVPN HTTP",
                "path": str(output_path.resolve()),
                "bytes": outcome.bytes_written,
                "elapsed_s": round(time.monotonic() - started, 2),
                "resolved_host": host_of(resolved_url),
                "doi_resolution": resolution,
                "mdpi_resolution": mdpi_resolution,
                "attempts": attempts,
            }

        if (
            publisher == "Elsevier"
            and not elsevier_browser_attempted
            and "/pdfft" in urllib.parse.urlsplit(target).path.casefold()
            and (
                outcome.human_verification
                or outcome.content_type.casefold().startswith("text/html")
            )
        ):
            # This is an institution-source operation, not an Elsevier API
            # request.  Chrome executes ScienceDirect's HTTP-200 JavaScript
            # challenge and we accept only the real PDF response it receives.
            elsevier_browser_attempted = True
            browser_started = time.monotonic()
            try:
                from .elsevier_institution_browser import download_elsevier_pdf

                browser_result = download_elsevier_pdf(
                    landing_url=client.proxify(resolved_url),
                    pdf_url=client.proxify(target),
                    cookies=client.session.cookies,
                    direct_network=getattr(client, "access_type", "") == "direct",
                    output_path=output_path,
                    overwrite=overwrite,
                    timeout=max(
                        12.0,
                        float(client.browser_timeout or client.timeout),
                    ),
                    max_bytes=client.max_bytes,
                )
            except Exception as exc:
                browser_result = {
                    "ok": False,
                    "error_type": "institution_browser_exception",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            attempts.append(
                {
                    "label": "ScienceDirect institution browser",
                    "target_host": host_of(client.base_url),
                    "status": 200 if browser_result.get("ok") else None,
                    "content_type": (
                        "application/pdf" if browser_result.get("ok") else ""
                    ),
                    "bytes": browser_result.get("bytes", 0),
                    "detail": browser_result.get("reason")
                    or browser_result.get("method")
                    or "",
                    "elapsed_s": round(time.monotonic() - browser_started, 3),
                    "dynamic_cdn": bool(browser_result.get("dynamic_cdn")),
                    "fetch_response_count": browser_result.get(
                        "fetch_response_count", 0
                    ),
                    "fetch_status": browser_result.get("fetch_status", 0),
                    "fetch_content_type": browser_result.get(
                        "fetch_content_type", ""
                    ),
                    "fetch_content_length": browser_result.get(
                        "fetch_content_length", ""
                    ),
                    "fetch_bytes": browser_result.get("fetch_bytes", 0),
                    "fetch_stream_complete": browser_result.get(
                        "fetch_stream_complete", False
                    ),
                    "fetch_body_complete": browser_result.get(
                        "fetch_body_complete", False
                    ),
                    "navigation_retry_count": browser_result.get(
                        "navigation_retry_count", 0
                    ),
                    "fetch_signature_valid": browser_result.get(
                        "fetch_signature_valid", False
                    ),
                    "fetch_eof_valid": browser_result.get(
                        "fetch_eof_valid", False
                    ),
                    "pdf_viewer_count": browser_result.get("pdf_viewer_count", 0),
                    "capture_method": browser_result.get("capture_method", ""),
                    "error_type": browser_result.get("error_type"),
                }
            )
            if browser_result.get("error_type") in {"elsevier_content_error", "session_expired"}:
                return {"ok": False, "doi": doi, "publisher": publisher,
                        "error_type": browser_result.get("error_type"),
                        "reason": browser_result.get("reason"), "attempts": attempts}
            if browser_result.get("ok"):
                return {
                    "ok": True,
                    "doi": doi,
                    "publisher": publisher,
                    "method": browser_result.get("method"),
                    "path": browser_result.get("path"),
                    "bytes": browser_result.get("bytes"),
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "resolved_host": host_of(resolved_url),
                    "institution_pdf_host": browser_result.get("resolved_host"),
                    "dynamic_cdn": bool(browser_result.get("dynamic_cdn")),
                    "doi_resolution": resolution,
                    "attempts": attempts,
                }

        if not outcome.html_text:
            continue
        parser = LinkParser()
        try:
            parser.feed(outcome.html_text)
        except Exception:
            continue

        refresh_targets: list[str] = []
        for raw in parser.refresh_links:
            link = resolve_discovered_link(
                raw, target, outcome.response_url or target, client.base_url
            )
            if link:
                refresh_targets.append(link)
        pdf_targets: list[str] = []
        for raw in parser.pdf_links:
            link = resolve_discovered_link(
                raw, target, outcome.response_url or target, client.base_url
            )
            if (
                link
                and pdf_like_url(link)
                and relevant_pdf_candidate(link, doi, publisher)
            ):
                pdf_targets.append(link)
        if publisher == "MDPI":
            # The WebVPN-fetched HTML may expose the exact deployment asset.
            # Prefer those official CDN URLs over guessing a journal slug.
            pdf_targets.extend(discover_mdpi_cdn_urls(outcome.html_text))

        # PDF links take priority; meta-refresh is still followed without a browser.
        for link in reversed(list(dict.fromkeys(refresh_targets))):
            queue.appendleft((link, "HTML meta refresh"))
        for link in reversed(list(dict.fromkeys(pdf_targets))):
            queue.appendleft((link, "PDF link discovered in HTML"))

    reason = "学校机构订阅未取得可验证的 PDF"
    error_type = None
    browser_failure = next(
        (
            attempt
            for attempt in reversed(attempts)
            if attempt.get("label") == "ScienceDirect institution browser"
        ),
        None,
    )
    if any(a.get("entitlement_state") == "not_entitled" for a in attempts):
        error_type = "institution_not_entitled"
        if publisher == "AIP":
            reason = (
                "当前学校会话可打开 AIP 摘要页，但 PDF 请求被重定向回摘要页；"
                "该文不在当前机构订阅下载权限内"
            )
        elif publisher == "ASCE":
            reason = (
                "当前学校会话已到达 ASCE 文章页，但页面明确显示 "
                "Get Access/购买入口；该文不在当前机构订阅权限内"
            )
        else:
            reason = "出版社将 PDF 请求退回摘要/购买页面；该文不在当前机构订阅下载权限内"
    elif browser_failure and browser_failure.get("error_type"):
        error_type = str(browser_failure["error_type"])
        reason = str(browser_failure.get("detail") or reason)
    elif any(a.get("human_verification") for a in attempts):
        error_type = "institution_human_verification_required"
        reason = "出版社要求浏览器人机验证，学校机构下载未取得有效 PDF"
    elif any(a.get("status") in (401, 403) for a in attempts):
        error_type = "institution_http_access_denied"
        reason = "学校机构请求返回 401/403，未取得全文 PDF"
    return {
        "ok": False,
        "doi": doi,
        "publisher": publisher,
        "error_type": error_type,
        "reason": reason,
        "elapsed_s": round(time.monotonic() - started, 2),
        "resolved_host": host_of(resolved_url),
        "doi_resolution": resolution,
        "mdpi_resolution": mdpi_resolution,
        "attempts": attempts,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "HTTP-only subscription PDF downloader for authenticated WEngine WebVPN sessions."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("doi", nargs="+", help="one or more DOI values")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("downloads"), help="directory for PDFs"
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=DEFAULT_COOKIE_FILE,
        help="standard browser cookie JSON",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="school WebVPN base URL")
    parser.add_argument("--key", default=DEFAULT_KEY, help="WEngine AES key text")
    parser.add_argument("--iv", default=DEFAULT_KEY, help="WEngine AES-CFB IV text")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout in seconds")
    parser.add_argument("--delay", type=float, default=2.0, help="minimum delay between requests")
    parser.add_argument("--max-mb", type=int, default=100, help="maximum accepted response size")
    parser.add_argument("--max-steps", type=int, default=12, help="maximum URL attempts per DOI")
    parser.add_argument("--overwrite", action="store_true", help="replace existing output PDFs")
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS certificate verification (not recommended)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON to stdout")
    parser.add_argument("--debug", action="store_true", help="enable diagnostic logs on stderr")
    parser.add_argument(
        "--elsevier-api-key",
        default=os.environ.get("ELS_API_KEY"),
        help="Elsevier API key (prefer ELS_API_KEY environment variable)",
    )
    parser.add_argument(
        "--elsevier-insttoken",
        default=os.environ.get("ELS_INSTTOKEN"),
        help="optional Elsevier institutional token (prefer ELS_INSTTOKEN)",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.timeout <= 0 or args.delay < 0 or args.max_mb <= 0 or args.max_steps <= 0:
        raise SystemExit("timeout/max-mb/max-steps must be positive; delay must be non-negative")

    try:
        dois = [normalize_doi(value) for value in args.doi]
        client = WebVPNClient(
            base_url=args.base_url.rstrip("/"),
            key=args.key.encode("utf-8"),
            iv=args.iv.encode("utf-8"),
            cookie_file=args.cookie_file.expanduser(),
            timeout=args.timeout,
            delay=args.delay,
            max_bytes=args.max_mb * 1024 * 1024,
            verify_tls=not args.insecure,
            elsevier_api_key=args.elsevier_api_key,
            elsevier_insttoken=args.elsevier_insttoken,
        )
    except (ValueError, DownloadError) as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    LOG.info(
        "Loaded %s cookies (%s for %s); cookie values are not logged",
        client.cookie_info["loaded"],
        client.cookie_info["matching_host"],
        host_of(client.base_url),
    )
    results: list[dict] = []
    for doi in dois:
        output_path = args.output_dir.expanduser() / doi_filename(doi)
        try:
            result = download_one(
                doi,
                output_path,
                client,
                overwrite=args.overwrite,
                max_steps=args.max_steps,
            )
        except (DownloadError, OSError, ValueError) as exc:
            result = {"ok": False, "doi": doi, "reason": str(exc), "attempts": []}
        results.append(result)
        if not args.json:
            if result["ok"]:
                print(
                    f"OK {doi} -> {result['path']} "
                    f"({result['bytes'] / 1024:.1f} KiB, {result['elapsed_s']:.2f}s)"
                )
            else:
                print(f"FAIL {doi}: {result.get('reason', 'unknown error')}")

    payload = {
        "ok": all(item.get("ok") for item in results),
        "webvpn": host_of(client.base_url),
        "browser_used": False,
        "grey_sources_used": False,
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
