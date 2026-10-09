#!/usr/bin/env python3
"""JavaScript-capable Elsevier PDF retrieval through an institution gateway.

ScienceDirect's ``/pdfft`` route can return an HTTP-200 JavaScript challenge to
plain HTTP clients.  This adapter navigates the already-proxied institutional
URL in a visible window of the program's persistent browser profile, then
accepts only an actual PDF response that the browser receives.  It never guesses or persists ScienceDirect CDN URLs.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from .app_paths import runtime_dir, temporary_folder, browser_data_dir
from .webvpn_login_cdp import (
    BrowserLoginError,
    CdpClient,
    ProfileBrowser,
    find_browser,
    launch_profile_browser,
    local_http_session,
)


class InstitutionBrowserError(RuntimeError):
    pass


class InstitutionSessionExpired(InstitutionBrowserError):
    pass


class ContentProviderError(InstitutionBrowserError):
    """An explicit publisher error page, not a pending download."""


def _check_content_error(mux, session_id):
    try:
        result = mux.call(
            "Runtime.evaluate",
            {"expression": "(() => { const t = (document.body?.innerText || '').replace(/\\s+/g, ' '); return /\\bCPE00001\\b/i.test(t) || /there was a problem providing the content you requested/i.test(t); })()",
             "returnByValue": True},
            session_id=session_id, timeout=1,
        )
    except BrowserLoginError:
        return
    if (result.get("result") or {}).get("value") is True:
        raise ContentProviderError("Elsevier 返回 CPE00001/内容提供错误页，立即结束当前访问路线")


def _has_human_challenge(mux, session_id):
    """True = challenge, False = loaded page, None = navigation/unknown."""
    try:
        result = mux.call("Runtime.evaluate", {
            "expression": "(() => { const t=(document.body?.innerText||'').slice(0,2000); const c=/are you a robot|please confirm you are a human|captcha challenge|请稍候|verify (?:that )?you are human|checking your browser|just a moment|performing security verification|验证您是人类|验证你是真人|确认您是真人|人机验证|安全验证/i.test(document.title+' '+t) || [...document.querySelectorAll('iframe[src*=\"challenges.cloudflare.com\"]')].some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&getComputedStyle(e).visibility!=='hidden';}); return c ? true : (document.readyState==='complete' && t.trim().length>30 ? false : null); })()",
            "returnByValue": True}, session_id=session_id, timeout=1)
        value = (result.get("result") or {}).get("value")
        return value if isinstance(value, bool) else None
    except BrowserLoginError:
        return None


def _wait_for_human_challenge(mux, session_id):
    mux.call("Page.bringToFront", session_id=session_id, timeout=2)
    print(json.dumps({"worker_event": "stage", "source": "webvpn",
                      "stage": "campus_human_verification"}), flush=True)
    print("校园网直连遇到人机验证，请在打开的浏览器内手动完成；最多等待 180 秒。", file=__import__('sys').stderr, flush=True)
    deadline = time.monotonic() + 180
    clear_since = None
    while time.monotonic() < deadline:
        _check_content_error(mux, session_id)
        state = _has_human_challenge(mux, session_id)
        if state is False:
            if clear_since is None:
                clear_since = time.monotonic()
            elif time.monotonic() - clear_since >= 2:
                return
        else:
            clear_since = None
        time.sleep(0.5)
    raise BrowserLoginError("等待人工验证超时，请完成验证后重试")


def _click_article_pdf(mux, session_id, expected_pii, allow_human_verification=True):
    """Click the article's own PDF control; do not synthesize a PDF URL."""
    expression = """(() => {
      const pii = __PII__;
      if(/authserver|cas\/login/i.test(location.pathname))return {login_required:true};
      if (!location.pathname.includes(pii)) return {clicked:false};
      const visible = e => {const r=e.getBoundingClientRect();return r.width>0 && r.height>0 && getComputedStyle(e).visibility!=='hidden' && !e.disabled;};
      const controls=[...document.querySelectorAll('a,button,[role="button"]')].filter(visible);
      const exact=controls.find(e=>{try{const u=new URL(e.href,location.href);return [location.hostname,'www.sciencedirect.com','sciencedirect.com'].includes(u.hostname)&&u.pathname.includes('/pii/'+pii+'/pdfft');}catch{return false;}});
      const label=controls.find(e=>{
        if(e.href){try{const u=new URL(e.href,location.href);if(![location.hostname,'www.sciencedirect.com','sciencedirect.com'].includes(u.hostname)||!u.pathname.includes(pii))return false;}catch{return false;}}
        return /(?:view|download|open)\s+(?:full[- ]?text\s+)?pdf|查看\s*pdf|下载\s*pdf/i.test((e.innerText||'')+' '+(e.getAttribute('aria-label')||''));
      });
      // A real URL can appear only after opening the site's PDF menu.
      const control=exact||label;
      if(!control)return {clicked:false};
      if(!exact && control.dataset.literaturePdfMenuOpened==='1') return {clicked:false};
      // Keep the native link and all query parameters in the monitored tab.
      if(control.tagName==='A')control.target='_self';
      if(!exact)control.dataset.literaturePdfMenuOpened='1';
      control.click();
      return {clicked:!!exact,menu_opened:!exact,method:exact?'article_pdf_link':'article_pdf_menu'};
    })()""".replace("__PII__", json.dumps(expected_pii))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _check_content_error(mux, session_id)
        if _has_human_challenge(mux, session_id) is True:
            if not allow_human_verification:
                raise InstitutionBrowserError("WebVPN 文章页要求人机验证，结束本路线并尝试校园网直连")
            started = time.monotonic()
            _wait_for_human_challenge(mux, session_id)
            deadline += time.monotonic() - started
        try:
            result = mux.call("Runtime.evaluate", {"expression": expression,
                              "returnByValue": True, "userGesture": True},
                              session_id=session_id, timeout=2)
            value = (result.get("result") or {}).get("value") or {}
            if value.get("login_required"):
                raise InstitutionSessionExpired("机构登录已失效，文章页被重定向到学校认证页面")
            if value.get("clicked"):
                return value
        except BrowserLoginError:
            pass
        time.sleep(0.5)
    raise InstitutionBrowserError("文章页未找到可用的 PDF 下载按钮；未尝试拼接直链，请在文章页确认全文权限")


