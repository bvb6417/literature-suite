"""School catalog and institutional-access adapter dispatch.

School profiles contain public gateway/routing parameters only. Passwords and
session cookies stay in the browser/application state directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .webvpn_hhu import (
    DEFAULT_KEY,
    DownloadError,
    USER_AGENT,
    WebVPNClient,
    download_one,
    host_of,
    load_cookie_jar,
)
from .app_paths import relocate_legacy_path
from .webvpn_login_cdp import (
    BrowserLoginError,
    application_state_dir,
    login_with_installed_browser,
)


DEFAULT_SCHOOLS_PATH = Path(__file__).resolve().parents[1] / "schools.json"
SUPPORTED_ACCESS_TYPES = {"direct", "webvpn", "ezproxy", "easyconnect", "atrust"}
ACCESS_TYPE_LABELS = {
    "direct": "校园网直连",
    "webvpn": "WebVPN",
    "ezproxy": "EZProxy",
    "easyconnect": "EasyConnect",
    "atrust": "aTrust",
}
LOGIN_MARKERS = (
    "/authserver/login",
    "/cas/login",
    "/sso/login",
    "/users/sign_in",
    "/login",
    "/signin",
    "/sign-in",
    "/oauth/authorize",
    "/saml/",
)

LOGIN_HTML_MARKERS = (
    "统一身份认证",
    "统一认证",
    "用户登录",
    "账号登录",
    "扫码登录",
    "single sign-on",
    "sign in to",
)


class InstitutionalConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchoolEntry:
    id: str
    name: str
    province: str
    access_type: str
    base_url: str = ""
    key: bytes = b""
    iv: bytes = b""
    gateway: str = ""
    proxy_template: str = ""
    connector_url: str = ""
    cookie_file_name: str = ""
    notes: str = ""
    origin: str = "local"

    @property
    def type_label(self) -> str:
        return ACCESS_TYPE_LABELS.get(self.access_type, self.access_type)

    @property
    def display(self) -> str:
        if self.access_type == "direct":
            return "校园网直连（无需选择学校）"
        location = f"{self.province} · " if self.province else ""
        return f"{self.name} · {location}{self.type_label}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "province": self.province,
            "type": self.access_type,
            "type_label": self.type_label,
            "base_url": self.base_url,
            "gateway": self.gateway,
            "notes": self.notes,
            "origin": self.origin,
            "display": self.display,
        }


DIRECT_CAMPUS_ENTRY = SchoolEntry(
    id="campus-network-direct",
    name="校园网直连",
    province="",
    access_type="direct",
    notes=(
        "无需学校配置或登录；绕过 HTTP/HTTPS 环境代理，使用当前 Windows "
        "网络出口。机构权限以出版社实际返回为准。"
    ),
    origin="builtin",
)


def _stable_school_id(name: str, host: str) -> str:
    digest = hashlib.sha1(f"{name}\0{host}".encode("utf-8")).hexdigest()[:12]
    return f"catalog-{digest}"


def _entry_from_mapping(raw: dict[str, Any], *, origin: str) -> SchoolEntry:
    name = str(raw.get("name") or "").strip()
    access_type = str(raw.get("type") or raw.get("school_type") or "webvpn").strip().casefold()
    base_url = str(raw.get("base_url") or raw.get("host") or "").strip().rstrip("/")
    gateway = str(raw.get("gateway") or "").strip().rstrip("/")
    if not name:
        raise InstitutionalConfigError("学校条目缺少 name")
    if access_type not in SUPPORTED_ACCESS_TYPES:
        raise InstitutionalConfigError(
            f"学校 {name} 的接入类型不受支持：{access_type}"
        )
    if access_type in {"webvpn", "ezproxy"} and not base_url:
        raise InstitutionalConfigError(f"学校 {name} 缺少 base_url")
    if base_url and not base_url.startswith(("https://", "http://")):
        base_url = "https://" + base_url
    if gateway and not gateway.startswith(("https://", "http://")):
        gateway = "https://" + gateway
    if access_type in {"easyconnect", "atrust"} and not gateway:
        gateway = base_url
    key_value = raw.get("crypto_key") or raw.get("key") or DEFAULT_KEY
    iv_value = raw.get("crypto_iv") or raw.get("iv") or key_value
    key = key_value if isinstance(key_value, bytes) else str(key_value).encode("utf-8")
    iv = iv_value if isinstance(iv_value, bytes) else str(iv_value).encode("utf-8")
    entry_id = str(raw.get("id") or "").strip()
    if not entry_id:
        entry_id = _stable_school_id(name, base_url or gateway)
    return SchoolEntry(
        id=entry_id,
        name=name,
        province=str(raw.get("province") or "").strip(),
        access_type=access_type,
        base_url=base_url,
        key=key,
        iv=iv,
        gateway=gateway,
        proxy_template=str(raw.get("proxy_template") or "").strip(),
        connector_url=str(raw.get("connector_url") or "").strip(),
        cookie_file_name=str(raw.get("cookie_file_name") or "").strip(),
        notes=str(raw.get("notes") or "").strip(),
        origin=origin,
    )


def load_school_catalog(
    path: Path = DEFAULT_SCHOOLS_PATH,
) -> tuple[list[SchoolEntry], list[str]]:
    path = path.expanduser().resolve()
    warnings: list[str] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InstitutionalConfigError(f"无法读取学校配置库 {path}: {exc}") from exc
    else:
        payload = {"schools": []}
        warnings.append(f"学校配置库不存在：{path}")
    if not isinstance(payload, dict):
        raise InstitutionalConfigError("schools.json 顶层必须是 JSON 对象")

    merged: dict[str, SchoolEntry] = {}
    local_values = payload.get("schools") or []
    if not isinstance(local_values, list):
        raise InstitutionalConfigError("schools.json 的 schools 必须是数组")
    for index, raw in enumerate(local_values, start=1):
        if not isinstance(raw, dict) or raw.get("enabled") is False:
            continue
        try:
            entry = _entry_from_mapping(
                raw,
                origin=str(raw.get("origin") or "local").strip().casefold(),
            )
        except InstitutionalConfigError as exc:
            warnings.append(f"本地学校条目 #{index} 已跳过：{exc}")
            continue
        merged[entry.name.casefold()] = entry

    # This mode is built into the application and must remain the first choice.
    merged.pop(DIRECT_CAMPUS_ENTRY.name.casefold(), None)
    entries = [DIRECT_CAMPUS_ENTRY, *sorted(
        merged.values(),
        key=lambda entry: (entry.province, entry.name, entry.type_label),
    )]
    return entries, warnings


def find_school(entries: list[SchoolEntry], query: str) -> SchoolEntry:
    value = query.strip().casefold()
    if not value:
        raise InstitutionalConfigError("学校名称或 ID 不能为空")
    for entry in entries:
        if value in {entry.id.casefold(), entry.name.casefold(), entry.display.casefold()}:
            return entry
    matches = [
        entry
        for entry in entries
        if value in entry.name.casefold()
        or value in entry.province.casefold()
        or value in entry.base_url.casefold()
        or value in entry.gateway.casefold()
    ]
    if not matches:
        raise InstitutionalConfigError(f"学校配置库中未找到：{query}")
    matches.sort(key=lambda entry: (len(entry.name), entry.name))
    return matches[0]


def schools_path_from_config(config):
    from suite_paths import APP_DIR, RESOURCE_DIR
    settings = config.get('institution') or {}
    path = Path(str(settings.get('schools_file') or 'schools.json')).expanduser()
    if path.is_absolute():
        return path
    external = APP_DIR / path
    return external if external.exists() else RESOURCE_DIR / path


def selected_school(
    config: dict[str, Any],
) -> tuple[SchoolEntry, Path, list[str]]:
    catalog_path = schools_path_from_config(config).resolve()
    entries, warnings = load_school_catalog(catalog_path)
    settings = config.get("institution") or {}
    query = str(settings.get("school_id") or settings.get("school_name") or "校园网直连")
    try:
        entry = find_school(entries, query)
    except InstitutionalConfigError:
        # Catalog-owned IDs may change between bundled revisions. Preserve a
        # user's selection by matching the stable school name before failing.
        name = str(settings.get("school_name") or "").strip()
        if name and name.casefold() != query.casefold():
            try:
                entry = find_school(entries, name)
            except InstitutionalConfigError:
                entry = None
            if entry is not None:
                return entry, catalog_path, warnings
        raise InstitutionalConfigError(
            f"已选择的学校“{name or query}”不在配置库中；请修改 {catalog_path.name} 后刷新"
        )

    return entry, catalog_path, warnings


def cookie_file_for(entry: SchoolEntry, config: dict[str, Any]) -> Path:
    settings = config.get("institution") or {}
    explicit = relocate_legacy_path(settings.get("cookie_file") or "")
    if explicit:
        return Path(os.path.expandvars(explicit)).expanduser()
    if entry.name == "河海大学":
        legacy = config.get("webvpn") or {}
        legacy_cookie = relocate_legacy_path(legacy.get("cookie_file") or "")
        if legacy_cookie:
            return Path(os.path.expandvars(legacy_cookie)).expanduser()
    filename = entry.cookie_file_name or re.sub(r"[^A-Za-z0-9._-]+", "_", entry.id) + ".json"
    return application_state_dir() / "schools" / filename


@dataclass
class RoutedAccessClient:
    """HTTP client for direct campus routing, EZProxy and external VPNs."""

    base_url: str
    access_type: str
    cookie_file: Path | None = None
    proxy_template: str = ""
    connector_url: str = ""
    timeout: float = 30.0
    browser_timeout: float | None = None
    delay: float = 2.0
    max_bytes: int = 100 * 1024 * 1024
    verify_tls: bool = True
    elsevier_api_key: str | None = None
    elsevier_insttoken: str | None = None
    session: requests.Session = field(init=False)
    cookie_info: dict[str, Any] = field(init=False, default_factory=dict)
    _last_request: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            }
        )
        if self.access_type == "ezproxy":
            parsed = urllib.parse.urlsplit(self.base_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname:
                raise DownloadError("EZProxy base_url 必须是 http(s) URL")
            if self.cookie_file is None:
                raise DownloadError("EZProxy 缺少 Cookie 文件路径")
            jar, info = load_cookie_jar(self.cookie_file, parsed.hostname)
            self.session.cookies.update(jar)
            self.cookie_info = info
            self.session.trust_env = False
        elif self.access_type == "direct":
            # Campus-network entitlement depends on the machine's public source
            # IP.  HTTP(S)_PROXY/ALL_PROXY would replace it with the proxy's IP,
            # so this adapter deliberately uses the current Windows route only.
            self.session.trust_env = False
        else:
            self.session.trust_env = True
            if self.connector_url:
                self.session.proxies.update(
                    {"http": self.connector_url, "https": self.connector_url}
                )

    def proxify(self, url: str) -> str:
        if self.access_type != "ezproxy":
            return url
        if host_of(url) == host_of(self.base_url):
            return url
        encoded = urllib.parse.quote(url, safe="")
        if "{url}" in self.proxy_template:
            return self.proxy_template.replace("{url}", encoded)
        separator = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{separator}url={encoded}"

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
        response = self.session.get(
            self.proxify(url),
            timeout=self.timeout,
            allow_redirects=True,
            stream=stream,
            verify=self.verify_tls,
            headers=headers,
        )
        self._last_request = time.monotonic()
        return response

    # Reuse the thoroughly validated streaming/HTML handling. It dispatches
    # through this class's get(), so routing still differs per adapter.
    fetch = WebVPNClient.fetch


def make_direct_network_client(config: dict[str, Any]) -> RoutedAccessClient:
    """Create a cookie-free client using the machine's current network route."""
    network = config.get("network") or {}
    download = config.get("download") or {}
    keys = config.get("api_keys") or {}
    return RoutedAccessClient(
        base_url="",
        access_type="direct",
        timeout=float(network.get("download_timeout_seconds") or 90),
        browser_timeout=float(
            download.get("per_paper_timeout_seconds") or 120
        ),
        delay=float((config.get("institution") or {}).get("request_delay_seconds") or 2),
        max_bytes=int(download.get("max_file_mb") or 120) * 1024 * 1024,
        verify_tls=True,
        elsevier_api_key=str(keys.get("elsevier") or "").strip() or None,
        elsevier_insttoken=str(keys.get("elsevier_inst_token") or "").strip() or None,
    )


