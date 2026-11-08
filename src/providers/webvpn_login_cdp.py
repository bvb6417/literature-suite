#!/usr/bin/env python3
"""WebVPN login using an installed Chrome/Edge and local CDP.

The browser is only used for the institutional login. Once the cookies are
captured and validated, all article downloads remain HTTP-only. When the school
profile has ``auto_login`` selectors and the user configured an account, the
login form is filled and submitted once; CAPTCHAs are always left to the user.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .app_paths import browser_profile_dir, state_dir


class BrowserLoginError(RuntimeError):
    pass


def application_state_dir() -> Path:
    return state_dir()


DEFAULT_COOKIE_FILE = application_state_dir() / "webvpn_cookies.json"

# HTTP cache ceiling for the persistent profile.
PROFILE_DISK_CACHE_BYTES = 50 * 1024 * 1024
# Model/hint downloads are the largest part of a fresh profile and are never
# needed for logging in or fetching PDFs.
PROFILE_DISABLED_FEATURES = (
    "OptimizationGuideModelDownloading",
    "OptimizationGuideOnDeviceModel",
    "OptimizationHints",
    "OptimizationHintsFetching",
    "OptimizationTargetPrediction",
)


def find_browser() -> Path | None:
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(
            [
                Path(local_app_data) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ]
        )
    candidates.extend(
        [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]
    )
    return next((candidate for candidate in candidates if candidate.exists()), None)


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def local_http_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def wait_for_cdp(port: int, process: subprocess.Popen[Any], timeout: float = 20) -> str:
    endpoint = f"http://127.0.0.1:{port}/json/version"
    session = local_http_session()
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserLoginError(
                f"浏览器在 CDP 启动前退出，退出码 {process.returncode}"
            )
        try:
            response = session.get(endpoint, timeout=2)
            if response.status_code == 200:
                websocket_url = str(response.json().get("webSocketDebuggerUrl") or "")
                if websocket_url:
                    return websocket_url
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    raise BrowserLoginError(f"无法连接本机浏览器 CDP：{last_error or '启动超时'}")


class CdpClient:
    def __init__(self, websocket_url: str) -> None:
        try:
            import websocket
        except ImportError as exc:
            raise BrowserLoginError(
                "缺少 websocket-client：python -m pip install websocket-client"
            ) from exc
        try:
            self.socket = websocket.create_connection(
                websocket_url,
                timeout=6,
                suppress_origin=True,
            )
        except Exception as exc:
            raise BrowserLoginError(f"无法连接浏览器调试通道：{exc}") from exc
        self._next_id = 1

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id
        self.socket.send(json.dumps(payload, separators=(",", ":")))
        while True:
            try:
                message = json.loads(self.socket.recv())
            except Exception as exc:
                raise BrowserLoginError(f"浏览器调试通道读取失败：{exc}") from exc
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise BrowserLoginError(
                    f"CDP {method} 失败：{message['error'].get('message', message['error'])}"
                )
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        try:
            self.socket.close()
        except Exception:
            pass


class _ProfileLock:
    """Cross-process lock so only one flow drives the persistent profile."""

    def __init__(self, path: Path, timeout: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(path, "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                self._lock()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    raise BrowserLoginError(
                        "浏览器配置正被另一个下载器任务使用，请等待其结束后重试"
                    )
                time.sleep(0.5)

    def _lock(self) -> None:
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._handle.closed:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()


def _running_profile_websocket(profile: Path) -> str:
    """CDP endpoint of a browser still running on ``profile`` (e.g. left over
    by a killed download worker), verified against DevToolsActivePort."""
    try:
        lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        browser_path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return ""
    session = local_http_session()
    try:
        response = session.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
        websocket_url = str(response.json().get("webSocketDebuggerUrl") or "")
    except (requests.RequestException, ValueError):
        return ""
    finally:
        session.close()
    if urllib.parse.urlsplit(websocket_url).path != browser_path:
        return ""
    return websocket_url


def _wait_for_profile_websocket(
    profile: Path, process: subprocess.Popen[Any], timeout: float
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserLoginError(
                f"浏览器在 CDP 启动前退出，退出码 {process.returncode}"
            )
        websocket_url = _running_profile_websocket(profile)
        if websocket_url:
            return websocket_url
        time.sleep(0.25)
    raise BrowserLoginError("无法连接本机浏览器 CDP：启动超时")


class ProfileBrowser:
    """A Chrome/Edge instance on the persistent profile.

    The profile keeps the institution's "remember me" state, saved passwords
    and publisher verification cookies across runs, so later logins usually
    finish without typing anything.
    """

    def __init__(
        self,
        *,
        browser: Path,
        cdp: CdpClient,
        profile: Path,
        process: subprocess.Popen[Any] | None,
        lock: _ProfileLock,
    ) -> None:
        self.browser = browser
        self.cdp = cdp
        self.profile = profile
        self.process = process
        self._lock = lock

    @property
    def attached(self) -> bool:
        return self.process is None

    def alive(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        try:
            self.cdp.call("Browser.getVersion")
            return True
        except BrowserLoginError:
            return False

    def close(self) -> None:
        try:
            self.cdp.call("Browser.close")
        except Exception:
            pass
        self.cdp.close()
        if self.process is not None:
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        else:
            # Re-attached instance: wait until it has really shut down so the
            # next run launches a fresh browser instead of attaching to it.
            deadline = time.monotonic() + 8
            while _running_profile_websocket(self.profile) and time.monotonic() < deadline:
                time.sleep(0.25)
        (self.profile / "DevToolsActivePort").unlink(missing_ok=True)
        self._lock.release()

    def __enter__(self) -> "ProfileBrowser":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def launch_profile_browser(
    start_url: str,
    *,
    browser: Path | None = None,
    extra_args: Iterable[str] = (),
    startup_timeout: float = 20,
    lock_timeout: float = 60,
    profile_dir: Path | None = None,
) -> ProfileBrowser:
    """Open ``start_url`` in the persistent profile, reusing a running instance."""
    browser = browser or find_browser()
    if browser is None:
        raise BrowserLoginError("找不到本机 Chrome 或 Microsoft Edge")
    profile = profile_dir or browser_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    lock = _ProfileLock(profile.parent / (profile.name + ".lock" if profile_dir else "profile.lock"), lock_timeout)
    try:
        websocket_url = _running_profile_websocket(profile)
        if websocket_url:
            cdp = CdpClient(websocket_url)
            try:
                arguments = cdp.call("Browser.getBrowserCommandLine").get("arguments", [])
                if "--disable-extensions" not in arguments:
                    raise BrowserLoginError("下载器专用浏览器仍按旧设置运行，请关闭该浏览器窗口后重试，以应用禁用扩展设置")
                cdp.call("Target.createTarget", {"url": start_url})
            except BrowserLoginError:
                cdp.close()
                raise
            return ProfileBrowser(
                browser=browser, cdp=cdp, profile=profile, process=None, lock=lock
            )

        # Port 0 lets Chrome pick a free port and record it in
        # DevToolsActivePort, which is also how a later run re-attaches.
        (profile / "DevToolsActivePort").unlink(missing_ok=True)
        command = [
            str(browser),
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--enable-automation",
            "--disable-extensions",
            "--no-default-browser-check",
            "--hide-crash-restore-bubble",
            f"--disk-cache-size={PROFILE_DISK_CACHE_BYTES}",
            "--disable-features=" + ",".join(PROFILE_DISABLED_FEATURES),
            *extra_args,
            "--new-window",
            start_url,
        ]
        creation_flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        if os.name == "nt":
            creation_flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        try:
            cdp = CdpClient(_wait_for_profile_websocket(profile, process, startup_timeout))
        except BrowserLoginError as exc:
            if process.poll() is None:
                process.terminate()
            elif process.returncode == 0:
                # Chrome hands the command line to an instance already using
                # this profile (one opened by hand) and exits immediately.
                raise BrowserLoginError(
                    f"浏览器配置 {profile} 正被一个手动打开的浏览器窗口占用，"
                    "请关闭该窗口后重试"
                ) from exc
            raise
        return ProfileBrowser(
            browser=browser, cdp=cdp, profile=profile, process=process, lock=lock
        )
    except BaseException:
        lock.release()
        raise


def relevant_cookies(cookies: list[dict[str, Any]], base_url: str) -> list[dict[str, Any]]:
    hostname = (urllib.parse.urlsplit(base_url).hostname or "").lower()
    labels = hostname.split(".")
    institutional_suffix = ".".join(labels[-3:]) if len(labels) >= 3 else hostname
    output: list[dict[str, Any]] = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if (
            domain != hostname
            and domain != institutional_suffix
            and not domain.endswith("." + institutional_suffix)
        ):
            continue
        if not cookie.get("name") or cookie.get("value") is None:
            continue
        output.append(
            {
                key: cookie[key]
                for key in (
                    "name",
                    "value",
                    "domain",
                    "path",
                    "expires",
                    "httpOnly",
                    "secure",
                    "sameSite",
                )
                if key in cookie
            }
        )
    return output


def atomic_cookie_write(path: Path, cookies: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    try:
        temporary.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


LOG = logging.getLogger(__name__)

# Runs in the login page. Selectors come from schools.json; the account and
# password arrive as call arguments, never as part of the script text.
AUTO_FILL_FUNCTION = r"""
async function (cfg, account, secret) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const one = (selector) => (selector ? document.querySelector(selector) : null);
  const all = (selector) => (selector ? Array.from(document.querySelectorAll(selector)) : []);
  const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const captchaShown = () => all(cfg.captcha).some((el) => visible(el) && el.innerHTML.trim() !== "");
  const fill = (el, value) => {
    el.removeAttribute("readonly");
    el.focus();
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  if (document.readyState !== "complete") return { state: "loading" };
  let user = one(cfg.username);
  if (!visible(user) && cfg.password_tab) {
    const tab = one(cfg.password_tab);
    if (tab) { tab.click(); await sleep(800); }
    user = one(cfg.username);
  }
  const pass = one(cfg.password);
  const submit = one(cfg.submit);
  if (!user || !pass || !submit) return { state: "fields_missing" };
  fill(user, account);
  user.dispatchEvent(new Event("focusout", { bubbles: true }));
  user.blur();
  await sleep(Math.max(0, Number(cfg.username_settle_seconds) || 0) * 1000);
  fill(pass, secret);
  pass.blur();
  if (captchaShown()) return { state: "captcha" };
  if (cfg.remember_me) {
    const box = one(cfg.remember_me);
    if (box && !box.checked) box.click();
  }
  submit.click();
  await sleep(1500);
  return { state: captchaShown() ? "captcha" : "submitted" };
}
"""

# Reads the page state after submission (wrong password, CAPTCHA shown).
AUTO_FILL_STATUS_FUNCTION = r"""
function (cfg) {
  const all = (selector) => (selector ? Array.from(document.querySelectorAll(selector)) : []);
  const visible = (el) => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const error = all(cfg.error).map((el) => (visible(el) ? el.innerText.trim() : "")).filter(Boolean).join(" ");
  const captcha = all(cfg.captcha).some((el) => visible(el) && el.innerHTML.trim() !== "");
  return { error: error.slice(0, 200), captcha: captcha };
}
"""

AUTO_FILL_MESSAGES = {
    "submitted": "已自动填写账号密码并提交，等待学校确认登录",
    "captcha": "登录页要求验证码，请在浏览器窗口中完成验证后登录",
    "fields_missing": "登录页上找不到 schools.json 配置的账号 / 密码 / 登录按钮，请手动登录并核对 auto_login 字段",
}


def _login_page_session(cdp: CdpClient, marker: str) -> str | None:
    """Attach to the tab currently showing the institution login page."""
    for target in cdp.call("Target.getTargets").get("targetInfos") or []:
        url = str(target.get("url") or "")
        if target.get("type") == "page" and (not marker or marker in url):
            attached = cdp.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
            return attached.get("sessionId")
    return None


def _call_in_page(cdp: CdpClient, session_id: str, function: str, *arguments: Any) -> dict[str, Any]:
    """Call ``function`` in the page with JSON arguments; returns its value."""
    root = cdp.call("Runtime.evaluate", {"expression": "globalThis"}, session_id=session_id)
    object_id = (root.get("result") or {}).get("objectId")
    if not object_id:
        raise BrowserLoginError("无法访问登录页的脚本环境")
    result = cdp.call(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": function,
            "arguments": [{"value": value} for value in arguments],
            "awaitPromise": True,
            "returnByValue": True,
        },
        session_id=session_id,
    )
    if result.get("exceptionDetails"):
        raise BrowserLoginError("登录页脚本执行失败：" + str((result["exceptionDetails"].get("exception") or {}).get("description") or "")[:120])
    value = (result.get("result") or {}).get("value")
    return value if isinstance(value, dict) else {}


def login_with_installed_browser(
    *,
    base_url: str,
    cookie_file: Path,
    timeout: float,
    validator: Callable[[Path], dict[str, Any]],
    browser: Path | None = None,
    auto_fill: dict[str, Any] | None = None,
) -> dict[str, Any]:
    browser = browser or find_browser()
    if browser is None:
        raise BrowserLoginError("找不到本机 Chrome 或 Microsoft Edge")
    if timeout <= 0:
        raise BrowserLoginError("登录等待时间必须大于 0 秒")

    state_dir = application_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    candidate = state_dir / "webvpn_cookies.candidate.json"

    with launch_profile_browser(
        base_url, browser=browser, extra_args=("--no-proxy-server",)
    ) as window:
        cdp = window.cdp
        # Automatic form filling: one submission at most, so a wrong
        # password never turns into repeated attempts against the account.
        fill_cfg = {key: value for key, value in (auto_fill or {}).items() if key not in {"account", "secret"}}
        fill_state = "configured" if auto_fill else "not_configured"
        try:
            deadline = time.monotonic() + timeout
            last_signature = ""
            last_validation = 0.0
            last_status: dict[str, Any] = {
                "valid": False,
                "state": "waiting",
                "message": "等待登录（自动填写或人工登录）" if auto_fill else "等待人工登录",
            }
            confirmation_signature = ""
            confirmation_started = 0.0
            fill_done = not auto_fill
            fill_session: str | None = None
            fill_checked = 0.0
            opened = time.monotonic()
            while time.monotonic() < deadline:
                if not window.alive():
                    raise BrowserLoginError("登录窗口已关闭，但尚未检测到有效登录")
                now = time.monotonic()
                if not fill_done and now - opened >= 2.0:
                    try:
                        fill_session = fill_session or _login_page_session(
                            cdp, str(fill_cfg.get("login_url_contains") or "")
                        )
                        if fill_session:
                            outcome = _call_in_page(
                                cdp, fill_session, AUTO_FILL_FUNCTION,
                                fill_cfg, auto_fill["account"], auto_fill["secret"],
                            )
                            state = str(outcome.get("state") or "")
                            if state and state != "loading":
                                fill_done = True
                                fill_state = state
                                last_status = {**last_status, "message": AUTO_FILL_MESSAGES.get(state, state)}
                                LOG.info("%s", last_status["message"])
                    except BrowserLoginError as exc:
                        fill_session = None
                        if not any(word in str(exc) for word in ("navigated", "closed", "No target", "detached")):
                            fill_done = True
                            fill_state = "error"
                            LOG.warning("自动填写登录表单失败，请在窗口中手动登录：%s", exc)
                        # Otherwise the page was mid-redirect (often the SSO session
                        # logging in by itself); look again on the next tick.
                elif fill_session and fill_state in {"submitted", "captcha"} and now - fill_checked >= 3.0:
                    fill_checked = now
                    try:
                        page = _call_in_page(cdp, fill_session, AUTO_FILL_STATUS_FUNCTION, fill_cfg)
                    except BrowserLoginError:
                        page = {}
                    if page.get("error"):
                        fill_state = "rejected"
                        last_status = {**last_status, "message": "学校登录页提示：" + str(page["error"])}
                        LOG.warning("%s（不会自动重试，请在窗口中处理）", last_status["message"])
                    elif page.get("captcha") and fill_state == "submitted":
                        fill_state = "captcha"
                        last_status = {**last_status, "message": AUTO_FILL_MESSAGES["captcha"]}
                        LOG.warning("%s", last_status["message"])
                result = cdp.call("Storage.getCookies")
                cookies = relevant_cookies(result.get("cookies") or [], base_url)
                signature_source = "\n".join(
                    sorted(
                        f"{item.get('domain')}:{item.get('name')}:{item.get('value', '')}"
                        for item in cookies
                    )
                )
                signature = hashlib.sha256(
                    signature_source.encode("utf-8")
                ).hexdigest()
                now = time.monotonic()
                should_validate = (
                    cookies
                    and (
                        signature != last_signature
                        or now - last_validation >= 5.0
                    )
                )
                if should_validate:
                    last_signature = signature
                    last_validation = now
                    atomic_cookie_write(candidate, cookies)
                    last_status = validator(candidate)
                    if last_status.get("valid"):
                        # Require the same cookie set to pass twice.  Login
                        # pages commonly issue an anonymous session cookie as
                        # soon as they open; a single successful-looking probe
                        # must not close the browser prematurely.
                        if signature != confirmation_signature:
                            confirmation_signature = signature
                            confirmation_started = now
                        elif now - confirmation_started >= 3.0:
                            atomic_cookie_write(cookie_file, cookies)
                            return {
                                "ok": True,
                                "method": "installed_browser_cdp",
                                "browser": browser.name,
                                "cookie_file": str(cookie_file.resolve()),
                                "cookie_count": len(cookies),
                                "auto_fill": fill_state,
                                "session": last_status,
                            }
                    else:
                        confirmation_signature = ""
                        confirmation_started = 0.0
                time.sleep(1.0)
            raise BrowserLoginError(
                f"登录等待超时（{int(timeout)} 秒）：{last_status.get('message', '未检测到有效会话')}"
            )
        except BrowserLoginError as exc:
            exc.auto_fill = fill_state  # type: ignore[attr-defined]
            raise
        finally:
            candidate.unlink(missing_ok=True)