def _is_cdp_receive_timeout(exc: Exception) -> bool:
    """Distinguish an idle socket poll from a broken CDP connection."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == (
        "WebSocketTimeoutException"
    )


def existing_cdp_websocket(port: int = 9222) -> str:
    """Return a running opt-in Chrome CDP endpoint without changing Chrome."""
    session = local_http_session()
    try:
        response = session.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
        if response.status_code == 200:
            return str(response.json().get("webSocketDebuggerUrl") or "")
    except (OSError, ValueError):
        return ""
    finally:
        session.close()
    return ""


def cookie_params(cookies: Iterable[Any]) -> list[dict[str, Any]]:
    """Convert RequestsCookieJar entries to CDP CookieParam dictionaries."""
    output: list[dict[str, Any]] = []
    for cookie in cookies:
        name = str(getattr(cookie, "name", "") or "")
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "")
        if not name or not domain:
            continue
        item: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": str(getattr(cookie, "path", "/") or "/"),
            "secure": bool(getattr(cookie, "secure", True)),
        }
        expires = getattr(cookie, "expires", None)
        if isinstance(expires, (int, float)) and expires > 0:
            item["expires"] = float(expires)
        rest = getattr(cookie, "_rest", {}) or {}
        if "HttpOnly" in rest or "httponly" in rest:
            item["httpOnly"] = True
        output.append(item)
    return output


class _CdpMux:
    """Preserve CDP events that arrive while a command response is pending."""

    def __init__(self, cdp: CdpClient) -> None:
        self.cdp = cdp
        self.events: deque[dict[str, Any]] = deque()

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str = "",
        timeout: float = 8,
    ) -> dict[str, Any]:
        request_id = self.cdp._next_id
        self.cdp._next_id += 1
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id
        try:
            self.cdp.socket.send(json.dumps(payload, separators=(",", ":")))
        except Exception as exc:
            raise BrowserLoginError(
                f"CDP {method} 发送失败：{type(exc).__name__}"
            ) from exc
        deadline = time.monotonic() + max(0.5, timeout)
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    self.cdp.socket.settimeout(
                        max(0.1, min(0.5, remaining))
                    )
                    message = json.loads(self.cdp.socket.recv())
                except Exception as exc:
                    if _is_cdp_receive_timeout(exc):
                        continue
                    raise BrowserLoginError(
                        f"CDP {method} 接收通道中断：{type(exc).__name__}: {exc}"
                    ) from exc
                if message.get("id") != request_id:
                    if "id" not in message:
                        self.events.append(message)
                    continue
                if message.get("error"):
                    error = message.get("error") or {}
                    raise BrowserLoginError(
                        f"CDP {method} 失败："
                        f"{error.get('message', error)}"
                    )
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        finally:
            try:
                self.cdp.socket.settimeout(6)
            except Exception:
                pass
        raise BrowserLoginError(f"CDP {method} 响应超时")

    def event(self, deadline: float) -> dict[str, Any] | None:
        if self.events:
            return self.events.popleft()
        try:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    self.cdp.socket.settimeout(
                        max(0.1, min(0.5, remaining))
                    )
                    message = json.loads(self.cdp.socket.recv())
                except Exception as exc:
                    if _is_cdp_receive_timeout(exc):
                        continue
                    raise BrowserLoginError(
                        "CDP 事件通道中断："
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                if "id" not in message:
                    return message
            return None
        finally:
            try:
                self.cdp.socket.settimeout(6)
            except Exception:
                pass


def _fetch_header(params: dict[str, Any], name: str) -> str:
    wanted = name.casefold()
    for item in params.get("responseHeaders") or []:
        if str(item.get("name") or "").casefold() == wanted:
            return str(item.get("value") or "")
    return ""


def _fetch_body_is_complete(
    status: int, params: dict[str, Any], body_bytes: int
) -> bool:
    """Verify that Fetch returned the full entity, not a Viewer preview chunk."""
    content_length = _fetch_header(params, "content-length").strip()
    content_encoding = _fetch_header(params, "content-encoding").strip()
    if status == 200:
        if content_length.isdigit() and not content_encoding:
            return body_bytes == int(content_length)
        return body_bytes > 0
    if status != 206:
        return False
    content_range = _fetch_header(params, "content-range").strip()
    matched = re.fullmatch(
        r"bytes\s+(\d+)-(\d+)/(\d+)", content_range, flags=re.IGNORECASE
    )
    if not matched:
        return False
    start, end, total = (int(value) for value in matched.groups())
    return start == 0 and end + 1 == total and body_bytes == total


def _fetch_expected_body_bytes(
    status: int, params: dict[str, Any]
) -> int:
    """Return the exact intercepted entity length when headers prove it."""
    if _fetch_header(params, "content-encoding").strip():
        return 0
    content_length = _fetch_header(params, "content-length").strip()
    if not content_length.isdigit():
        return 0
    declared = int(content_length)
    if declared <= 0:
        return 0
    if status == 200:
        return declared
    if status != 206:
        return 0
    matched = re.fullmatch(
        r"bytes\s+(\d+)-(\d+)/(\d+)",
        _fetch_header(params, "content-range").strip(),
        flags=re.IGNORECASE,
    )
    if not matched:
        return 0
    start, end, _total = (int(value) for value in matched.groups())
    return declared if end >= start and end - start + 1 == declared else 0


def _request_headers_with_full_range(
    request: dict[str, Any],
) -> list[dict[str, str]]:
    """Preserve a paused CDN request's headers and replace only Range."""
    output: list[dict[str, str]] = []
    headers = request.get("headers") or {}
    if isinstance(headers, dict):
        items = headers.items()
    elif isinstance(headers, list):
        items = (
            (item.get("name"), item.get("value"))
            for item in headers
            if isinstance(item, dict)
        )
    else:
        items = ()
    for name, value in items:
        if not name or str(name).casefold() == "range":
            continue
        output.append({"name": str(name), "value": str(value)})
    output.append({"name": "Range", "value": "bytes=0-"})
    return output