def direct_network_status() -> dict[str, Any]:
    """Describe direct-campus capability without falsely claiming entitlement."""
    return {
        "valid": False,
        "can_attempt": True,
        "connection_verified": False,
        "state": "direct_unverified",
        "access_type": "direct",
        "access_type_label": "校园网直连",
        "proxy_bypassed": True,
        "login_supported": False,
        "message": (
            "无需选择学校或登录；将绕过 HTTP/HTTPS 环境代理，使用当前 Windows "
            "网络出口。是否具有机构权限，以出版社实际返回的 PDF 为准。"
        ),
    }


def _response_html_preview(response: requests.Response) -> str:
    """Return a bounded HTML preview for login-page detection."""
    content_type = str(response.headers.get("content-type") or "").casefold()
    if "html" not in content_type and "xhtml" not in content_type:
        return ""
    raw = response.content[:256 * 1024]
    encoding = response.encoding or response.apparent_encoding or "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _looks_like_login_response(response: requests.Response, html_text: str) -> bool:
    """Recognize common login pages without relying on one school's URL layout."""
    final_url = urllib.parse.unquote(response.url).casefold()
    if any(marker in final_url for marker in LOGIN_MARKERS):
        return True

    lower_html = html_text.casefold()
    if not lower_html:
        return False
    if re.search(r"<input[^>]+type\s*=\s*['\"]?password\b", lower_html):
        return True
    return any(marker in lower_html for marker in LOGIN_HTML_MARKERS)


