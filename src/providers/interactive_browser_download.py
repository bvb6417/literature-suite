#!/usr/bin/env python3
"""Human-in-the-loop browser fallback for public publisher CAPTCHA pages.

The module never solves or bypasses a CAPTCHA. It opens the program's own
persistent Chrome/Edge profile (separate from the user's everyday browser)
and waits while the user completes the publisher's own verification. Browser cookies are then used only for the requested official
PDF URLs. A PDF downloaded by the browser itself is accepted as an alternative.
"""

from __future__ import annotations

import hashlib
import base64
import binascii
import json
import os
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from .app_paths import remove_tree_with_retry, runtime_dir
from .webvpn_login_cdp import (
    BrowserLoginError,
    CdpClient,
    ProfileBrowser,
    find_browser,
    launch_profile_browser,
)


class InteractiveDownloadError(RuntimeError):
    pass


def _looks_like_pdf(content: bytes) -> bool:
    return len(content) >= 1000 and content.startswith(b"%PDF-") and b"%%EOF" in content[-8192:]


def _atomic_pdf_write(path: Path, content: bytes, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise InteractiveDownloadError(f"目标文件已存在：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cookie_header(cookies: list[dict[str, Any]], url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").casefold()
    request_path = parsed.path or "/"
    now = time.time()
    values: list[str] = []
    for item in cookies:
        domain = str(item.get("domain") or "").lstrip(".").casefold()
        path = str(item.get("path") or "/")
        expires = float(item.get("expires") or 0)
        if (
            not item.get("name")
            or item.get("value") is None
            or not domain
            or (hostname != domain and not hostname.endswith("." + domain))
            or not request_path.startswith(path)
            or (expires > 0 and expires < now)
        ):
            continue
        values.append(f"{item['name']}={item['value']}")
    return "; ".join(values)


def _cookie_signature(cookies: list[dict[str, Any]]) -> str:
    material = "\n".join(
        sorted(
            f"{item.get('domain')}:{item.get('path')}:{item.get('name')}:{item.get('value')}"
            for item in cookies
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _cdp_cookie_params(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert persisted DevTools cookies back to accepted CookieParam fields."""
    allowed = {
        "name",
        "value",
        "url",
        "domain",
        "path",
        "secure",
        "httpOnly",
        "sameSite",
        "expires",
        "priority",
        "sameParty",
        "sourceScheme",
        "sourcePort",
        "partitionKey",
    }
    output: list[dict[str, Any]] = []
    for raw in cookies:
        if not raw.get("name") or raw.get("value") is None:
            continue
        item = {key: value for key, value in raw.items() if key in allowed}
        expires = item.get("expires")
        if isinstance(expires, (int, float)) and expires <= 0:
            item.pop("expires", None)
        output.append(item)
    return output


def _downloaded_pdf(directory: Path) -> tuple[Path, bytes] | None:
    candidates = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() == ".pdf"
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if _looks_like_pdf(content):
            return path, content
    return None


def _page_session_for_host(cdp: CdpClient, host: str) -> str | None:
    targets = cdp.call("Target.getTargets").get("targetInfos") or []
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        target_host = (
            urllib.parse.urlsplit(str(target.get("url") or "")).hostname or ""
        ).casefold()
        if target_host != host:
            continue
        attached = cdp.call(
            "Target.attachToTarget",
            {"targetId": target.get("targetId"), "flatten": True},
        )
        session_id = str(attached.get("sessionId") or "")
        if session_id:
            cdp.call("Runtime.enable", session_id=session_id)
            return session_id
    return None


def _browser_fetch_pdf(
    cdp: CdpClient, session_id: str, url: str
) -> dict[str, Any]:
    """Fetch same-origin content inside the verified page, preserving JS state."""
    encoded_url = json.dumps(url)
    expression = f"""
        (async () => {{
          try {{
            const response = await fetch({encoded_url}, {{
              credentials: 'include', redirect: 'follow', cache: 'no-store'
            }});
            const buffer = await response.arrayBuffer();
            const bytes = new Uint8Array(buffer);
            let binary = '';
            const chunk = 32768;
            for (let offset = 0; offset < bytes.length; offset += chunk) {{
              binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
            }}
            return JSON.stringify({{
              ok: response.ok,
              status: response.status,
              contentType: response.headers.get('content-type') || '',
              finalUrl: response.url,
              data: btoa(binary)
            }});
          }} catch (error) {{
            return JSON.stringify({{ok: false, error: String(error)}});
          }}
        }})()
    """
    evaluated = cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        session_id=session_id,
    )
    if evaluated.get("exceptionDetails"):
        return {"ok": False, "error": "浏览器页面执行 PDF 请求失败"}
    value = ((evaluated.get("result") or {}).get("value"))
    if not isinstance(value, str):
        return {"ok": False, "error": "浏览器页面没有返回可解析的请求结果"}
    try:
        payload = json.loads(value)
    except ValueError:
        return {"ok": False, "error": "浏览器页面返回了无效 JSON"}
    encoded = str(payload.pop("data", "") or "")
    try:
        payload["content"] = base64.b64decode(encoded, validate=True) if encoded else b""
    except (ValueError, binascii.Error):
        payload["content"] = b""
        payload["error"] = "浏览器页面返回的 PDF 数据编码无效"
    return payload


def _browser_discover_pdf_urls(
    cdp: CdpClient, session_id: str
) -> list[str]:
    """Discover PDF controls after the user has cleared a repository challenge."""
    expression = r"""
        (() => {
          const values = [];
          const add = (value) => {
            if (!value) return;
            try { values.push(new URL(value, location.href).href); } catch (_) {}
          };
          document.querySelectorAll('meta[name="citation_pdf_url"]').forEach(
            node => add(node.content)
          );
          document.querySelectorAll('link, a, iframe, embed, object').forEach(node => {
            const value = node.href || node.src || node.data || '';
            const hint = [value, node.type, node.rel, node.title,
                          node.getAttribute('aria-label'), node.textContent]
                         .filter(Boolean).join(' ').toLowerCase();
            if (hint.includes('.pdf') || hint.includes('/pdf') ||
                hint.includes('download') || hint.includes('full text')) add(value);
          });
          return JSON.stringify([...new Set(values)]);
        })()
    """
    evaluated = cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
        session_id=session_id,
    )
    value = ((evaluated.get("result") or {}).get("value"))
    if not isinstance(value, str):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [
        str(url)
        for url in parsed
        if isinstance(url, str) and url.startswith(("https://", "http://"))
    ] if isinstance(parsed, list) else []


def download_batch_after_manual_verification(
    items: list[dict[str, Any]],
    *,
    timeout: float,
    overwrite: bool,
    browser: Path | None = None,
) -> list[dict[str, Any]]:
    """Resolve one publisher-host queue with a single visible browser session."""
    browser = browser or find_browser()
    if browser is None:
        raise InteractiveDownloadError("找不到本机 Chrome 或 Microsoft Edge")
    if timeout <= 0:
        raise InteractiveDownloadError("人工验证等待时间必须大于 0 秒")
    prepared: list[dict[str, Any]] = []
    for raw in items:
        landing_url = str(raw.get("landing_url") or "").strip()
        landing_host = (urllib.parse.urlsplit(landing_url).hostname or "").casefold()
        pdf_urls = list(
            dict.fromkeys(
                str(url).strip()
                for url in (raw.get("pdf_urls") or [])
                if str(url).startswith(("https://", "http://"))
                and urllib.parse.urlsplit(str(url)).hostname
            )
        )
        if not landing_host:
            continue
        prepared.append(
            {
                "doi": str(raw.get("doi") or ""),
                "landing_url": landing_url,
                "landing_host": landing_host,
                "pdf_urls": pdf_urls,
                "output_path": Path(raw["output_path"]),
                "referer": str(raw.get("referer") or landing_url),
                "browser_cookies": [
                    item
                    for item in (raw.get("browser_cookies") or [])
                    if isinstance(item, dict)
                ],
            }
        )
    if not prepared:
        raise InteractiveDownloadError("没有可供人工验证的出版社或仓储库落地页")

    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    window: ProfileBrowser | None = None
    attempts: dict[str, list[dict[str, Any]]] = {
        item["doi"]: [] for item in prepared
    }
    results: dict[str, dict[str, Any]] = {}
    last_reasons: dict[str, str] = {
        item["doi"]: "等待用户在浏览器中完成人机验证" for item in prepared
    }
    pending: dict[str, dict[str, Any]] = {item["doi"]: item for item in prepared}

    downloads = Path(tempfile.mkdtemp(prefix="publisher-verify-", dir=str(runtime)))
    initial_cookies: list[dict[str, Any]] = []
    for item in prepared:
        initial_cookies.extend(item["browser_cookies"])
    try:
        # The persistent profile remembers publisher verification
        # (e.g. Cloudflare clearance) between runs.
        window = launch_profile_browser(
            "about:blank" if initial_cookies else prepared[0]["landing_url"],
            browser=browser,
            extra_args=("--disable-pdf-extension",),
        )
        cdp = window.cdp
        if initial_cookies:
            cookie_params = _cdp_cookie_params(initial_cookies)
            if cookie_params:
                cdp.call("Storage.setCookies", {"cookies": cookie_params})
            cdp.call(
                "Target.createTarget",
                {"url": prepared[0]["landing_url"]},
            )
        try:
            cdp.call(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": str(downloads.resolve()),
                    "eventsEnabled": True,
                },
            )
        except BrowserLoginError:
            pass
        browser_info = cdp.call("Browser.getVersion")
        user_agent = str(browser_info.get("userAgent") or "Mozilla/5.0")
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )
        page_sessions: dict[str, str] = {}
        deadline = time.monotonic() + timeout
        last_signature = ""
        last_probe = 0.0
        opened_dois = {prepared[0]["doi"]}
        last_tab_opened = time.monotonic()
        while pending and time.monotonic() < deadline:
            if not window.alive():
                for doi in pending:
                    last_reasons[doi] = "人工验证窗口已关闭，但尚未检测到有效 PDF"
                break

            # A manual click is unambiguous when just one item remains.
            if len(prepared) == 1 and len(pending) == 1:
                browser_file = _downloaded_pdf(downloads)
                if browser_file is not None:
                    downloaded_path, content = browser_file
                    doi, item = next(iter(pending.items()))
                    _atomic_pdf_write(item["output_path"], content, overwrite=overwrite)
                    results[doi] = {
                        "ok": True,
                        "doi": doi,
                        "method": "browser_download_after_manual_verification",
                        "browser": browser.name,
                        "path": str(item["output_path"].resolve()),
                        "bytes": len(content),
                        "browser_filename": downloaded_path.name,
                        "attempts": attempts[doi],
                    }
                    pending.pop(doi)
                    break

            storage = cdp.call("Storage.getCookies")
            cookies = [
                item for item in (storage.get("cookies") or []) if isinstance(item, dict)
            ]
            signature = _cookie_signature(cookies)
            now = time.monotonic()
            if signature != last_signature or now - last_probe >= 4.0:
                last_signature = signature
                last_probe = now
                for doi, item in list(pending.items()):
                    browser_completed = False
                    page_session = page_sessions.get(item["landing_host"])
                    if not page_session:
                        try:
                            page_session = _page_session_for_host(
                                cdp, item["landing_host"]
                            )
                        except BrowserLoginError:
                            page_session = None
                        if page_session:
                            page_sessions[item["landing_host"]] = page_session
                    if page_session:
                        try:
                            discovered_urls = _browser_discover_pdf_urls(
                                cdp, page_session
                            )
                        except BrowserLoginError:
                            discovered_urls = []
                            page_sessions.pop(item["landing_host"], None)
                        new_urls = [
                            url
                            for url in discovered_urls
                            if url not in item["pdf_urls"]
                            and (
                                urllib.parse.urlsplit(url).hostname or ""
                            ).casefold() == item["landing_host"]
                        ]
                        if new_urls:
                            item["pdf_urls"].extend(new_urls)
                            attempts[doi].append(
                                {
                                    "transport": "verified_browser_dom_discovery",
                                    "urls": new_urls,
                                }
                            )
                        for url in item["pdf_urls"]:
                            url_host = (
                                urllib.parse.urlsplit(url).hostname or ""
                            ).casefold()
                            if url_host != item["landing_host"]:
                                continue
                            try:
                                browser_response = _browser_fetch_pdf(
                                    cdp, page_session, url
                                )
                            except BrowserLoginError as exc:
                                page_sessions.pop(item["landing_host"], None)
                                attempts[doi].append(
                                    {
                                        "transport": "verified_browser_page",
                                        "url": url,
                                        "exception_type": type(exc).__name__,
                                        "exception_message": str(exc),
                                    }
                                )
                                break
                            content = browser_response.pop("content", b"")
                            attempts[doi].append(
                                {
                                    "transport": "verified_browser_page",
                                    "url": url,
                                    "final_url": browser_response.get("finalUrl"),
                                    "status": browser_response.get("status"),
                                    "content_type": str(
                                        browser_response.get("contentType") or ""
                                    ).split(";", 1)[0],
                                    "bytes": len(content),
                                    "error": browser_response.get("error"),
                                }
                            )
                            if (
                                browser_response.get("status") == 200
                                and _looks_like_pdf(content)
                            ):
                                _atomic_pdf_write(
                                    item["output_path"], content, overwrite=overwrite
                                )
                                results[doi] = {
                                    "ok": True,
                                    "doi": doi,
                                    "method": "verified_browser_page_fetch",
                                    "browser": browser.name,
                                    "path": str(item["output_path"].resolve()),
                                    "bytes": len(content),
                                    "resolved_url": browser_response.get("finalUrl"),
                                    "attempts": attempts[doi],
                                }
                                pending.pop(doi, None)
                                browser_completed = True
                                break
                            last_reasons[doi] = (
                                f"浏览器内请求 HTTP {browser_response.get('status')}，"
                                "请先完成页面中的人机验证"
                            )
                    if browser_completed:
                        continue
                    for url in item["pdf_urls"]:
                        headers = {"Referer": item["referer"]}
                        cookie = _cookie_header(cookies, url)
                        if cookie:
                            headers["Cookie"] = cookie
                        try:
                            response = session.get(
                                url,
                                headers=headers,
                                timeout=min(20.0, max(2.0, deadline - now)),
                                allow_redirects=True,
                            )
                        except requests.RequestException as exc:
                            last_reasons[doi] = f"{type(exc).__name__}: {exc}"
                            attempts[doi].append(
                                {
                                    "url": url,
                                    "exception_type": type(exc).__name__,
                                    "exception_message": str(exc),
                                }
                            )
                            continue
                        content = response.content
                        attempts[doi].append(
                            {
                                "url": url,
                                "final_url": response.url,
                                "status": response.status_code,
                                "content_type": response.headers.get("Content-Type", "").split(";", 1)[0],
                                "bytes": len(content),
                            }
                        )
                        if response.status_code == 200 and _looks_like_pdf(content):
                            _atomic_pdf_write(
                                item["output_path"], content, overwrite=overwrite
                            )
                            results[doi] = {
                                "ok": True,
                                "doi": doi,
                                "method": "browser_cookies_after_manual_verification",
                                "browser": browser.name,
                                "path": str(item["output_path"].resolve()),
                                "bytes": len(content),
                                "resolved_url": response.url,
                                "attempts": attempts[doi],
                            }
                            pending.pop(doi, None)
                            break
                        last_reasons[doi] = (
                            f"HTTP {response.status_code}，仍未返回 PDF；"
                            "请在浏览器中完成验证，必要时点击 PDF 下载"
                        )

            # If the first verification did not unlock another article,
            # open that article in a new tab without starting another browser.
            if pending and time.monotonic() - last_tab_opened >= 15.0:
                next_item = next(
                    (item for doi, item in pending.items() if doi not in opened_dois),
                    None,
                )
                if next_item is not None:
                    try:
                        cdp.call("Target.createTarget", {"url": next_item["landing_url"]})
                        opened_dois.add(next_item["doi"])
                        last_tab_opened = time.monotonic()
                    except BrowserLoginError:
                        pass
            time.sleep(0.75)
    finally:
        if window is not None:
            window.close()
        remove_tree_with_retry(downloads)

    for doi, item in pending.items():
        results[doi] = {
            "ok": False,
            "doi": doi,
            "reason": last_reasons[doi],
            "attempts": attempts[doi],
        }
    return [results[item["doi"]] for item in prepared]


def download_after_manual_verification(
    *,
    landing_url: str,
    pdf_urls: list[str],
    output_path: Path,
    timeout: float,
    overwrite: bool,
    referer: str | None = None,
    browser: Path | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for a one-item verification queue."""
    return download_batch_after_manual_verification(
        [
            {
                "doi": output_path.stem,
                "landing_url": landing_url,
                "pdf_urls": pdf_urls,
                "output_path": output_path,
                "referer": referer or landing_url,
            }
        ],
        timeout=timeout,
        overwrite=overwrite,
        browser=browser,
    )[0]
