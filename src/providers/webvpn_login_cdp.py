#!/usr/bin/env python3
"""Manual WebVPN login using an installed Chrome/Edge and local CDP.

The browser is only used for the interactive institutional login. Once the
cookies are captured and validated, all article downloads remain HTTP-only.
"""

from __future__ import annotations

import json
import hashlib
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


def login_with_installed_browser(
    *,
    base_url: str,
    cookie_file: Path,
    timeout: float,
    validator: Callable[[Path], dict[str, Any]],
    browser: Path | None = None,
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
        try:
            deadline = time.monotonic() + timeout
            last_signature = ""
            last_validation = 0.0
            last_status: dict[str, Any] = {
                "valid": False,
                "state": "waiting",
                "message": "等待人工登录",
            }
            confirmation_signature = ""
            confirmation_started = 0.0
            while time.monotonic() < deadline:
                if not window.alive():
                    raise BrowserLoginError("登录窗口已关闭，但尚未检测到有效登录")
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
                                "session": last_status,
                            }
                    else:
                        confirmation_signature = ""
                        confirmation_started = 0.0
                time.sleep(1.0)
            raise BrowserLoginError(
                f"登录等待超时（{int(timeout)} 秒）：{last_status.get('message', '未检测到有效会话')}"
            )
        finally:
            candidate.unlink(missing_ok=True)