def _is_webvpn_proxy_route(base_url: str, response_url: str) -> bool:
    """Confirm that a WEngine response stayed on an encoded external route.

    Merely receiving a 200 page from the gateway is not proof of login: most
    gateways issue an anonymous session cookie on their login/home page.  An
    authenticated WEngine request to an external URL retains a path shaped as
    ``/https/<iv+ciphertext>/...`` (or the HTTP/port equivalent).
    """
    base = urllib.parse.urlsplit(base_url)
    final = urllib.parse.urlsplit(response_url)
    if (base.hostname or "").casefold() != (final.hostname or "").casefold():
        return False
    parts = [part for part in final.path.split("/") if part]
    if len(parts) < 2:
        return False
    scheme_part = parts[0].casefold()
    encrypted_host = parts[1].casefold()
    return bool(
        re.fullmatch(r"https?(?:-\d+)?", scheme_part)
        and re.fullmatch(r"[0-9a-f]{40,}", encrypted_host)
    )


def make_institution_client(
    config: dict[str, Any],
    *,
    cookie_override: Path | None = None,
) -> tuple[WebVPNClient | RoutedAccessClient, SchoolEntry]:
    entry, _catalog_path, _warnings = selected_school(config)
    network = config.get("network") or {}
    download = config.get("download") or {}
    keys = config.get("api_keys") or {}
    common = {
        "timeout": float(network.get("download_timeout_seconds") or 90),
        "browser_timeout": float(
            download.get("per_paper_timeout_seconds") or 120
        ),
        "delay": float((config.get("institution") or {}).get("request_delay_seconds") or 2),
        "max_bytes": int(download.get("max_file_mb") or 120) * 1024 * 1024,
        "verify_tls": True,
        "elsevier_api_key": str(keys.get("elsevier") or "").strip() or None,
        "elsevier_insttoken": str(keys.get("elsevier_inst_token") or "").strip() or None,
    }
    if entry.access_type == "direct":
        return make_direct_network_client(config), entry
    if entry.access_type == "webvpn":
        client = WebVPNClient(
            base_url=entry.base_url,
            key=entry.key,
            iv=entry.iv,
            cookie_file=cookie_override or cookie_file_for(entry, config),
            **common,
        )
        return client, entry
    if entry.access_type == "ezproxy":
        client = RoutedAccessClient(
            base_url=entry.base_url,
            access_type="ezproxy",
            cookie_file=cookie_override or cookie_file_for(entry, config),
            proxy_template=entry.proxy_template,
            **common,
        )
        return client, entry
    connector = str((config.get("institution") or {}).get("connector_url") or entry.connector_url)
    client = RoutedAccessClient(
        base_url=entry.gateway,
        access_type=entry.access_type,
        connector_url=connector,
        **common,
    )
    return client, entry


