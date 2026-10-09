"""Metadata-driven resolver for MDPI's public PDF asset URLs.

The CDN layout is not a documented API, so every generated URL must still be
validated as a PDF.  Bibliographic numbers come from Crossref (or a canonical
MDPI URL), never from fixed-width slicing of the DOI suffix.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

try:
    from .app_paths import relocate_legacy_path, state_dir
except ImportError:  # pragma: no cover - direct script execution
    from app_paths import relocate_legacy_path, state_dir  # type: ignore


DEFAULT_CACHE_FILE = state_dir() / "mdpi_journal_cache.json"
OFFICIAL_CDN_HOSTS = ("mdpi-res.com", "res.mdpi.com")
DEFAULT_SUFFIXES = ("", "-v2", "-v3", "-v1", "-v4", "-v5")


def is_mdpi_doi(doi: str) -> bool:
    return bool(re.fullmatch(r"10\.3390/[a-z0-9._-]+", doi, re.I))


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        return next((str(item).strip() for item in value if str(item).strip()), "")
    return str(value or "").strip()


def normalize_slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", ascii_text.casefold())


def _positive_number(value: Any) -> int | None:
    match = re.search(r"\d+", _first_text(value))
    if not match:
        return None
    number = int(match.group())
    return number if number >= 0 else None


def _doi_code(doi: str) -> str:
    match = re.match(r"10\.3390/([a-z]+)", doi, re.I)
    return match.group(1).casefold() if match else ""


def _canonical_mdpi_parts(urls: Iterable[str]) -> dict[str, Any]:
    for url in urls:
        parsed = urllib.parse.urlsplit(str(url or ""))
        if not (parsed.hostname or "").casefold().endswith("mdpi.com"):
            continue
        match = re.search(
            r"/(\d{4}-\d{3}[\dXx]?)/(\d+)/(\d+)/(\d+)(?:/|$)",
            parsed.path,
        )
        if match:
            issn, volume, issue, article = match.groups()
            return {
                "issn": issn.upper(),
                "volume": int(volume),
                "issue": int(issue),
                "article": int(article),
                "metadata_source": "canonical_mdpi_url",
            }
    return {}


def bibliographic_parts(
    record: dict[str, Any], known_urls: Iterable[str] = ()
) -> dict[str, Any]:
    link_urls = [
        str(item.get("URL") or "")
        for item in record.get("link") or []
        if isinstance(item, dict)
    ]
    canonical = _canonical_mdpi_parts([*known_urls, *link_urls])
    issn = _first_text(record.get("ISSN")) or str(canonical.get("issn") or "")
    volume = _positive_number(record.get("volume"))
    issue = _positive_number(record.get("issue"))
    article = _positive_number(record.get("article-number"))
    if article is None:
        article = _positive_number(record.get("page"))
    return {
        "issn": issn.upper(),
        "volume": volume if volume is not None else canonical.get("volume"),
        "issue": issue if issue is not None else canonical.get("issue"),
        "article": article if article is not None else canonical.get("article"),
        "metadata_source": "crossref" if record else canonical.get("metadata_source", ""),
    }


def _mdpi_settings(config: dict[str, Any]) -> dict[str, Any]:
    return dict(config.get("mdpi") or {})


def cache_path(config: dict[str, Any]) -> Path:
    configured = relocate_legacy_path(_mdpi_settings(config).get("cache_file") or "")
    return Path(configured).expanduser() if configured else DEFAULT_CACHE_FILE


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "journals": {}}
    if not isinstance(value, dict) or not isinstance(value.get("journals"), dict):
        return {"version": 1, "journals": {}}
    return value


def _configured_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def slug_candidates(
    doi: str,
    record: dict[str, Any],
    issn: str,
    config: dict[str, Any],
    extra_slugs: Iterable[tuple[str, str]] = (),
) -> list[tuple[str, str]]:
    settings = _mdpi_settings(config)
    overrides = settings.get("slug_overrides") or {}
    cache = _load_cache(cache_path(config))
    code = _doi_code(doi)
    values: list[tuple[str, str]] = []
    if isinstance(overrides, dict):
        for key in (issn, issn.casefold(), code):
            if key and key in overrides:
                values.extend(
                    (item, "config_override") for item in _configured_values(overrides[key])
                )
    cached = (cache.get("journals") or {}).get(issn) if issn else None
    if isinstance(cached, dict) and cached.get("slug"):
        values.append((str(cached["slug"]), "issn_cache"))
    values.extend(extra_slugs)
    if code:
        values.append((code, "doi_code"))
    for field, source in (
        ("short-container-title", "crossref_short_title"),
        ("container-title", "crossref_title"),
    ):
        raw = record.get(field) or []
        for item in raw if isinstance(raw, list) else [raw]:
            values.append((str(item), source))

    limit = max(1, min(12, int(settings.get("max_slug_candidates") or 6)))
    output: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw, source in values:
        slug = normalize_slug(raw)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        output.append((slug, source))
        if len(output) >= limit:
            break
    return output


def _cdn_hosts(config: dict[str, Any]) -> list[str]:
    configured = _configured_values(_mdpi_settings(config).get("cdn_hosts"))
    hosts = configured or [OFFICIAL_CDN_HOSTS[0], OFFICIAL_CDN_HOSTS[1]]
    return list(
        dict.fromkeys(
            host.casefold()
            for host in hosts
            if host.casefold() in OFFICIAL_CDN_HOSTS
        )
    )


def _version_suffixes(config: dict[str, Any]) -> list[str]:
    configured = _configured_values(_mdpi_settings(config).get("version_suffixes"))
    values = configured or list(DEFAULT_SUFFIXES)
    return list(
        dict.fromkeys(value for value in values if re.fullmatch(r"(?:-v\d+)?", value))
    )


def official_candidate_urls(
    doi: str,
    record: dict[str, Any],
    config: dict[str, Any],
    *,
    known_urls: Iterable[str] = (),
    extra_slugs: Iterable[tuple[str, str]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not is_mdpi_doi(doi):
        return [], {}
    parts = bibliographic_parts(record, known_urls)
    issn = str(parts.get("issn") or "")
    volume = parts.get("volume")
    article = parts.get("article")
    slugs = slug_candidates(doi, record, issn, config, extra_slugs)
    diagnostics = {
        **parts,
        "slug_candidates": [
            {"slug": slug, "source": source} for slug, source in slugs
        ],
        "cache_file": str(cache_path(config)),
    }
    if volume is None or article is None or not slugs:
        diagnostics["reason"] = "missing volume/article/slug metadata"
        return [], diagnostics

    hosts = _cdn_hosts(config)
    suffixes = _version_suffixes(config)
    candidates: list[dict[str, Any]] = []
    # Interleave slugs before trying another version.  A title-derived slug can
    # therefore succeed quickly when the DOI code is an abbreviation.
    for host in hosts:
        for suffix in suffixes:
            for slug, slug_source in slugs:
                stem = f"{slug}-{int(volume):02d}-{int(article):05d}"
                candidates.append(
                    {
                        "url": (
                            f"https://{host}/d_attachment/{slug}/{stem}/"
                            f"article_deploy/{stem}{suffix}.pdf"
                        ),
                        "host": host,
                        "license": "publisher-open-access",
                        "version": "publishedVersion",
                        "source": "mdpi_oa",
                        "mdpi_slug": slug,
                        "mdpi_slug_source": slug_source,
                        "mdpi_issn": issn,
                        "mdpi_volume": int(volume),
                        "mdpi_article": int(article),
                    }
                )
    diagnostics["candidate_count"] = len(candidates)
    return candidates, diagnostics


def slug_from_mdpi_homepage(url: str) -> str:
    parsed = urllib.parse.urlsplit(str(url or ""))
    if not (parsed.hostname or "").casefold().endswith("mdpi.com"):
        return ""
    match = re.search(r"/journal/([^/?#]+)", parsed.path, re.I)
    return normalize_slug(urllib.parse.unquote(match.group(1))) if match else ""


def fetch_openalex_source_slugs(
    issn: str,
    *,
    timeout: float,
    contact_email: str = "",
    api_key: str = "",
    session: requests.Session | None = None,
) -> tuple[list[tuple[str, str]], str, dict[str, Any]]:
    endpoint = "https://api.openalex.org/sources/issn:" + urllib.parse.quote(issn, safe="-")
    params: dict[str, str] = {}
    if contact_email:
        params["mailto"] = contact_email
    if api_key:
        params["api_key"] = api_key
    owned = session is None
    requester = session or requests.Session()
    if owned:
        requester.trust_env = False
    try:
        response = requester.get(
            endpoint,
            params=params,
            headers={"Accept": "application/json", "User-Agent": "literature-downloader/1.7"},
            timeout=timeout,
        )
        attempt = {
            "stage": "journal_metadata",
            "provider": "openalex_source",
            "url": endpoint,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
            "bytes": len(response.content),
        }
        if response.status_code != 200:
            return [], f"OpenAlex Source HTTP {response.status_code}", attempt
        record = response.json()
        slug = slug_from_mdpi_homepage(str(record.get("homepage_url") or ""))
        if not slug:
            return [], "OpenAlex Source 未提供 MDPI 期刊主页 slug", attempt
        return [(slug, "openalex_source_homepage")], "", attempt
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return (
            [],
            f"OpenAlex Source 请求失败：{type(exc).__name__}: {exc}",
            {
                "stage": "journal_metadata",
                "provider": "openalex_source",
                "url": endpoint,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )
    finally:
        if owned:
            requester.close()


def remember_success(config: dict[str, Any], candidate: dict[str, Any]) -> None:
    issn = str(candidate.get("mdpi_issn") or "").strip().upper()
    slug = normalize_slug(str(candidate.get("mdpi_slug") or ""))
    if not issn or not slug:
        return
    path = cache_path(config)
    temporary: Path | None = None
    try:
        cache = _load_cache(path)
        journals = cache.setdefault("journals", {})
        existing = journals.get(issn)
        if isinstance(existing, dict) and existing.get("slug") == slug:
            return
        journals[issn] = {
            "slug": slug,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "source": str(candidate.get("mdpi_slug_source") or "validated_pdf"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError:
        # A read-only or temporarily locked cache must never turn a valid PDF
        # download into a failure.
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def fetch_crossref_record(
    doi: str,
    *,
    timeout: float,
    contact_email: str = "",
    session: requests.Session | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    endpoint = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    agent = "literature-downloader/1.7"
    if contact_email:
        agent += f" (mailto:{contact_email})"
    owned = session is None
    requester = session or requests.Session()
    if owned:
        requester.trust_env = False
    try:
        response = requester.get(
            endpoint,
            headers={"Accept": "application/json", "User-Agent": agent},
            timeout=timeout,
        )
        attempt = {
            "stage": "metadata",
            "provider": "crossref",
            "url": endpoint,
            "status": response.status_code,
            "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
            "bytes": len(response.content),
        }
        if response.status_code != 200:
            return {}, f"Crossref 元数据 HTTP {response.status_code}", attempt
        message = response.json().get("message")
        if not isinstance(message, dict):
            return {}, "Crossref 返回中没有有效的 message 对象", attempt
        return message, "", attempt
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return (
            {},
            f"Crossref 请求失败：{type(exc).__name__}: {exc}",
            {
                "stage": "metadata",
                "provider": "crossref",
                "url": endpoint,
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )
    finally:
        if owned:
            requester.close()