def _decode_cdp_io_data(block: dict[str, Any]) -> bytes:
    """Restore IO.read bytes using Chrome's per-chunk encoding flag."""
    raw = block.get("data")
    if not isinstance(raw, str):
        raise ValueError("CDP IO.read data is not a string")
    if block.get("base64Encoded") is True:
        return base64.b64decode(raw, validate=True)
    # Chrome 150 sends a chunk directly when its bytes form valid UTF-8,
    # regardless of the response MIME type.  Latin-1 either corrupts that
    # chunk or raises for non-Latin code points.
    return raw.encode("utf-8", errors="strict")


def _read_cdp_stream(
    mux: _CdpMux,
    session_id: str,
    handle: str,
    *,
    max_bytes: int,
    deadline: float,
    expected_bytes: int = 0,
    chunk_bytes: int = 256 * 1024,
) -> tuple[bytes, int, bool]:
    chunks: list[bytes] = []
    total = 0
    complete = False
    chunk_bytes = max(16 * 1024, min(1024 * 1024, int(chunk_bytes)))
    try:
        if expected_bytes < 0 or expected_bytes > max_bytes:
            return b"", 0, False
        while time.monotonic() < deadline:
            if expected_bytes and total >= expected_bytes:
                complete = total == expected_bytes
                break
            requested = (
                min(chunk_bytes, expected_bytes - total)
                if expected_bytes
                else chunk_bytes
            )
            block = mux.call(
                "IO.read",
                {"handle": handle, "size": requested},
                session_id=session_id,
                # The paper deadline is the only transfer ceiling.  A separate
                # fixed per-chunk timeout incorrectly kills slow but active
                # institutional links.
                timeout=max(0.5, deadline - time.monotonic()),
            )
            try:
                chunk = _decode_cdp_io_data(block)
            except (binascii.Error, TypeError, UnicodeError, ValueError):
                return b"", total, False
            if not chunk:
                if block.get("eof"):
                    complete = not expected_bytes or total == expected_bytes
                break
            total += len(chunk)
            if total > max_bytes or (
                expected_bytes and total > expected_bytes
            ):
                return b"", total, False
            chunks.append(chunk)
            # DevToolsStreamPipe may return the last requested body bytes with
            # eof=false and only report EOF on a subsequent read.  When the
            # response headers prove the length, another read can hang while
            # waiting to fill its requested size; reaching the length is the
            # completion condition.
            if expected_bytes and total == expected_bytes:
                complete = True
                break
            if block.get("eof"):
                complete = not expected_bytes or total == expected_bytes
                break
    except BrowserLoginError:
        return b"", total, False
    finally:
        try:
            mux.call(
                "IO.close",
                {"handle": handle},
                session_id=session_id,
                timeout=3,
            )
        except Exception:
            pass
    return (b"".join(chunks) if complete else b""), total, complete


def _continue_fetch_request(
    mux: _CdpMux, session_id: str, request_id: str
) -> bool:
    try:
        mux.call(
            "Fetch.continueRequest",
            {"requestId": request_id},
            session_id=session_id,
            timeout=3,
        )
        return True
    except Exception:
        return False


def _continue_fetch_response(
    mux: _CdpMux, session_id: str, request_id: str
) -> bool:
    try:
        mux.call(
            "Fetch.continueResponse",
            {"requestId": request_id},
            session_id=session_id,
            timeout=3,
        )
        return True
    except Exception:
        # continueResponse is experimental and may be absent in an older Edge.
        # At response stage, continueRequest without overrides releases the
        # original response unchanged.
        return _continue_fetch_request(mux, session_id, request_id)