def institution_session_status(config: dict[str, Any]) -> dict[str, Any]:
    try:
        entry, catalog_path, warnings = selected_school(config)
    except (InstitutionalConfigError, OSError) as exc:
        return {
            "valid": False,
            "state": "config_error",
            "message": str(exc),
            "login_supported": False,
        }
    base = {
        "school_id": entry.id,
        "school_name": entry.name,
        "province": entry.province,
        "access_type": entry.access_type,
        "access_type_label": entry.type_label,
        "catalog_path": str(catalog_path),
        "catalog_warnings": warnings,
        "origin": entry.origin,
    }
    if entry.access_type == "direct":
        return {**base, **direct_network_status()}
    if entry.access_type in {"easyconnect", "atrust"}:
        connector = str((config.get("institution") or {}).get("connector_url") or entry.connector_url)
        return {
            **base,
            "valid": False,
            "can_attempt": True,
            "connection_verified": False,
            "state": "external_unverified",
            "message": (
                f"{entry.type_label} 由外部客户端管理；配置可尝试下载，"
                "但无法自动确认已连接该学校网络。将使用"
                + (f"连接器 {connector}" if connector else "当前 Windows 网络路由")
            ),
            "login_supported": False,
            "external_login_required": True,
            "gateway": entry.gateway,
            "connector_url": connector,
        }
    response: requests.Response | None = None
    client: WebVPNClient | RoutedAccessClient | None = None
    try:
        client, _entry = make_institution_client(config)
        response = client.get(
            "https://doi.org/",
            stream=False,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"},
        )
        html_preview = _response_html_preview(response)
        expired = _looks_like_login_response(response, html_preview)
        route_verified = True
        if entry.access_type == "webvpn":
            route_verified = _is_webvpn_proxy_route(entry.base_url, response.url)
        if entry.access_type == "ezproxy":
            parsed_final = urllib.parse.urlsplit(response.url)
            expired = expired or (
                host_of(response.url) == host_of(entry.base_url)
                and "login" in parsed_final.path.casefold()
            )
        if expired or not route_verified or response.status_code >= 400:
            if expired:
                detail = "探测请求仍停留在登录页"
            elif not route_verified:
                detail = "探测请求仍停留在学校网关首页，尚未进入校外代理路由"
            else:
                detail = f"校外探测请求返回 HTTP {response.status_code}"
            return {
                **base,
                "valid": False,
                "state": "login_required",
                "message": f"{entry.type_label} 登录尚未完成：{detail}",
                "login_supported": True,
                "http_status": response.status_code,
            }
        return {
            **base,
            "valid": True,
            "state": "valid",
            "message": f"{entry.name} {entry.type_label} 会话有效",
            "login_supported": True,
            "http_status": response.status_code,
            "cookie_count": (client.cookie_info or {}).get("matching_host", 0),
        }
    except requests.RequestException as exc:
        return {
            **base,
            "valid": False,
            "state": "network_error",
            "message": f"{type(exc).__name__}: {exc}",
            "login_supported": True,
        }
    except (DownloadError, ValueError, OSError) as exc:
        return {
            **base,
            "valid": False,
            "state": "missing",
            "message": str(exc),
            "login_supported": True,
        }
    finally:
        if response is not None:
            response.close()
        if client is not None:
            client.session.close()


