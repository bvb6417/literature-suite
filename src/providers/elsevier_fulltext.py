#!/usr/bin/env python3
"""Retrieve Elsevier full text via PDF, XML PDF objects, or XML reconstruction."""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CONFIG = SKILL_DIR / "config.json"
DEFAULT_PROXY: dict[str, str | None] = {
    "http": None,
    "https": None,
    "all": None,
}


def session(proxy: dict[str, str | None]) -> requests.Session:
    """Create a requests session configured with the caller-provided proxy map."""
    client = requests.Session()
    # Ignore HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (for example v2rayN). A real
    # campus VPN still works because its virtual adapter is part of Windows'
    # normal routing table rather than requests' proxy environment.
    client.trust_env = False
    client.proxies.update(proxy)
    return client


def clean_doi(value: str) -> str:
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value.strip(), flags=re.I)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    return value.rstrip(".,;").casefold()


def safe_name(doi: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", doi).strip("._") or "article"


def load_credentials(config_path: Path) -> tuple[str, str]:
    data = json.loads(config_path.read_text(encoding="utf-8"))
    keys = data.get("api_keys") or {}
    api_key = str(keys.get("elsevier") or "").strip()
    inst_token = str(keys.get("elsevier_inst_token") or "").strip()
    if not api_key:
        raise RuntimeError(f"Elsevier API key is not configured in {config_path}")
    return api_key, inst_token


def pdf_info(content: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "signature_valid": content.startswith(b"%PDF-"),
        "bytes": len(content),
        "pages": None,
        "text_chars": None,
        "parse_error": None,
    }
    if not result["signature_valid"]:
        return result
    try:
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(content))
        texts: list[str] = []
        for page in reader.pages:
            try:
                texts.append(page.extract_text() or "")
            except Exception:
                texts.append("")
        result["pages"] = len(reader.pages)
        result["text_chars"] = len(" ".join(" ".join(texts).split()))
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
    return result


def local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].casefold()


def xml_info(content: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "bytes": len(content),
        "root": None,
        "body_nodes": 0,
        "section_nodes": 0,
        "para_nodes": 0,
        "reference_nodes": 0,
        "body_text_chars": 0,
        "fulltext_detected": False,
        "parse_error": None,
    }
    try:
        root = ET.fromstring(content)
        result["root"] = local_name(root)
        nodes = list(root.iter())
        bodies = [node for node in nodes if local_name(node) == "body"]
        result["body_nodes"] = len(bodies)
        result["section_nodes"] = sum(
            1 for node in nodes if local_name(node) in {"section", "sections", "sec"}
        )
        result["para_nodes"] = sum(
            1 for node in nodes if local_name(node) in {"para", "p"}
        )
        result["reference_nodes"] = sum(
            1 for node in nodes if local_name(node) in {"reference", "ref"}
        )
        body_text = " ".join("".join(node.itertext()) for node in bodies)
        result["body_text_chars"] = len(" ".join(body_text.split()))
        result["fulltext_detected"] = bool(
            result["body_nodes"]
            and result["body_text_chars"] >= 500
            and (result["para_nodes"] >= 3 or result["section_nodes"] >= 1)
        )
    except Exception as exc:
        result["parse_error"] = f"{type(exc).__name__}: {exc}"
    return result