def _abort_fetch_response(
    mux: _CdpMux,
    session_id: str,
    request_id: str,
    *,
    stream_taken: bool = False,
) -> bool:
    try:
        mux.call(
            "Fetch.failRequest",
            {"requestId": request_id, "errorReason": "Aborted"},
            session_id=session_id,
            timeout=2,
        )
        return True
    except Exception:
        pass
    # Once takeResponseBodyAsStream succeeds CDP explicitly forbids continuing
    # the original response. Fetch.disable/target cleanup will release it if
    # failRequest itself was rejected.
    if not stream_taken:
        return _continue_fetch_request(mux, session_id, request_id)
    return False


def _send_navigation_and_collect(
    cdp: CdpClient,
    session_id: str,
    url: str,
    *,
    timeout: float,
    expected_pii: str,
    ignored_target_ids: set[str],
    max_bytes: int,
    allow_human_verification: bool = False,
) -> dict[str, Any]:
    """Capture the original browser PDF response before Viewer transforms it."""
    mux = _CdpMux(cdp)
    pdf_responses: list[tuple[str, dict[str, Any]]] = []
    completed: set[str] = set()
    viewer_targets: dict[str, str] = {}
    held_fetch_requests: list[tuple[str, str]] = []
    taken_fetch_requests: set[tuple[str, str]] = set()
    fetch_content = b""
    fetch_response_count = 0
    fetch_status = 0
    fetch_content_type = ""
    fetch_content_length = ""
    fetch_expected_bytes = 0
    fetch_bytes = 0
    fetch_stream_complete = False
    fetch_body_complete = False
    fetch_signature_valid = False
    fetch_eof_valid = False
    fetch_enabled = False
    retry_at: float | None = None
    final_failure_at: float | None = None
    navigation_retry_count = 0
    page_loaded_at: float | None = None
    next_error_probe = time.monotonic()
    human_waited = False
    deadline = time.monotonic() + max(3.0, timeout)
    post_load_wait = min(20.0, max(12.0, timeout * 0.75))

    def schedule_capture_retry() -> None:
        nonlocal retry_at, final_failure_at
        if navigation_retry_count == 0 and retry_at is None:
            retry_at = min(deadline, time.monotonic() + 2.0)
        elif navigation_retry_count > 0:
            final_failure_at = min(deadline, time.monotonic() + 2.0)

    try:
        # Observe native responses for both WebVPN and campus access. Do not
        # request a guessed /pdfft URL or alter the publisher's request headers.
        click_started = time.monotonic()
        _click_article_pdf(mux, session_id, expected_pii,
                           allow_human_verification=allow_human_verification)
        deadline += time.monotonic() - click_started
        while time.monotonic() < deadline:
            if time.monotonic() >= next_error_probe:
                _check_content_error(mux, session_id)
                if allow_human_verification and not human_waited and _has_human_challenge(mux, session_id):
                    waiting_started = time.monotonic()
                    _wait_for_human_challenge(mux, session_id)
                    deadline += time.monotonic() - waiting_started
                    page_loaded_at = time.monotonic()
                    human_waited = True
                next_error_probe = time.monotonic() + 1.0
            if (
                page_loaded_at is not None
                and time.monotonic() - page_loaded_at >= post_load_wait
                and not viewer_targets
                and fetch_response_count == 0
            ):
                break
            event_deadline = min(deadline, next_error_probe)
            if retry_at is not None:
                event_deadline = min(event_deadline, retry_at)
            if final_failure_at is not None:
                event_deadline = min(event_deadline, final_failure_at)
            message = mux.event(event_deadline)
            if message is None:
                if (
                    retry_at is not None
                    and time.monotonic() >= retry_at
                    and navigation_retry_count == 0
                    and time.monotonic() + 3 < deadline
                ):
                    for held_session, held_request_id in held_fetch_requests:
                        _abort_fetch_response(
                            mux,
                            held_session,
                            held_request_id,
                            stream_taken=(
                                held_session,
                                held_request_id,
                            )
                            in taken_fetch_requests,
                        )
                    held_fetch_requests.clear()
                    taken_fetch_requests.clear()
                    mux.call(
                        "Page.navigate",
                        {"url": url},
                        session_id=session_id,
                        timeout=min(8, max(2, deadline - time.monotonic())),
                    )
                    navigation_retry_count = 1
                    retry_at = None
                    continue
                if (
                    final_failure_at is not None
                    and time.monotonic() >= final_failure_at
                ):
                    break
                if time.monotonic() < deadline:
                    continue
                break
            method = message.get("method")
            params = message.get("params") or {}
            if method in {"Target.targetCreated", "Target.targetInfoChanged"}:
                target_info = params.get("targetInfo") or {}
                asset_url = _pdf_viewer_asset_url(
                    str(target_info.get("url") or ""), expected_pii
                )
                target_id = str(target_info.get("targetId") or "")
                if (
                    target_id
                    and target_id not in ignored_target_ids
                    and asset_url
                ):
                    viewer_targets[target_id] = asset_url
                continue
            if message.get("sessionId") != session_id:
                continue
            if method == "Fetch.requestPaused":
                request = params.get("request") or {}
                request_url = str(request.get("url") or "")
                request_id = str(params.get("requestId") or "")
                response_stage = (
                    "responseStatusCode" in params
                    or "responseErrorReason" in params
                )
                asset_host = (
                    urllib.parse.urlsplit(request_url).hostname or ""
                ).casefold()
                expected_asset = (
                    asset_host == "pdf.sciencedirectassets.com"
                    or asset_host.endswith(".pdf.sciencedirectassets.com")
                ) and expected_pii.casefold() in urllib.parse.unquote(
                    request_url
                ).casefold()
                if not request_id:
                    continue
                if not response_stage:
                    if not expected_asset:
                        if not _continue_fetch_request(
                            mux, session_id, request_id
                        ):
                            _abort_fetch_response(
                                mux,
                                session_id,
                                request_id,
                                stream_taken=False,
                            )
                        continue
                    try:
                        # Scope Range to the observed PDF CDN request.  The
                        # WebVPN /pdfft page, challenge scripts and unrelated
                        # assets keep their original request semantics.
                        mux.call(
                            "Fetch.continueRequest",
                            {
                                "requestId": request_id,
                                "headers": _request_headers_with_full_range(
                                    request
                                ),
                            },
                            session_id=session_id,
                            timeout=min(
                                6,
                                max(1, deadline - time.monotonic()),
                            ),
                        )
                    except BrowserLoginError:
                        _abort_fetch_response(
                            mux,
                            session_id,
                            request_id,
                            stream_taken=False,
                        )
                        schedule_capture_retry()
                    continue
                if not expected_asset:
                    if not _continue_fetch_response(
                        mux, session_id, request_id
                    ):
                        _abort_fetch_response(
                            mux,
                            session_id,
                            request_id,
                            stream_taken=False,
                        )
                    continue
                fetch_status = int(params.get("responseStatusCode") or 0)
                fetch_content_type = _fetch_header(params, "content-type")
                fetch_content_length = _fetch_header(
                    params, "content-length"
                )
                pdf_like_response = fetch_status in {200, 206} and (
                    "application/pdf" in fetch_content_type.casefold()
                    or urllib.parse.urlsplit(request_url).path.casefold().endswith(
                        ".pdf"
                    )
                )
                if not pdf_like_response:
                    if not _continue_fetch_response(
                        mux, session_id, request_id
                    ):
                        _abort_fetch_response(
                            mux,
                            session_id,
                            request_id,
                            stream_taken=False,
                        )
                    continue
                fetch_response_count += 1
                fetch_bytes = 0
                fetch_stream_complete = False
                fetch_body_complete = False
                fetch_signature_valid = False
                fetch_eof_valid = False
                held_fetch_requests.append((session_id, request_id))
                candidate = b""
                expected_response_bytes = _fetch_expected_body_bytes(
                    fetch_status, params
                )
                fetch_expected_bytes = expected_response_bytes
                try:
                    streamed = mux.call(
                        "Fetch.takeResponseBodyAsStream",
                        {"requestId": request_id},
                        session_id=session_id,
                        timeout=min(6, max(1, deadline - time.monotonic())),
                    )
                    taken_fetch_requests.add((session_id, request_id))
                    handle = str(streamed.get("stream") or "")
                    if handle:
                        candidate, fetch_bytes, fetch_stream_complete = _read_cdp_stream(
                            mux,
                            session_id,
                            handle,
                            max_bytes=max_bytes,
                            deadline=deadline,
                            expected_bytes=expected_response_bytes,
                        )
                        fetch_signature_valid = candidate.startswith(b"%PDF-")
                        fetch_eof_valid = b"%%EOF" in candidate[-8192:]
                        fetch_body_complete = (
                            fetch_stream_complete
                            and _fetch_body_is_complete(
                                fetch_status, params, fetch_bytes
                            )
                        )
                        if (
                            fetch_body_complete
                            and _valid_pdf(candidate, max_bytes)
                        ):
                            fetch_content = candidate
                            break
                except BrowserLoginError:
                    candidate = b""
                _abort_fetch_response(
                    mux,
                    session_id,
                    request_id,
                    stream_taken=(session_id, request_id)
                    in taken_fetch_requests,
                )
                try:
                    held_fetch_requests.remove((session_id, request_id))
                except ValueError:
                    pass
                taken_fetch_requests.discard((session_id, request_id))
                schedule_capture_retry()
            elif method == "Page.loadEventFired":
                page_loaded_at = time.monotonic()
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                mime = str(response.get("mimeType") or "").casefold()
                url_value = str(response.get("url") or "")
                status = int(float(response.get("status") or 0))
                if status in {200, 206} and (
                    "application/pdf" in mime
                    or ".pdf"
                    in urllib.parse.urlsplit(url_value).path.casefold()
                ):
                    pdf_responses.append(
                        (str(params.get("requestId") or ""), response)
                    )
            elif method == "Network.loadingFinished":
                completed.add(str(params.get("requestId") or ""))
                if (
                    not fetch_enabled
                    and any(
                        item_id in completed
                        for item_id, _response in pdf_responses
                    )
                ):
                    break
    finally:
        for held_session, request_id in held_fetch_requests:
            _abort_fetch_response(
                mux,
                held_session,
                request_id,
                stream_taken=(held_session, request_id)
                in taken_fetch_requests,
            )
        if fetch_enabled:
            try:
                mux.call(
                    "Fetch.disable", session_id=session_id, timeout=3
                )
            except BrowserLoginError:
                pass
    return {
        "pdf_responses": pdf_responses,
        "completed": completed,
        "viewer_targets": viewer_targets,
        "fetch_content": fetch_content,
        "fetch_response_count": fetch_response_count,
        "fetch_status": fetch_status,
        "fetch_content_type": fetch_content_type,
        "fetch_content_length": fetch_content_length,
        "fetch_expected_bytes": fetch_expected_bytes,
        "fetch_bytes": fetch_bytes,
        "fetch_stream_complete": fetch_stream_complete,
        "fetch_body_complete": fetch_body_complete,
        "navigation_retry_count": navigation_retry_count,
        "fetch_signature_valid": fetch_signature_valid,
        "fetch_eof_valid": fetch_eof_valid,
    }