def login_institution(config: dict[str, Any], timeout: float) -> dict[str, Any]:
    entry, catalog_path, warnings = selected_school(config)
    if entry.access_type == "direct":
        return {
            "ok": True,
            "no_login_required": True,
            "school_id": entry.id,
            "school_name": entry.name,
            "access_type": entry.access_type,
            "access_type_label": entry.type_label,
            "reason": "校园网直连无需登录；开始下载后将直接使用当前 Windows 网络出口。",
            "catalog_path": str(catalog_path),
            "catalog_warnings": warnings,
        }
    if entry.access_type in {"easyconnect", "atrust"}:
        return {
            "ok": True,
            "external_login_required": True,
            "school_id": entry.id,
            "school_name": entry.name,
            "access_type": entry.access_type,
            "access_type_label": entry.type_label,
            "gateway": entry.gateway,
            "reason": f"请先在 {entry.type_label} 客户端中连接 {entry.name}，然后直接开始下载。",
            "catalog_path": str(catalog_path),
            "catalog_warnings": warnings,
        }
    target_cookie_file = cookie_file_for(entry, config)

    def validator(candidate: Path) -> dict[str, Any]:
        candidate_config = json.loads(json.dumps(config))
        candidate_config.setdefault("institution", {})["cookie_file"] = str(candidate)
        return institution_session_status(candidate_config)

    try:
        result = login_with_installed_browser(
            base_url=entry.base_url,
            cookie_file=target_cookie_file,
            timeout=timeout,
            validator=validator,
        )
    except BrowserLoginError as exc:
        return {
            "ok": False,
            "error_type": "browser_login_failed",
            "reason": str(exc),
            "method": "installed_browser_cdp",
            "school_id": entry.id,
            "school_name": entry.name,
            "access_type": entry.access_type,
            "access_type_label": entry.type_label,
        }
    result.update(
        {
            "school_id": entry.id,
            "school_name": entry.name,
            "access_type": entry.access_type,
            "access_type_label": entry.type_label,
        }
    )
    return result