def text_content(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def first_descendant(element: ET.Element, *names: str) -> ET.Element | None:
    wanted = {name.casefold() for name in names}
    return next((child for child in element.iter() if local_name(child) in wanted), None)


def object_catalog(root: ET.Element) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for element in root.iter():
        element_name = local_name(element)
        if element_name not in {"object", "choice"}:
            continue
        url = text_content(element)
        if not url.startswith(("https://", "http://")):
            continue
        mimetype = str(element.attrib.get("mimetype") or "")
        if not mimetype:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            mimetype = str((query.get("httpAccept") or [""])[0])
        output.append({
            "ref": str(element.attrib.get("ref") or ""),
            "category": str(element.attrib.get("category") or ""),
            "type": str(element.attrib.get("type") or ""),
            "mimetype": mimetype,
            "size": str(element.attrib.get("size") or ""),
            "url": url,
        })
    return output


def main_pdf_attachment_eids(root: ET.Element) -> list[str]:
    """Use only explicit main-PDF attachment IDs returned by the official API."""
    eids = []
    for element in root.iter():
        if local_name(element).casefold() not in {"attachment-eid", "object-eid"}:
            continue
        value = (element.text or "").strip()
        if re.fullmatch(r"1-s2\.0-[A-Za-z0-9]+-main(?:ext)?\.pdf", value, re.I) and value not in eids:
            eids.append(value)
    return eids


def merge_object_catalogs(*catalogs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge embedded and Object Retrieval API entries without duplicate URLs."""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for catalog in catalogs:
        for item in catalog:
            url = item.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
    return merged


def request_headers(api_key: str, inst_token: str, accept: str) -> dict[str, str]:
    headers = {"X-ELS-APIKey": api_key, "Accept": accept}
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token
    return headers


def download_pdf_object(
    http: requests.Session,
    objects: list[dict[str, str]],
    api_key: str,
    inst_token: str,
    proxy: dict[str, str | None],
    timeout: int,
    *,
    aam_only: bool = True,
) -> tuple[bytes | None, dict[str, Any]]:
    candidates = [
        item for item in objects
        if (
            item["mimetype"].casefold() == "application/pdf"
            or "pdf" in item["type"].casefold()
        )
        and (not aam_only or "aam-pdf" in item["type"].casefold())
    ]
    candidates.sort(key=lambda item: (item["type"].casefold() != "aam-pdf", item["type"]))
    attempts: list[dict[str, Any]] = []
    for item in candidates:
        response = fetch(
            http, item["url"], request_headers(api_key, inst_token, "application/pdf"),
            proxy, timeout,
        )
        info = pdf_info(response.content)
        attempt = {
            "type": item["type"],
            "ref": item["ref"],
            "declared_size": item["size"],
            "status_code": response.status_code,
            **info,
        }
        attempts.append(attempt)
        if response.status_code == 200 and info["signature_valid"] and (info.get("pages") or 0) > 1:
            return response.content, {"status": "success", "selected": attempt, "attempts": attempts}
    status = "not_found"
    if attempts and all(item.get("status_code") in {401, 403} for item in attempts):
        status = "authorization_failed"
    return None, {"status": status, "selected": None, "attempts": attempts}


INLINE_TAGS = {
    "bold": "strong",
    "italic": "em",
    "sup": "sup",
    "inf": "sub",
    "underline": "u",
    "monospace": "code",
    "small-caps": "span",
}


def mathml_html(element: ET.Element) -> str:
    tag = local_name(element)
    attributes = []
    for key, value in element.attrib.items():
        name = key.rsplit("}", 1)[-1]
        if name in {"altimg", "alttext"}:
            continue
        attributes.append(f' {html.escape(name)}="{html.escape(str(value), quote=True)}"')
    if tag == "math":
        attributes.append(' xmlns="http://www.w3.org/1998/Math/MathML"')
    pieces = [f"<{tag}{''.join(attributes)}>", html.escape(element.text or "")]
    for child in element:
        pieces.append(mathml_html(child))
        pieces.append(html.escape(child.tail or ""))
    pieces.append(f"</{tag}>")
    return "".join(pieces)


def inline_html(element: ET.Element) -> str:
    name = local_name(element)
    if name == "math":
        return mathml_html(element)
    if name in {"formula", "inline-formula"}:
        math_node = first_descendant(element, "math")
        return mathml_html(math_node) if math_node is not None else html.escape(text_content(element))
    tag = INLINE_TAGS.get(name)
    attributes = ""
    if name == "small-caps":
        attributes = ' class="small-caps"'
    elif name in {"cross-ref", "inter-ref"} and element.attrib.get("refid"):
        tag = "a"
        attributes = f' href="#{html.escape(str(element.attrib["refid"]), quote=True)}"'
    pieces = [html.escape(element.text or "")]
    for child in element:
        pieces.append(inline_html(child))
        pieces.append(html.escape(child.tail or ""))
    inner = "".join(pieces)
    return f"<{tag}{attributes}>{inner}</{tag}>" if tag else inner


def caption_html(element: ET.Element) -> str:
    label = next((text_content(child) for child in element if local_name(child) == "label"), "")
    caption = next((text_content(child) for child in element if local_name(child) == "caption"), "")
    return " ".join(part for part in (label, caption) if part)


def table_html(element: ET.Element) -> str:
    rows: list[str] = []
    for row in element.iter():
        if local_name(row) != "row":
            continue
        parent_is_head = False
        # ElementTree has no parent pointers; header rows normally appear before tbody.
        cells = []
        for entry in row:
            if local_name(entry) != "entry":
                continue
            rowspan = int(entry.attrib.get("morerows", "0") or 0) + 1
            attrs = f' rowspan="{rowspan}"' if rowspan > 1 else ""
            cells.append(f"<td{attrs}>{inline_html(entry)}</td>")
        if cells:
            rows.append(f"<tr>{''.join(cells)}</tr>")
    caption = html.escape(caption_html(element))
    caption_tag = f"<caption>{caption}</caption>" if caption else ""
    return f'<div class="table-wrap" id="{html.escape(element.attrib.get("id", ""))}"><table>{caption_tag}{"".join(rows)}</table></div>'


def block_html(
    element: ET.Element,
    image_paths: dict[str, Path],
    html_dir: Path,
    depth: int = 1,
    figure_by_id: dict[str, ET.Element] | None = None,
    placed_figure_ids: set[str] | None = None,
) -> str:
    figure_by_id = figure_by_id if figure_by_id is not None else {}
    placed_figure_ids = placed_figure_ids if placed_figure_ids is not None else set()

    def render(child: ET.Element, child_depth: int = depth) -> str:
        return block_html(
            child,
            image_paths,
            html_dir,
            child_depth,
            figure_by_id,
            placed_figure_ids,
        )

    name = local_name(element)
    element_id = html.escape(str(element.attrib.get("id") or ""), quote=True)
    id_attr = f' id="{element_id}"' if element_id else ""
    if name in {"sections", "body", "bibliography-sec"}:
        return "".join(render(child) for child in element)
    if name in {"section", "sec"}:
        return f"<section{id_attr}>{''.join(render(child, depth + 1) for child in element)}</section>"
    if name in {"section-title", "title"}:
        level = min(4, max(2, depth))
        return f"<h{level}{id_attr}>{inline_html(element)}</h{level}>"
    if name in {"para", "simple-para", "note-para"}:
        markup = f"<p{id_attr}>{inline_html(element)}</p>"
        # Elsevier XML explicitly connects an in-text citation to a float with
        # refid. Place only those figures after the paragraph containing their
        # first citation; never infer placement from captions or numbering.
        for reference in element.iter():
            if local_name(reference) not in {"cross-ref", "inter-ref"}:
                continue
            for refid in str(reference.attrib.get("refid") or "").split():
                figure = figure_by_id.get(refid)
                if figure is not None and refid not in placed_figure_ids:
                    markup += render(figure)
        return markup
    if name == "figure":
        figure_id = str(element.attrib.get("id") or "")
        if figure_id and figure_id in placed_figure_ids:
            return ""
        if figure_id:
            placed_figure_ids.add(figure_id)
        locator = ""
        for child in element.iter():
            if local_name(child) == "link" and child.attrib.get("locator"):
                locator = str(child.attrib["locator"])
                break
        image = image_paths.get(locator)
        image_tag = ""
        if image:
            relative = os.path.relpath(image, html_dir).replace("\\", "/")
            image_tag = f'<img src="{html.escape(relative, quote=True)}" alt="{html.escape(caption_html(element), quote=True)}">'
        return f'<figure{id_attr}>{image_tag}<figcaption>{html.escape(caption_html(element))}</figcaption></figure>'
    if name == "table":
        return table_html(element)
    if name in {"formula", "display"}:
        math_node = first_descendant(element, "math")
        formula = mathml_html(math_node) if math_node is not None else html.escape(text_content(element))
        label_node = first_descendant(element, "label")
        label = html.escape(text_content(label_node))
        return f'<div class="equation"{id_attr}><div>{formula}</div><span>{label}</span></div>'
    if name in {"list", "list-item", "item-info"}:
        if name == "list":
            return f"<ul{id_attr}>{''.join(render(child) for child in element)}</ul>"
        if name == "list-item":
            return f"<li{id_attr}>{''.join(render(child) for child in element)}</li>"
        return "".join(render(child) for child in element)
    if name == "bibliography":
        title = first_descendant(element, "section-title")
        refs = []
        for ref in element.iter():
            if local_name(ref) != "bib-reference":
                continue
            source = first_descendant(ref, "source-text")
            refs.append(f'<li id="{html.escape(ref.attrib.get("id", ""))}">{html.escape(text_content(source) or text_content(ref))}</li>')
        return f'<section class="references"><h2>{html.escape(text_content(title) or "References")}</h2><ol>{"".join(refs)}</ol></section>'
    if name in {"quote", "displayed-quote"}:
        return f"<blockquote{id_attr}>{inline_html(element)}</blockquote>"
    children = "".join(render(child) for child in element)
    return children or (f"<p{id_attr}>{inline_html(element)}</p>" if text_content(element) else "")


def choose_figure_objects(root: ET.Element, objects: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    required: set[str] = set()
    for figure in root.iter():
        if local_name(figure) != "figure":
            continue
        for child in figure.iter():
            if local_name(child) == "link" and child.attrib.get("locator"):
                required.add(str(child.attrib["locator"]))
    priority = {"high": 3, "standard": 2, "thumbnail": 1}
    selected: dict[str, dict[str, str]] = {}
    for item in objects:
        ref = item["ref"]
        if ref not in required or not item["mimetype"].startswith("image/"):
            continue
        old = selected.get(ref)
        if old is None or priority.get(item["category"], 0) > priority.get(old["category"], 0):
            selected[ref] = item
    return selected


def download_figure_assets(
    http: requests.Session,
    root: ET.Element,
    objects: list[dict[str, str]],
    api_key: str,
    inst_token: str,
    proxy: dict[str, str | None],
    timeout: int,
    asset_dir: Path,
    *,
    max_workers: int = 4,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    selected = choose_figure_objects(root, objects)
    paths: dict[str, Path] = {}
    attempts: list[dict[str, Any]] = []
    extension_by_type = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/svg+xml": ".svg"}
    asset_dir.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[int, str, dict[str, str], Path]] = []
    for index, (ref, item) in enumerate(selected.items()):
        path = asset_dir / f"{safe_name(ref)}{extension_by_type.get(item['mimetype'], '.bin')}"
        if path.exists() and path.stat().st_size > 100:
            paths[ref] = path
            attempts.append({"ref": ref, "status_code": 200, "bytes": path.stat().st_size, "success": True, "cached": True})
            continue
        pending.append((index, ref, item, path))

    def download_one_asset(
        entry: tuple[int, str, dict[str, str], Path],
    ) -> tuple[int, str, Path, dict[str, Any], bytes | None]:
        index, ref, item, path = entry
        started = __import__("time").monotonic()
        worker_http = http
        close_worker = False
        # A requests.Session should not be mutated/read concurrently.  Direct
        # API downloads therefore get a private session per worker; WebVPN's
        # requests-compatible adapter is kept serial by its caller.
        if max_workers > 1 and isinstance(http, requests.Session):
            worker_http = session(proxy)
            close_worker = True
        try:
            response = fetch(
                worker_http,
                item["url"],
                request_headers(api_key, inst_token, item["mimetype"] or "*/*"),
                proxy,
                timeout,
            )
            content = response.content
            valid = response.status_code == 200 and len(content) > 100
            attempt = {
                "ref": ref,
                "status_code": response.status_code,
                "bytes": len(content),
                "success": valid,
                "elapsed_s": round(__import__("time").monotonic() - started, 3),
            }
            return index, ref, path, attempt, content if valid else None
        finally:
            if close_worker:
                worker_http.close()

    worker_count = max(1, min(int(max_workers or 1), len(pending) or 1))
    completed: list[tuple[int, str, Path, dict[str, Any], bytes | None]] = []
    if worker_count == 1:
        completed = [download_one_asset(entry) for entry in pending]
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="elsevier-figure"
        ) as executor:
            futures = [executor.submit(download_one_asset, entry) for entry in pending]
            completed = [future.result() for future in as_completed(futures)]
    for _index, ref, path, attempt, content in sorted(completed):
        attempts.append(attempt)
        if content is not None:
            path.write_bytes(content)
            paths[ref] = path
    return paths, attempts


def find_chromium(explicit: Path | None = None) -> Path:
    if explicit:
        path = explicit.resolve()
        if path.exists():
            return path
        raise RuntimeError(f"Chromium executable does not exist: {path}")
    for name in ("chrome.exe", "msedge.exe", "chromium.exe", "chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        local_app = Path(local_app_data)
        candidates.extend(
            [
                local_app / "Google" / "Chrome" / "Application" / "chrome.exe",
                local_app / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
        candidates.extend(local_app.glob("ms-playwright/chromium-*/chrome-win64/chrome.exe"))
    candidates.extend([
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ])
    existing = [path for path in candidates if path.exists()]
    if not existing:
        raise RuntimeError("No Chromium/Chrome/Edge executable found; pass --chromium PATH")
    return sorted(existing)[-1]


def article_html(root: ET.Element, doi: str, image_paths: dict[str, Path], html_path: Path) -> str:
    core = first_descendant(root, "coredata") or root
    title = text_content(first_descendant(core, "title")) or doi
    journal = text_content(first_descendant(core, "publicationname"))
    date = text_content(first_descendant(core, "coverdisplaydate", "coverdate"))
    head = first_descendant(root, "head")
    authors: list[str] = []
    affiliations: list[str] = []
    abstract = ""
    keywords: list[str] = []
    if head is not None:
        for author in head.iter():
            if local_name(author) != "author":
                continue
            given = text_content(first_descendant(author, "given-name"))
            surname = text_content(first_descendant(author, "surname"))
            name = " ".join(part for part in (given, surname) if part)
            if name and name not in authors:
                authors.append(name)
        for affiliation in head.iter():
            if local_name(affiliation) == "affiliation" and affiliation.attrib.get("id"):
                label = text_content(first_descendant(affiliation, "label"))
                display = text_content(first_descendant(affiliation, "textfn", "source-text"))
                value = " ".join(part for part in (label, display) if part)
                if value and value not in affiliations:
                    affiliations.append(value)
        abstract_node = first_descendant(head, "abstract")
        abstract = text_content(abstract_node)
        for keyword in head.iter():
            if local_name(keyword) == "keyword":
                value = text_content(keyword)
                if value and value not in keywords:
                    keywords.append(value)
    body = first_descendant(root, "body")
    floats = first_descendant(root, "floats")
    bibliography = first_descendant(root, "bibliography")
    figure_by_id = {
        str(figure.attrib["id"]): figure
        for figure in (floats.iter() if floats is not None else [])
        if local_name(figure) == "figure" and figure.attrib.get("id")
    }
    placed_figure_ids: set[str] = set()
    body_markup = (
        block_html(body, image_paths, html_path.parent, 1, figure_by_id, placed_figure_ids)
        if body is not None else ""
    )
    floats_markup = (
        block_html(floats, image_paths, html_path.parent, 1, figure_by_id, placed_figure_ids)
        if floats is not None else ""
    )
    references_markup = block_html(bibliography, image_paths, html_path.parent) if bibliography is not None else ""
    affiliation_markup = "".join(f"<div>{html.escape(value)}</div>" for value in affiliations)
    keyword_markup = ", ".join(html.escape(value) for value in keywords)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
@page {{ size: A4; margin: 18mm 17mm 20mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #20242a; font: 10.5pt/1.55 Georgia, "Times New Roman", serif; }}
.notice {{ border: 1px solid #c4a04a; background: #fff8df; padding: 8px 12px; margin-bottom: 18px; font: 8.5pt/1.4 Arial, sans-serif; color: #654f14; }}
h1 {{ font: 700 22pt/1.15 Arial, sans-serif; margin: 12px 0 16px; color: #16283c; }}
h2 {{ font: 700 15pt/1.25 Arial, sans-serif; margin: 22px 0 8px; color: #163e63; border-bottom: 1px solid #d9e1e8; padding-bottom: 3px; }}
h3 {{ font: 700 12pt/1.3 Arial, sans-serif; margin: 17px 0 6px; color: #245478; }}
h4 {{ font: 700 10.5pt/1.3 Arial, sans-serif; margin: 13px 0 4px; }}
p {{ margin: 0 0 8px; text-align: justify; hyphens: auto; }}
.authors {{ font: 11pt/1.5 Arial, sans-serif; margin-bottom: 8px; }}
.affiliations {{ color: #52606d; font: 8.5pt/1.4 Arial, sans-serif; margin-bottom: 12px; }}
.meta {{ color: #52606d; font: 9pt Arial, sans-serif; margin: 7px 0; }}
.abstract {{ background: #f4f7fa; border-left: 4px solid #567b9d; padding: 10px 13px; margin: 16px 0; }}
.abstract h2 {{ margin-top: 0; border: 0; font-size: 12pt; }}
section {{ break-inside: auto; }}
figure {{ margin: 16px auto; text-align: center; break-inside: avoid; }}
figure img {{ max-width: 100%; max-height: 225mm; object-fit: contain; }}
figcaption {{ margin-top: 5px; font: 8.5pt/1.35 Arial, sans-serif; text-align: left; color: #3c4752; }}
.table-wrap {{ overflow: hidden; margin: 15px 0; break-inside: avoid; }}
table {{ width: 100%; border-collapse: collapse; font: 7.8pt/1.3 Arial, sans-serif; }}
caption {{ text-align: left; font-weight: 700; padding: 0 0 5px; }}
td, th {{ border: 1px solid #aeb8c2; padding: 4px; vertical-align: top; }}
.equation {{ display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; margin: 10px 0; break-inside: avoid; overflow-x: hidden; }}
.equation > div {{ text-align: center; }}
math {{ font-size: 112%; }}
blockquote {{ border-left: 3px solid #aeb8c2; margin: 12px 18px; padding-left: 12px; color: #46515c; }}
.references ol {{ padding-left: 25px; }}
.references li {{ margin: 0 0 5px; padding-left: 3px; font-size: 8.5pt; overflow-wrap: anywhere; }}
.small-caps {{ font-variant: small-caps; }}
a {{ color: inherit; text-decoration: none; }}
</style></head><body>
<div class="notice"><strong>Reconstructed full text.</strong> Generated from an authorized Elsevier XML response; pagination and typography differ from the publisher Version of Record.</div>
<header><div class="meta">{html.escape(journal)}{(' · ' + html.escape(date)) if date else ''}</div><h1>{html.escape(title)}</h1>
<div class="authors">{html.escape(', '.join(authors))}</div><div class="affiliations">{affiliation_markup}</div>
<div class="meta">DOI: {html.escape(doi)}</div></header>
<section class="abstract"><h2>Abstract</h2><p>{html.escape(abstract)}</p></section>
{f'<div class="meta"><strong>Keywords:</strong> {keyword_markup}</div>' if keyword_markup else ''}
<main>{body_markup}{f'<section class="floats"><h2>Figures and tables</h2>{floats_markup}</section>' if floats_markup else ''}{references_markup}</main></body></html>'''


def reconstruct_pdf(
    http: requests.Session,
    root: ET.Element,
    objects: list[dict[str, str]],
    doi: str,
    api_key: str,
    inst_token: str,
    proxy: dict[str, str | None],
    timeout: int,
    out_dir: Path,
    chromium: Path | None,
    *,
    figure_download_workers: int = 4,
) -> dict[str, Any]:
    stem = safe_name(doi)
    work_dir = out_dir / f"{stem}.reconstructed"
    asset_dir = work_dir / "assets"
    html_path = work_dir / "article.html"
    pdf_path = out_dir / f"{stem}.pdf"
    image_paths, image_attempts = download_figure_assets(
        http,
        root,
        objects,
        api_key,
        inst_token,
        proxy,
        timeout,
        asset_dir,
        max_workers=figure_download_workers,
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(article_html(root, doi, image_paths, html_path), encoding="utf-8")
    browser = find_chromium(chromium)
    with __import__("tempfile").TemporaryDirectory(prefix="elsevier-render-") as profile:
        command = [
            str(browser), "--headless", "--disable-gpu", "--no-sandbox",
            "--allow-file-access-from-files", f"--user-data-dir={profile}",
            "--no-pdf-header-footer", "--print-to-pdf-no-header",
            f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
        ]
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(120, timeout * 3),
            creationflags=(
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if os.name == "nt"
                else 0
            ),
        )
    if process.returncode != 0 or not pdf_path.exists():
        stderr = process.stderr or ""
        stdout = process.stdout or ""
        raise RuntimeError(
            f"Chromium PDF rendering failed ({process.returncode}): {(stderr or stdout)[-500:]}"
        )
    info = pdf_info(pdf_path.read_bytes())
    return {
        "status": "success" if info["signature_valid"] and (info.get("pages") or 0) > 1 else "error",
        "path": str(pdf_path.resolve()),
        "html": str(html_path.resolve()),
        "browser": str(browser),
        "figures_requested": len(image_attempts),
        "figures_downloaded": len(image_paths),
        "figure_attempts": image_attempts,
        **info,
    }


def fetch(
    http: requests.Session,
    url: str,
    headers: dict[str, str],
    proxy: dict[str, str | None],
    timeout: int,
) -> requests.Response:
    # Buffer a complete response before exposing it to PDF/XML consumers.
    # Restart interrupted idempotent GETs; never concatenate partial PDFs.
    deadline = time.monotonic() + max(1, timeout)
    history = []
    for attempt in range(1, 4):
        response = None
        received = 0
        try:
            remaining = max(1, deadline - time.monotonic())
            response = http.get(url, headers=headers, proxies=proxy,
                                timeout=min(timeout, remaining), allow_redirects=True, stream=True)
            restricted = ("not entitled" in response.headers.get("X-ELS-Status", "").casefold()
                          and "first page" in response.headers.get("X-ELS-Status", "").casefold()
                          and "application/pdf" in headers.get("Accept", ""))
            if restricted:
                # Some previews retain large embedded assets. The entitlement
                # header already rules out full text, so do not download it.
                response._content = b""
                response._content_consumed = True
                history.append({"attempt": attempt, "status": response.status_code,
                                "bytes": 0, "result": "restricted_preview_skipped",
                                "declared_bytes": response.headers.get("Content-Length", "")})
            else:
                chunks = []
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() >= deadline:
                        raise requests.exceptions.Timeout("Elsevier transfer exceeded its time budget")
                    if chunk:
                        chunks.append(chunk)
                        received += len(chunk)
                response._content = b"".join(chunks)
                response._content_consumed = True
                history.append({"attempt": attempt, "status": response.status_code,
                                "bytes": received, "result": "complete"})
            response.literature_transfer_attempts = history
            response.close()
            return response
        except requests.exceptions.SSLError:
            if response is not None:
                response.close()
            raise  # Never weaken TLS verification to get around a certificate error.
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            if response is not None:
                response.close()
            history.append({"attempt": attempt, "bytes": received,
                            "error_type": type(exc).__name__, "result": "interrupted"})
            if attempt == 3 or time.monotonic() + attempt >= deadline:
                exc.literature_transfer_attempts = history
                raise
            time.sleep(attempt)
    raise RuntimeError("Elsevier transfer failed")


def test_doi(
    http: requests.Session,
    doi: str,
    api_key: str,
    inst_token: str,
    proxy: dict[str, str | None],
    timeout: int,
    out_dir: Path,
    save_responses: bool,
    chromium: Path | None,
    *,
    use_aam: bool = False,
    use_xml_reconstruction: bool = False,
    want_xml_output: bool = False,
    figure_download_workers: int = 4,
    skip_publisher_pdf: bool = False,
) -> dict[str, Any]:
    encoded = urllib.parse.quote(doi, safe="/")
    url = f"https://api.elsevier.com/content/article/doi/{encoded}"
    stem = safe_name(doi)
    saved: dict[str, str] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / f"{stem}.pdf"
    xml_path = out_dir / f"{stem}.xml"
    skipped = {"status": "disabled", "status_code": None}
    pdf: dict[str, Any] = {**skipped, "reason": "not requested"}
    aam_redirect: dict[str, Any] = {**skipped, "reason": "AAM disabled"}
    xml: dict[str, Any] = {
        **skipped,
        "reason": "XML reconstruction disabled",
        "fulltext_detected": False,
        "parse_error": None,
    }
    object_pdf: dict[str, Any] = {
        "status": "disabled" if not use_aam else "not_attempted",
        "selected": None,
        "attempts": [],
    }
    object_catalog_url = f"https://api.elsevier.com/content/object/doi/{encoded}"
    object_retrieval: dict[str, Any] = {
        "url": object_catalog_url,
        "status": "not_requested",
        "status_code": None,
        "objects": 0,
        "aam_pdf_candidates": 0,
    }
    main_pdf_object: dict[str, Any] = {"status": "not_attempted", "attempts": []}
    reconstruction: dict[str, Any] | None = None
    catalog_objects: list[dict[str, str]] = []

    def result(classification: str) -> dict[str, Any]:
        return {
            "doi": doi,
            "route": "provided_transport",
            "main_pdf_object": main_pdf_object,
            "inst_token_configured": bool(inst_token),
            "options": {
                "use_aam": bool(use_aam),
                "use_xml_reconstruction": bool(use_xml_reconstruction),
                "want_xml_output": bool(want_xml_output),
                "figure_download_workers": int(figure_download_workers),
                "skip_publisher_pdf": bool(skip_publisher_pdf),
            },
            "pdf": pdf,
            "aam_redirect": aam_redirect,
            "xml": xml,
            "object_retrieval": object_retrieval,
            "xml_pdf_object": object_pdf,
            "reconstruction": reconstruction,
            "classification": classification,
            "saved": saved,
        }

    def load_catalog() -> list[dict[str, str]]:
        nonlocal catalog_objects, object_retrieval
        if object_retrieval.get("status") != "not_requested":
            return catalog_objects
        catalog_response = fetch(
            http,
            object_catalog_url,
            request_headers(api_key, inst_token, "text/xml"),
            proxy,
            timeout,
        )
        catalog_parse_error: str | None = None
        catalog_root: ET.Element | None = None
        if catalog_response.status_code in {200, 300}:
            try:
                catalog_root = ET.fromstring(catalog_response.content)
            except ET.ParseError as exc:
                catalog_parse_error = f"{type(exc).__name__}: {exc}"
        catalog_objects = object_catalog(catalog_root) if catalog_root is not None else []
        object_retrieval = {
            "url": object_catalog_url,
            "status": "complete",
            "status_code": catalog_response.status_code,
            "content_type": catalog_response.headers.get("Content-Type", ""),
            "els_status": catalog_response.headers.get("X-ELS-Status", ""),
            "bytes": len(catalog_response.content),
            "parse_error": catalog_parse_error,
            "objects": len(catalog_objects),
            "aam_pdf_candidates": sum(
                1
                for item in catalog_objects
                if "aam-pdf" in item["type"].casefold()
            ),
        }
        return catalog_objects

    def fetch_xml() -> tuple[ET.Element | None, list[dict[str, str]]]:
        nonlocal xml
        xml_response = fetch(
            http,
            url + "?view=FULL",
            request_headers(api_key, inst_token, "application/xml"),
            proxy,
            timeout,
        )
        xml = {
            "status": "complete",
            "status_code": xml_response.status_code,
            "content_type": xml_response.headers.get("Content-Type", ""),
            "els_status": xml_response.headers.get("X-ELS-Status", ""),
            **xml_info(xml_response.content),
        }
        root = (
            ET.fromstring(xml_response.content)
            if xml_response.status_code == 200 and xml.get("parse_error") is None
            else None
        )
        if save_responses and root is not None:
            xml_path.write_bytes(xml_response.content)
            saved["xml"] = str(xml_path.resolve())
        return root, object_catalog(root) if root is not None else []

    # Explicit XML output is a direct XML retrieval operation; AAM and PDF
    # reconstruction toggles only govern PDF fallback behavior.
    if want_xml_output:
        root, _embedded = fetch_xml()
        return result("xml_fulltext" if root is not None and xml.get("fulltext_detected") else "fulltext_not_confirmed")

    if skip_publisher_pdf:
        pdf = {
            **skipped,
            "status": "skipped",
            "reason": "publisher PDF already attempted before AAM/XML fallback",
        }
    else:
        try:
            pdf_response = fetch(
                http,
                url,
                request_headers(api_key, inst_token, "application/pdf"),
                proxy,
                timeout,
            )
            pdf = {
                "status": "complete",
                "status_code": pdf_response.status_code,
                "content_type": pdf_response.headers.get("Content-Type", ""),
                "els_status": pdf_response.headers.get("X-ELS-Status", ""),
                **pdf_info(pdf_response.content),
                "transfer_attempts": getattr(pdf_response, "literature_transfer_attempts", []),
            }
            likely_full_pdf = bool(
                pdf_response.status_code == 200
                and pdf.get("signature_valid")
                and isinstance(pdf.get("pages"), int)
                and pdf["pages"] > 1
            )
            if (
                save_responses
                and pdf_response.status_code == 200
                and pdf.get("signature_valid")
            ):
                preview_path = out_dir / f"{stem}.preview.pdf"
                preview_path.write_bytes(pdf_response.content)
                saved["api_pdf_response"] = str(preview_path.resolve())
            if likely_full_pdf:
                if save_responses:
                    final_path.write_bytes(pdf_response.content)
                    saved["fulltext_pdf"] = str(final_path.resolve())
                return result("publisher_pdf")
        except Exception as exc:
            pdf = {"status": "failed", "error_type": type(exc).__name__,
                   "reason": "Article PDF API request failed; trying official main PDF attachment",
                   "transfer_attempts": getattr(exc, "literature_transfer_attempts", [])}

        # Official MAIN PDF attachments can be available even when the article
        # PDF representation is deliberately restricted to its first page.
        try:
            root, _embedded = fetch_xml()
            eids = main_pdf_attachment_eids(root) if root is not None else []
            main_pdf_object["status"] = "not_found"
            if root is not None:
                core = first_descendant(root, "coredata")
                returned_doi = first_descendant(core, "doi") if core is not None else None
                if returned_doi is not None and clean_doi(text_content(returned_doi)).casefold() != clean_doi(doi).casefold():
                    eids = []
                    main_pdf_object["status"] = "doi_mismatch"
            for eid in eids:
                response = fetch(http,
                    "https://api.elsevier.com/content/object/eid/" + urllib.parse.quote(eid, safe=""),
                    request_headers(api_key, inst_token, "application/pdf"), proxy, timeout)
                info = pdf_info(response.content)
                attempt = {"eid": eid, "status_code": response.status_code,
                           "els_status": response.headers.get("X-ELS-Status", ""), **info}
                main_pdf_object["attempts"].append(attempt)
                if (response.status_code == 200 and info.get("signature_valid")
                        and (info.get("pages") or 0) > 1
                        and "not entitled" not in attempt["els_status"].casefold()):
                    main_pdf_object["status"] = "success"
                    main_pdf_object["selected"] = attempt
                    pdf = {"status": "complete", "method": "main_pdf_object", **attempt}
                    if save_responses:
                        final_path.write_bytes(response.content)
                        saved["fulltext_pdf"] = str(final_path.resolve())
                    return result("publisher_pdf")
        except Exception as exc:
            main_pdf_object.update(status="failed", error_type=type(exc).__name__)

    # Official Article Retrieval API fallback.  Elsevier documents
    # ``amsRedirect=true`` for redirecting an unentitled PDF request to the
    # author manuscript when one is available.
    if use_aam:
        aam_response = fetch(
            http,
            url + "?amsRedirect=true",
            request_headers(api_key, inst_token, "application/pdf"),
            proxy,
            timeout,
        )
        aam_redirect = {
            "status": "complete",
            "status_code": aam_response.status_code,
            "content_type": aam_response.headers.get("Content-Type", ""),
            "els_status": aam_response.headers.get("X-ELS-Status", ""),
            "final_url": aam_response.url,
            **pdf_info(aam_response.content),
        }
        if (
            aam_response.status_code == 200
            and aam_redirect.get("signature_valid")
            and isinstance(aam_redirect.get("pages"), int)
            and aam_redirect["pages"] > 1
        ):
            if save_responses:
                final_path.write_bytes(aam_response.content)
                saved["fulltext_pdf"] = str(final_path.resolve())
            return result("aam_pdf")

        objects = load_catalog()
        if objects:
            object_pdf_content, object_pdf = download_pdf_object(
                http,
                objects,
                api_key,
                inst_token,
                proxy,
                timeout,
                aam_only=True,
            )
            if object_pdf_content:
                if save_responses:
                    final_path.write_bytes(object_pdf_content)
                    saved["fulltext_pdf"] = str(final_path.resolve())
                return result("aam_pdf")

    if use_xml_reconstruction:
        root, embedded_objects = fetch_xml()
        if root is not None and xml.get("fulltext_detected"):
            objects = merge_object_catalogs(embedded_objects, load_catalog())
            if save_responses:
                reconstruction = reconstruct_pdf(
                    http,
                    root,
                    objects,
                    doi,
                    api_key,
                    inst_token,
                    proxy,
                    timeout,
                    out_dir,
                    chromium,
                    figure_download_workers=figure_download_workers,
                )
                if reconstruction.get("status") == "success":
                    saved["fulltext_pdf"] = str(reconstruction["path"])
                    return result("xml_reconstructed_pdf")
            return result("xml_fulltext_rebuild_available")

    if pdf.get("pages") == 1:
        return result("single_page_metadata_only")
    return result("fulltext_not_confirmed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test Elsevier PDF then XML retrieval with an explicit no-proxy map."
    )
    parser.add_argument("dois", nargs="+", help="One or more Elsevier article DOIs")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out-dir", type=Path, default=SKILL_DIR / "tmp" / "elsevier-direct-test")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--save", action="store_true", help="Save raw responses and produce the best complete PDF")
    parser.add_argument("--chromium", type=Path, default=None, help="Chromium/Chrome/Edge executable used for XML reconstruction")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        api_key, inst_token = load_credentials(args.config.resolve())
        proxy = dict(DEFAULT_PROXY)
        http = session(proxy=proxy)
        results = [
            test_doi(
                http,
                clean_doi(value),
                api_key,
                inst_token,
                proxy,
                max(1, args.timeout),
                args.out_dir.resolve(),
                args.save,
                args.chromium,
            )
            for value in args.dois
        ]
        print(json.dumps({"status": "success", "results": results}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "error", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