def _response_body(cdp: CdpClient, session_id: str, request_id: str) -> bytes:
    try:
        body = cdp.call(
            "Network.getResponseBody",
            {"requestId": request_id},
            session_id=session_id,
        )
    except BrowserLoginError:
        return b""
    raw = body.get("body") or ""
    try:
        if body.get("base64Encoded"):
            return base64.b64decode(raw)
        return str(raw).encode("utf-8", errors="strict")
    except (binascii.Error, TypeError, UnicodeError, ValueError):
        return b""


def _valid_pdf(content: bytes, max_bytes: int) -> bool:
    return (
        1024 <= len(content) <= max_bytes
        and content.startswith(b"%PDF-")
        and b"%%EOF" in content[-8192:]
    )


def _downloaded_pdf(directory: Path, max_bytes: int) -> bytes:
    for path in sorted(
        directory.glob("*.pdf"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    ):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if _valid_pdf(content, max_bytes):
            return content
    return b""


def _expected_pii(pdf_url: str) -> str:
    path = urllib.parse.urlsplit(pdf_url).path
    marker = "/pii/"
    if marker not in path.casefold():
        return ""
    tail = path[path.casefold().index(marker) + len(marker) :]
    return tail.split("/", 1)[0].strip()


def _pdf_viewer_asset_url(target_url: str, expected_pii: str) -> str:
    """Extract a browser-observed ScienceDirect PDF URL without guessing it."""
    parsed = urllib.parse.urlsplit(target_url)
    if parsed.scheme.casefold() != "chrome-extension":
        return ""
    candidate = ""
    raw_query = parsed.query
    for part in raw_query.split("&"):
        raw_name, separator, raw_value = part.partition("=")
        if separator and urllib.parse.unquote(raw_name).casefold() == "file":
            # Decode exactly the viewer's outer layer. ``parse_qs`` uses
            # unquote_plus and can silently turn a signed asset's literal "+"
            # into a space. Nested escapes such as %252F must remain %2F.
            candidate = urllib.parse.unquote(raw_value).strip()
            break
    if not candidate:
        raw_path = parsed.path.lstrip("/")
        embedded_path = raw_path
        if not embedded_path.startswith(("https://", "http://")):
            embedded_path = urllib.parse.unquote(raw_path)
        if embedded_path.startswith(("https://", "http://")):
            candidate = embedded_path
            if parsed.query:
                candidate += "?" + parsed.query
    asset = urllib.parse.urlsplit(candidate)
    host = (asset.hostname or "").casefold()
    if asset.scheme.casefold() != "https" or not (
        host == "pdf.sciencedirectassets.com"
        or host.endswith(".pdf.sciencedirectassets.com")
    ):
        return ""
    if not asset.path.casefold().endswith(".pdf"):
        return ""
    if expected_pii and expected_pii.casefold() not in urllib.parse.unquote(
        candidate
    ).casefold():
        return ""
    return candidate


def _save_pdf(output_path: Path, content: bytes, *, overwrite: bool) -> None:
    if output_path.exists() and not overwrite:
        raise InstitutionBrowserError(f"Output already exists: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    runtime = runtime_dir()
    runtime.mkdir(parents=True, exist_ok=True)
    temporary = runtime / (
        f"institution-{os.getpid()}-{time.monotonic_ns()}.pdf.part"
    )
    try:
        temporary.write_bytes(content)
        try:
            os.replace(temporary, output_path)
        except OSError:
            output_path.write_bytes(content)
    finally:
        temporary.unlink(missing_ok=True)


def download_elsevier_pdf(
    *,
    landing_url: str,
    pdf_url: str,
    cookies: Iterable[Any],
    output_path: Path,
    overwrite: bool,
    timeout: float = 25,
    max_bytes: int = 120 * 1024 * 1024,
    browser: Path | None = None,
    direct_network: bool = False,
) -> dict[str, Any]:
    """Return a real PDF produced by the institution-authorized browser flow."""
    started = time.monotonic()
    browser = browser or find_browser()
    if browser is None:
        return {
            "ok": False,
            "error_type": "institution_browser_unavailable",
            "reason": "找不到本机 Chrome 或 Microsoft Edge",
        }
    if direct_network:
        landing_host = urllib.parse.urlsplit(landing_url).hostname
        pdf_host = urllib.parse.urlsplit(pdf_url).hostname
        if (landing_host in {"linkinghub.elsevier.com", "www.sciencedirect.com", "sciencedirect.com"}
                and pdf_host in {"www.sciencedirect.com", "sciencedirect.com"}
                and _expected_pii(landing_url) == _expected_pii(pdf_url)
                and _expected_pii(pdf_url)):
            landing_url = pdf_url.split("/pdfft", 1)[0].split("?", 1)[0]
    if urllib.parse.urlsplit(landing_url).hostname != urllib.parse.urlsplit(pdf_url).hostname:
        return {
            "ok": False,
            "error_type": "institution_browser_invalid_route",
            "reason": "机构文章页与 PDF 入口不属于同一 WebVPN 主机",
        }
    expected_pii = _expected_pii(pdf_url)
    if not expected_pii:
        return {
            "ok": False,
            "error_type": "institution_browser_invalid_route",
            "reason": "ScienceDirect PDF 入口缺少文章 PII",
        }

    window: ProfileBrowser | None = None
    cdp: CdpClient | None = None
    target_id = ""
    initial_target_ids: set[str] = set()
    owned_target_ids: set[str] = set()
    shared_browser = False
    route_host = ""
    with temporary_folder("elsevier-institution-") as downloads:
        try:
            # Use only the managed profile whose extension policy we control.
            websocket_url = ""
            if websocket_url:
                shared_browser = True
                cdp = CdpClient(websocket_url)
            else:
                window = launch_profile_browser(
                    "about:blank",
                    browser=browser,
                    extra_args=(
                        "--window-position=100,100",
                        "--window-size=900,700",
                        "--no-proxy-server",
                    ),
                    profile_dir=(browser_data_dir() / "campus-direct-profile") if direct_network else None,
                    startup_timeout=min(15, timeout),
                )
                cdp = window.cdp
            try:
                initial_target_ids = {
                    str(item.get("targetId") or "")
                    for item in (
                        cdp.call("Target.getTargets").get("targetInfos") or []
                    )
                    if item.get("targetId")
                }
            except BrowserLoginError:
                initial_target_ids = set()
            try:
                cdp.call("Target.setDiscoverTargets", {"discover": True})
            except BrowserLoginError:
                pass
            params = cookie_params(cookies)
            if params:
                cdp.call("Storage.setCookies", {"cookies": params})
            if not shared_browser:
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
            target_id = str(
                cdp.call("Target.createTarget", {"url": landing_url}).get("targetId")
                or ""
            )
            if target_id:
                owned_target_ids.add(target_id)
            attached = cdp.call(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = str(attached.get("sessionId") or "")
            if not session_id:
                raise InstitutionBrowserError("无法附加到机构 ScienceDirect 页面")
            cdp.call("Page.enable", session_id=session_id)
            cdp.call(
                "Network.enable",
                {
                    "maxTotalBufferSize": max_bytes + 8 * 1024 * 1024,
                    "maxResourceBufferSize": max_bytes + 4 * 1024 * 1024,
                },
                session_id=session_id,
            )
            # Let WEngine/ScienceDirect establish their JavaScript and cookie
            # state before entering /pdfft.
            landing_mux = _CdpMux(cdp)
            landing_deadline = time.monotonic() + min(8.0, max(3.0, timeout / 4))
            while time.monotonic() < landing_deadline:
                _check_content_error(landing_mux, session_id)
                if direct_network and _has_human_challenge(landing_mux, session_id):
                    _wait_for_human_challenge(landing_mux, session_id)
                    break
                time.sleep(0.25)
            navigation = _send_navigation_and_collect(
                cdp,
                session_id,
                pdf_url,
                timeout=max(5, timeout - (time.monotonic() - started)),
                expected_pii=expected_pii,
                ignored_target_ids=initial_target_ids,
                max_bytes=max_bytes,
                allow_human_verification=direct_network,
            )
            responses = navigation["pdf_responses"]
            completed = navigation["completed"]
            viewer_targets = navigation["viewer_targets"]
            # The source tab can hand the PDF off to Chrome's built-in Viewer
            # in a second target.  Only targets that appeared after our initial
            # snapshot can belong to this download; remember them so the outer
            # cleanup closes the complete current-item tab chain.
            owned_target_ids.update(
                set(viewer_targets).difference(initial_target_ids)
            )
            try:
                for target_info in (
                    cdp.call("Target.getTargets").get("targetInfos") or []
                ):
                    discovered_id = str(target_info.get("targetId") or "")
                    if not discovered_id or discovered_id in initial_target_ids:
                        continue
                    asset_url = _pdf_viewer_asset_url(
                        str(target_info.get("url") or ""), expected_pii
                    )
                    if asset_url:
                        viewer_targets[discovered_id] = asset_url
                        owned_target_ids.add(discovered_id)
            except BrowserLoginError:
                pass
            content = navigation["fetch_content"]
            capture_method = (
                "browser_fetch_response" if content else "network_response"
            )
            if content:
                route_host = "pdf.sciencedirectassets.com"
            else:
                for request_id, response in reversed(responses):
                    if request_id and request_id in completed:
                        candidate = _response_body(cdp, session_id, request_id)
                        if _valid_pdf(candidate, max_bytes):
                            content = candidate
                            route_host = urllib.parse.urlsplit(
                                str(response.get("url") or "")
                            ).hostname or ""
                            break
            if not content:
                content = _downloaded_pdf(downloads, max_bytes)
                if content:
                    capture_method = "browser_download"
            if not content:
                page_state: dict[str, Any] = {}
                try:
                    evaluated = cdp.call(
                        "Runtime.evaluate",
                        {
                            "expression": (
                                "JSON.stringify({title:document.title,"
                                "scheme:location.protocol,"
                                "challenge:/challenge-platform|captcha/i.test("
                                "document.documentElement.innerHTML)})"
                            ),
                            "returnByValue": True,
                        },
                        session_id=session_id,
                    )
                    raw_state = ((evaluated.get("result") or {}).get("value"))
                    if isinstance(raw_state, str):
                        page_state = json.loads(raw_state)
                except (BrowserLoginError, ValueError):
                    page_state = {}
                fetch_bytes = int(navigation["fetch_bytes"] or 0)
                fetch_expected_bytes = int(
                    navigation["fetch_expected_bytes"] or 0
                )
                stream_complete = bool(navigation["fetch_stream_complete"])
                if max(fetch_bytes, fetch_expected_bytes) > max_bytes:
                    error_type = "institution_browser_pdf_too_large"
                    reason = (
                        "机构浏览器已收到 ScienceDirect PDF 响应，但文件超过"
                        f" {max_bytes / 1024 / 1024:.0f} MiB 大小上限"
                    )
                elif navigation["fetch_response_count"] and not stream_complete:
                    error_type = "institution_browser_stream_timeout"
                    read_detail = (
                        f"已读取 {fetch_bytes / 1024 / 1024:.1f} MiB"
                        if fetch_bytes
                        else "尚未取得正文数据"
                    )
                    reason = (
                        "机构浏览器已收到 ScienceDirect PDF 响应，但在"
                        f" {timeout:g} 秒内未读取完成（{read_detail}）"
                    )
                elif direct_network and _has_human_challenge(_CdpMux(cdp), session_id) is True:
                    error_type = "human_verification_required"
                    reason = "网站仍要求人工验证；已保留直连浏览器配置，未自动重试或刷新验证页"
                elif navigation["fetch_response_count"] and stream_complete:
                    error_type = "institution_browser_invalid_pdf"
                    reason = "机构浏览器收到的 ScienceDirect 响应未通过 PDF 完整性校验"
                else:
                    error_type = "institution_browser_no_pdf"
                    reason = "机构浏览器执行了 ScienceDirect 下载页，但未收到有效 PDF"
                return {
                    "ok": False,
                    "error_type": error_type,
                    "reason": reason,
                    "elapsed_s": round(time.monotonic() - started, 2),
                    "pdf_response_count": len(responses),
                    "fetch_response_count": navigation["fetch_response_count"],
                    "fetch_status": navigation["fetch_status"],
                    "fetch_content_type": navigation["fetch_content_type"],
                    "fetch_content_length": navigation[
                        "fetch_content_length"
                    ],
                    "fetch_expected_bytes": navigation[
                        "fetch_expected_bytes"
                    ],
                    "fetch_bytes": navigation["fetch_bytes"],
                    "fetch_stream_complete": navigation[
                        "fetch_stream_complete"
                    ],
                    "fetch_body_complete": navigation[
                        "fetch_body_complete"
                    ],
                    "navigation_retry_count": navigation[
                        "navigation_retry_count"
                    ],
                    "fetch_signature_valid": navigation[
                        "fetch_signature_valid"
                    ],
                    "fetch_eof_valid": navigation["fetch_eof_valid"],
                    "pdf_viewer_count": len(viewer_targets),
                    "page_state": page_state,
                }
            _save_pdf(output_path, content, overwrite=overwrite)
            return {
                "ok": True,
                "method": "institution_sciencedirect_browser",
                "path": str(output_path.resolve()),
                "bytes": len(content),
                "elapsed_s": round(time.monotonic() - started, 2),
                "resolved_host": route_host,
                "dynamic_cdn": bool(route_host and route_host != urllib.parse.urlsplit(pdf_url).hostname),
                "capture_method": capture_method,
                "fetch_response_count": navigation["fetch_response_count"],
                "fetch_status": navigation["fetch_status"],
                "fetch_content_type": navigation["fetch_content_type"],
                "fetch_content_length": navigation["fetch_content_length"],
                "fetch_expected_bytes": navigation["fetch_expected_bytes"],
                "fetch_bytes": navigation["fetch_bytes"],
                "fetch_stream_complete": navigation["fetch_stream_complete"],
                "fetch_body_complete": navigation["fetch_body_complete"],
                "navigation_retry_count": navigation[
                    "navigation_retry_count"
                ],
                "fetch_signature_valid": navigation["fetch_signature_valid"],
                "fetch_eof_valid": navigation["fetch_eof_valid"],
                "shared_browser": shared_browser,
            }
        except InstitutionSessionExpired as exc:
            return {"ok": False, "error_type": "session_expired",
                    "reason": str(exc), "elapsed_s": round(time.monotonic() - started, 2)}
        except ContentProviderError as exc:
            return {"ok": False, "error_type": "elsevier_content_error",
                    "reason": str(exc), "elapsed_s": round(time.monotonic() - started, 2)}
        except Exception as exc:
            return {
                "ok": False,
                "error_type": "institution_browser_failed",
                "reason": f"{type(exc).__name__}: {exc}",
                "elapsed_s": round(time.monotonic() - started, 2),
            }
        finally:
            if cdp is not None:
                for owned_target_id in owned_target_ids:
                    try:
                        cdp.call(
                            "Target.closeTarget",
                            {"targetId": owned_target_id},
                        )
                    except Exception:
                        pass
            if window is not None:
                window.close()
            elif cdp is not None:
                cdp.close()