def download_via_institution(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    overwrite: bool,
    output_format: str,
    progress=None,
) -> dict[str, Any]:
    """Retry failed WebVPN downloads through a fresh campus-network client."""
    forced_direct = bool((config.get("_runtime") or {}).get("force_campus_direct"))
    entry, _, _ = selected_school(config)
    if not forced_direct:
        try:
            result = _download_selected_institution(
                doi, output_path, config, overwrite=overwrite,
                output_format=output_format,
            )
        except Exception as exc:
            if entry.access_type != "webvpn" or output_format != "pdf":
                raise
            result = {
                "ok": False, "source": "institution", "access_type": "webvpn",
                "error_type": "institution_exception",
                "reason": f"{type(exc).__name__}: {exc}", "attempts": [],
            }
        if result.get("ok") or entry.access_type != "webvpn" or output_format != "pdf":
            return result
    else:
        result = None
    if output_format != "pdf":
        return {"ok": False, "source": "institution", "skipped": True,
                "reason": "校园网直连只提供 PDF", "attempts": []}
    logging.getLogger(__name__).warning(
        "[%s] WebVPN 未取得 PDF，自动尝试校园网直连（不使用 WebVPN）", doi
    )
    if progress:
        progress("campus_direct_download")
    client = None
    try:
        client = make_direct_network_client(config)
        direct = download_one(
            doi, output_path, client, overwrite=overwrite,
            max_steps=24 if doi.casefold().startswith("10.3390/") else 12,
            config=config,
        )
    except Exception as exc:
        direct = {"ok": False, "error_type": "campus_direct_exception",
                  "reason": f"{type(exc).__name__}: {exc}", "attempts": []}
    finally:
        if client is not None:
            client.session.close()
    direct.update(source="institution", school=entry.name, access_type="direct",
                  access_type_label="校园网直连", fallback_from="webvpn")
    if result is not None:
        direct["webvpn_failure"] = result
        direct["attempts"] = [
            {"label": "WebVPN failed; retry campus direct", "access_type": "webvpn",
             "error_type": result.get("error_type"), "detail": result.get("reason"),
             "details": result.get("attempts", [])},
            *[{**attempt, "access_type": "direct"} for attempt in direct.get("attempts", [])],
        ]
    if not direct.get("ok"):
        direct["reason"] = "WebVPN 失败后，校园网直连也未取得 PDF：" + str(direct.get("reason") or "下载失败")
    return direct


def _download_selected_institution(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    *,
    overwrite: bool,
    output_format: str,
) -> dict[str, Any]:
    if output_format != "pdf":
        return {
            "ok": False,
            "source": "institution",
            "reason": "机构接入只提供 PDF",
            "skipped": True,
            "attempts": [],
        }
    # The CLI already performs this network probe once before a batch. Reusing
    # it avoids one WebVPN round-trip (up to the full timeout) for every DOI.
    runtime = config.get("_runtime") or {}
    cached_status = runtime.get("institution_status")
    status = (
        cached_status
        if isinstance(cached_status, dict)
        else institution_session_status(config)
    )
    if not status.get("valid") and not status.get("can_attempt"):
        return {
            "ok": False,
            "source": "institution",
            "error_type": "session_expired",
            "reason": status.get("message"),
            "school": status.get("school_name"),
            "access_type": status.get("access_type"),
            "attempts": [],
        }
    client: WebVPNClient | RoutedAccessClient | None = None
    try:
        client, entry = make_institution_client(config)
        result = download_one(
            doi,
            output_path,
            client,  # type: ignore[arg-type]
            overwrite=overwrite,
            max_steps=24 if doi.casefold().startswith("10.3390/") else 12,
            config=config,
        )
        result["source"] = result.get("source") or "institution"
        result["school"] = entry.name
        result["access_type"] = entry.access_type
        result["access_type_label"] = entry.type_label
        return result
    finally:
        if client is not None:
            client.session.close()


def apply_school_selection(
    config: dict[str, Any],
    entry: SchoolEntry,
    catalog_path: Path,
) -> dict[str, Any]:
    settings = dict(config.get("institution") or {})
    settings.update(
        {
            "school_id": entry.id,
            "school_name": entry.name,
            "access_type": entry.access_type,
            "schools_file": str(catalog_path.resolve()),
        }
    )
    settings.pop("cookie_file", None)
    config["institution"] = settings
    return config
