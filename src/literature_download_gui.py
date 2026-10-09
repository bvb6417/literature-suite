#!/usr/bin/env python3
"""Tkinter frontend that invokes literature_download_cli.py as a subprocess."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import ctypes
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


from suite_paths import APP_DIR
# Theme files are intentionally kept beside the EXE/source tree instead of
# being embedded into PyInstaller.  This makes updates a simple folder copy.
from suite_paths import RESOURCE_DIR
CLI_PY_PATH = APP_DIR / "literature_download_cli.py"
CLI_EXE_PATH = APP_DIR / "literature_download_cli.exe"
DEFAULT_CONFIG = APP_DIR / "config.local.json"
DEFAULT_DOWNLOADS = APP_DIR / "downloads"
DEFAULT_SCHOOLS = RESOURCE_DIR / "schools.json"
AZURE_THEME_PATH = RESOURCE_DIR / "themes" / "azure" / "azure.tcl"

OFFICIAL_ACCOUNT_URLS = {
    "elsevier": "https://dev.elsevier.com/apikey/manage",
    "openalex": "https://openalex.org/settings/api",
    "crossref": (
        "https://www.crossref.org/documentation/retrieve-metadata/"
        "rest-api/access-and-authentication/"
    ),
}


def windows_no_window_flags(*, new_process_group: bool = False) -> int:
    """Suppress transient console windows without hiding GUI applications."""
    if os.name != "nt":
        return 0
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    return flags


def format_elapsed(seconds: Any) -> str:
    """Format a duration compactly for the result table and status bar."""
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 60:
        return f"{total:.1f} 秒"
    minutes, remainder = divmod(int(round(total)), 60)
    if minutes < 60:
        return f"{minutes} 分 {remainder:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分 {remainder:02d} 秒"


def resolve_download_kernel():
    from suite_paths import ENTRY_PATH
    return ENTRY_PATH


def system_python_error(kernel: Path) -> str:
    """Probe the PATH-level `python` command without searching for python.exe."""
    if kernel.suffix.casefold() != ".py" or not getattr(sys, "frozen", False):
        return ""
    try:
        completed = subprocess.run(
            ["python", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return (
            "本机没有 Python 环境，无法运行 literature_download_cli.py。\n\n"
            "请安装 Python，并确保在命令提示符中可以直接运行 python。"
        )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        return (
            "本机的系统 Python 命令不可用，无法运行 literature_download_cli.py。"
            + (f"\n\n{detail}" if detail else "")
        )
    return ""


def enable_windows_dpi_awareness() -> None:
    """Prevent Windows from bitmap-scaling Tk, which makes text look blurry."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


enable_windows_dpi_awareness()


class ModernProgressBar(tk.Canvas):
    """Flat rounded progress bar with determinate and indeterminate modes."""

    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
        self._maximum = float(kwargs.pop("maximum", 100))
        self._value = float(kwargs.pop("value", 0))
        self._mode = str(kwargs.pop("mode", "determinate"))
        self._phase = 0.0
        self._timer: str | None = None
        super().__init__(
            master,
            height=12,
            background="#FFFFFF",
            highlightthickness=0,
            borderwidth=0,
            **kwargs,
        )
        self.bind("<Configure>", lambda _event: self._draw())

    @staticmethod
    def _rounded_points(
        x1: float, y1: float, x2: float, y2: float, radius: float
    ) -> list[float]:
        return [
            x1 + radius, y1, x2 - radius, y1, x2, y1,
            x2, y1 + radius, x2, y2 - radius, x2, y2,
            x2 - radius, y2, x1 + radius, y2, x1, y2,
            x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]

    def _rounded_rectangle(
        self, x1: float, y1: float, x2: float, y2: float, fill: str
    ) -> None:
        radius = max(1.0, min((y2 - y1) / 2, (x2 - x1) / 2))
        self.create_polygon(
            self._rounded_points(x1, y1, x2, y2, radius),
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline="",
        )

    def _draw(self) -> None:
        self.delete("all")
        width = max(1, self.winfo_width())
        height = max(8, self.winfo_height())
        y1, y2 = 2, height - 2
        self._rounded_rectangle(0, y1, width, y2, "#E2E8F0")
        if self._mode == "indeterminate":
            segment = max(28, width * 0.28)
            start = (width + segment) * self._phase - segment
            end = min(width, start + segment)
            start = max(0, start)
            if end > start:
                self._rounded_rectangle(start, y1, end, y2, "#3B82F6")
            return
        fraction = min(1.0, max(0.0, self._value / max(1.0, self._maximum)))
        fill_width = width * fraction
        if fill_width > 1:
            self._rounded_rectangle(0, y1, fill_width, y2, "#2563EB")

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if cnf:
            kwargs.update(cnf)
        if "maximum" in kwargs:
            self._maximum = float(kwargs.pop("maximum"))
        if "value" in kwargs:
            self._value = float(kwargs.pop("value"))
        if "mode" in kwargs:
            self._mode = str(kwargs.pop("mode"))
        result = super().configure(**kwargs) if kwargs else None
        self._draw()
        return result

    config = configure

    def start(self, interval: int = 16) -> None:
        self.stop()

        def animate() -> None:
            self._phase = (self._phase + 0.025) % 1.0
            self._draw()
            self._timer = self.after(max(10, interval), animate)

        animate()

    def stop(self) -> None:
        if self._timer is not None:
            self.after_cancel(self._timer)
            self._timer = None
        self._draw()


class ToolTip:
    """Small delayed hover hint for controls without native tooltips."""

    def __init__(self, widget: tk.Misc, text: str, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._timer: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event: tk.Event[Any]) -> None:
        self._cancel()
        self._timer = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._timer is not None:
            self.widget.after_cancel(self._timer)
            self._timer = None

    def _show(self) -> None:
        self._timer = None
        if self._window is not None:
            return
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        try:
            window.attributes("-topmost", True)
        except tk.TclError:
            pass
        x = self.widget.winfo_pointerx() + 14
        y = self.widget.winfo_pointery() + 18
        window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            window,
            text=self.text,
            justify="left",
            wraplength=430,
            background="#0F172A",
            foreground="#FFFFFF",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=7,
            font=("Microsoft YaHei UI", 9),
        ).pack()
        self._window = window

    def _hide(self, _event: tk.Event[Any] | None = None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None


class DownloadApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        try:
            self.tk.call("tk", "scaling", self.winfo_fpixels("1i") / 72.0)
        except tk.TclError:
            pass
        self.title("SCI 全文下载器")
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        # Use a comfortable desktop size based on the manually adjusted layout;
        # only shrink when the physical screen cannot accommodate it.
        window_width = max(1100, min(1500, screen_width - 80))
        window_height = max(800, min(1000, screen_height - 80))
        left = max(0, (screen_width - window_width) // 2)
        top = max(0, (screen_height - window_height) // 2)
        self.geometry(f"{window_width}x{window_height}+{left}+{top}")
        self.minsize(min(1180, window_width), min(820, window_height))
        self.configure(background="#FFFFFF")
        self._window_icon = self._make_search_icon()
        self.iconphoto(True, self._window_icon)
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.download_kernel = resolve_download_kernel()
        self._busy = False
        self._download_active = False
        self._stop_requested = False
        self._download_process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()
        self._result_paths: dict[str, Path] = {}
        self._result_links: dict[str, str] = {}
        self._result_items: dict[str, str] = {}
        self._expired_prompted = False
        self._download_total = 0
        self._download_completed = 0
        self._download_successes = 0
        self._batch_started_at: float | None = None
        self._current_item_doi = ""
        self._current_item_started_at: float | None = None
        self._current_item_timeout = 0
        self._pane_ratio_job: str | None = None
        self._settings_scroll_job: str | None = None
        self._settings_scroll_visible = True

        self._config_path = DEFAULT_CONFIG
        self.output_dir = tk.StringVar(value=str(DEFAULT_DOWNLOADS))
        self.output_format = tk.StringVar(value="pdf")
        self.use_openalex = tk.BooleanVar(value=True)
        self.use_elsevier = tk.BooleanVar(value=True)
        self.use_webvpn = tk.BooleanVar(value=True)
        self.elsevier_use_aam = tk.BooleanVar(value=False)
        self.elsevier_use_xml_rebuild = tk.BooleanVar(value=False)
        self.overwrite = tk.BooleanVar(value=False)
        self.status_text = tk.StringVar(value="正在检查下载来源……")
        self.progress_text = tk.StringVar(value="等待任务")
        self.total_time_text = tk.StringVar(value="本次任务总耗时：—")
        self.settings_status_text = tk.StringVar(value="")
        self.download_timeout_seconds = tk.StringVar(value="90")
        self.per_paper_timeout_seconds = tk.StringVar(value="120")
        self.max_retries = tk.StringVar(value="2")
        self.retry_delay_seconds = tk.StringVar(value="2")
        self.institution_delay_seconds = tk.StringVar(value="2")
        self.elsevier_figure_workers = tk.StringVar(value="4")
        self.elsevier_api_key = tk.StringVar(value="")
        self.openalex_api_key = tk.StringVar(value="")
        self.crossref_email = tk.StringVar(value="")
        self.show_api_keys = tk.BooleanVar(value=False)
        self._credentials_origin_path: Path | None = None
        self.school_query = tk.StringVar(value="正在加载学校配置库……")
        self.school_detail_text = tk.StringVar(value="")
        self._school_entries: list[dict[str, Any]] = []
        self._school_by_display: dict[str, dict[str, Any]] = {}
        self._school_catalog_path = DEFAULT_SCHOOLS
        self._loading_schools_initially = True

        self._load_config_into_vars()
        self._configure_style()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(0, self._show_main_window)
        self.after_idle(self._set_initial_pane_position)
        self.after(120, self._set_initial_pane_position)
        self.after(450, self._set_initial_pane_position)
        self.after(100, self._poll_events)
        self.after(220, self.load_schools)

    def _show_main_window(self) -> None:
        """Override inherited hidden startup flags from console-less launchers."""
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
        except tk.TclError:
            pass

    def _make_search_icon(self) -> tk.PhotoImage:
        """Draw a small transparent magnifying-glass window icon."""
        image = tk.PhotoImage(width=32, height=32)
        for y in range(32):
            for x in range(32):
                distance = ((x - 12.5) ** 2 + (y - 12.5) ** 2) ** 0.5
                on_ring = 7.0 <= distance <= 10.0
                handle_x = x - 19
                handle_y = y - 19
                on_handle = (
                    handle_x >= 0
                    and handle_y >= 0
                    and abs(handle_x - handle_y) <= 2
                    and handle_x <= 10
                )
                if on_ring or on_handle:
                    image.put("#111827", (x, y))
        return image

    def _set_initial_pane_position(self) -> None:
        """Keep the DOI and result panes at an exact 1:2 width ratio."""
        self._pane_ratio_job = None
        try:
            width = self.main_pane.winfo_width()
            if width > 1:
                self.main_pane.sashpos(0, int(round(width / 3)))
        except tk.TclError:
            pass

    def _schedule_pane_ratio(self, _event: tk.Event[Any] | None = None) -> None:
        """Reapply the ratio after Tk finishes a window/layout resize."""
        if self._pane_ratio_job is not None:
            try:
                self.after_cancel(self._pane_ratio_job)
            except tk.TclError:
                pass
        self._pane_ratio_job = self.after(25, self._set_initial_pane_position)

    def _config_file_path(self) -> Path:
        return self._config_path

    def _load_config_into_vars(self) -> None:
        """Load GUI-owned settings, masking credentials in their entry widgets."""
        path = self._config_file_path()
        self._credentials_origin_path = None
        if not path.exists():
            self.elsevier_api_key.set("")
            self.openalex_api_key.set("")
            self.crossref_email.set("")
            self._credentials_origin_path = path.resolve()
            self.settings_status_text.set("配置文件不存在；保存后将创建。")
            return
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("配置文件顶层必须是 JSON 对象")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.elsevier_api_key.set("")
            self.openalex_api_key.set("")
            self.crossref_email.set("")
            self.settings_status_text.set(f"配置读取失败：{exc}")
            return

        api_keys = config.get("api_keys") if isinstance(config.get("api_keys"), dict) else {}
        network = config.get("network") if isinstance(config.get("network"), dict) else {}
        download = config.get("download") if isinstance(config.get("download"), dict) else {}
        institution = (
            config.get("institution")
            if isinstance(config.get("institution"), dict)
            else {}
        )
        elsevier = config.get("elsevier") if isinstance(config.get("elsevier"), dict) else {}

        self.download_timeout_seconds.set(str(network.get("download_timeout_seconds", 90)))
        self.max_retries.set(str(network.get("max_retries", 2)))
        self.retry_delay_seconds.set(str(network.get("retry_delay_seconds", 2)))
        self.per_paper_timeout_seconds.set(str(download.get("per_paper_timeout_seconds", 120)))
        self.institution_delay_seconds.set(str(institution.get("request_delay_seconds", 2)))
        self.elsevier_use_aam.set(bool(elsevier.get("use_aam", False)))
        self.elsevier_use_xml_rebuild.set(
            bool(elsevier.get("use_xml_reconstruction", False))
        )
        self.elsevier_figure_workers.set(str(elsevier.get("figure_download_workers", 4)))
        self.elsevier_api_key.set(str(api_keys.get("elsevier") or "").strip())
        self.openalex_api_key.set(str(api_keys.get("openalex") or "").strip())
        self.crossref_email.set(str(config.get("contact_email") or "").strip())
        self.show_api_keys.set(False)
        self._toggle_key_visibility()

        source_order = download.get("source_order")
        if isinstance(source_order, list):
            enabled = {str(item).casefold() for item in source_order}
            self.use_openalex.set("openalex" in enabled)
            self.use_elsevier.set("elsevier" in enabled)
            self.use_webvpn.set("webvpn" in enabled)
        self._credentials_origin_path = path.resolve()
        self.settings_status_text.set("配置已加载")

    @staticmethod
    def _validated_number(
        value: str,
        label: str,
        *,
        minimum: float,
        maximum: float,
        integer: bool = False,
    ) -> int | float:
        try:
            number = float(value.strip())
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"{label}必须是数字") from exc
        if integer and not number.is_integer():
            raise ValueError(f"{label}必须是整数")
        if not minimum <= number <= maximum:
            raise ValueError(f"{label}必须在 {minimum:g}–{maximum:g} 之间")
        return int(number) if integer else number

    def save_system_config(self, *, silent: bool = False) -> bool:
        """Persist GUI settings while preserving unknown and compatibility fields."""
        path = self._config_file_path()
        try:
            if path.exists():
                config = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise ValueError("配置文件顶层必须是 JSON 对象")
            else:
                config = {}

            contact_email = self.crossref_email.get().strip()
            if contact_email and (
                "@" not in contact_email
                or any(character.isspace() for character in contact_email)
            ):
                raise ValueError("Crossref 邮箱格式不正确")

            download_timeout = self._validated_number(
                self.download_timeout_seconds.get(),
                "下载请求超时",
                minimum=5,
                maximum=600,
                integer=True,
            )
            per_paper_timeout = self._validated_number(
                self.per_paper_timeout_seconds.get(),
                "单篇安全上限",
                minimum=120,
                maximum=3600,
                integer=True,
            )
            max_retries = self._validated_number(
                self.max_retries.get(),
                "重试次数",
                minimum=0,
                maximum=10,
                integer=True,
            )
            retry_delay = self._validated_number(
                self.retry_delay_seconds.get(),
                "重试间隔",
                minimum=0,
                maximum=60,
            )
            institution_delay = self._validated_number(
                self.institution_delay_seconds.get(),
                "机构请求间隔",
                minimum=0,
                maximum=60,
            )
            figure_workers = self._validated_number(
                self.elsevier_figure_workers.get(),
                "Elsevier 图片并发数",
                minimum=1,
                maximum=8,
                integer=True,
            )
            source_order = self._selected_sources()
            if not source_order:
                raise ValueError("请至少启用一个下载来源")

            for section in ("network", "download", "institution", "elsevier"):
                if not isinstance(config.get(section), dict):
                    config[section] = {}
            if not isinstance(config.get("api_keys"), dict):
                config["api_keys"] = {}
            config["contact_email"] = contact_email
            config["api_keys"].update(
                {
                    "elsevier": self.elsevier_api_key.get().strip(),
                    "openalex": self.openalex_api_key.get().strip(),
                }
            )
            config["network"].update(
                {
                    "download_timeout_seconds": download_timeout,
                    "max_retries": max_retries,
                    "retry_delay_seconds": retry_delay,
                }
            )
            config["download"].update(
                {
                    "per_paper_timeout_seconds": per_paper_timeout,
                    "source_order": source_order,
                }
            )
            config["institution"]["request_delay_seconds"] = institution_delay
            config["elsevier"].update(
                {
                    "use_aam": bool(self.elsevier_use_aam.get()),
                    "use_xml_reconstruction": bool(
                        self.elsevier_use_xml_rebuild.get()
                    ),
                    "figure_download_workers": figure_workers,
                }
            )

            payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_dir = APP_DIR / ".tmp" / "runtime"
            temp_dir.mkdir(parents=True, exist_ok=True)
            temp_path = temp_dir / f"{path.name}.part"
            temp_path.write_text(payload, encoding="utf-8")
            try:
                os.replace(temp_path, path)
            except OSError:
                # A user-selected config can live on another volume.  Keep the
                # temporary artifact under .tmp even when an atomic rename is
                # unavailable across drives.
                path.write_text(payload, encoding="utf-8")
                temp_path.unlink(missing_ok=True)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.settings_status_text.set(f"保存失败：{exc}")
            if not silent:
                messagebox.showerror("配置保存失败", str(exc))
            return False

        self._credentials_origin_path = path.resolve()
        self.settings_status_text.set("配置已保存")
        if not silent:
            messagebox.showinfo("配置已保存", "系统配置已保存，并将在下一次下载时生效。")
        return True

    def _toggle_key_visibility(self) -> None:
        mask = "" if self.show_api_keys.get() else "*"
        for widget_name in ("elsevier_key_entry", "openalex_key_entry"):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(show=mask)

    def open_official_account_page(self, provider: str) -> None:
        url = OFFICIAL_ACCOUNT_URLS.get(provider)
        if not url:
            messagebox.showerror("链接不存在", "没有找到对应的官方申请链接。")
            return
        try:
            opened = webbrowser.open_new_tab(url)
        except (OSError, webbrowser.Error) as exc:
            messagebox.showerror("无法打开浏览器", str(exc))
            return
        if not opened:
            messagebox.showwarning(
                "浏览器未响应",
                f"未能调用默认浏览器，请手动访问：\n{url}",
            )

    def open_config_file(self) -> None:
        path = self._config_file_path()
        if not path.exists():
            if not self.save_system_config(silent=True):
                messagebox.showerror("配置不存在", f"无法创建：{path}")
                return
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        self._azure_theme_loaded = False
        try:
            self.tk.call("source", str(AZURE_THEME_PATH))
            self.tk.call("set_theme", "light")
            self._azure_theme_loaded = True
        except (OSError, tk.TclError):
            try:
                style.theme_use("vista" if "vista" in style.theme_names() else "clam")
            except tk.TclError:
                pass
        style.configure("TFrame", background="#FFFFFF")
        style.configure(
            "TLabelframe",
            background="#FFFFFF",
            bordercolor="#DDE5F0",
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background="#FFFFFF",
            foreground="#1E293B",
            font=("Microsoft YaHei UI", 10, "bold"),
        )
        style.configure(
            "TLabel",
            background="#FFFFFF",
            foreground="#334155",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure("Surface.TFrame", background="#FFFFFF")
        style.configure("Card.TLabel", background="#FFFFFF", foreground="#475569")
        style.configure(
            "TButton",
            font=("Microsoft YaHei UI", 9),
            padding=(12, 7),
            borderwidth=0,
        )
        style.configure(
            "Accent.TButton",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(18, 9),
        )
        style.configure(
            "Stop.TButton",
            background="#DC2626",
            foreground="#FFFFFF",
            font=("Microsoft YaHei UI", 10, "bold"),
            padding=(18, 9),
        )
        style.map(
            "Stop.TButton",
            background=[("active", "#B91C1C"), ("disabled", "#F1A1A1")],
        )
        style.configure(
            "Header.TButton",
            background="#294A6D",
            foreground="#FFFFFF",
            padding=(12, 7),
        )
        style.map("Header.TButton", background=[("active", "#355C84")])
        style.configure(
            "TEntry",
            fieldbackground="#FFFFFF",
            bordercolor="#CBD5E1",
            lightcolor="#CBD5E1",
            darkcolor="#CBD5E1",
            padding=7,
        )
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground="#334155",
            rowheight=30,
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#EEF3F9",
            foreground="#334155",
            relief="flat",
            padding=(8, 8),
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", "#1E3A8A")])
        style.configure(
            "Download.Horizontal.TProgressbar",
            troughcolor="#E2E8F0",
            background="#2563EB",
            lightcolor="#2563EB",
            darkcolor="#2563EB",
            thickness=10,
        )

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self, padding=(2, 2))
        self.notebook.grid(row=0, column=0, padx=14, pady=(12, 10), sticky="nsew")
        self.download_tab = ttk.Frame(self.notebook, padding=(4, 10, 4, 2))
        self.settings_tab = ttk.Frame(self.notebook, padding=0)
        self.notebook.add(self.download_tab, text="  下载任务  ")
        self.notebook.add(self.settings_tab, text="  系统配置  ")
        self._build_download_tab()
        self._build_settings_tab()

    def _build_download_tab(self) -> None:
        tab = self.download_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        options = ttk.LabelFrame(tab, text="下载设置", padding=12)
        options.grid(row=0, column=0, padx=4, pady=(0, 10), sticky="ew")
        options.columnconfigure(1, weight=1)
        self.status_label = ttk.Label(
            options,
            textvariable=self.status_text,
            style="Card.TLabel",
            foreground="#64748B",
        )
        self.status_label.grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(options, text="启用来源", style="Card.TLabel").grid(
            row=2, column=0, pady=(13, 0), sticky="w"
        )
        source_bar = ttk.Frame(options, style="Surface.TFrame")
        source_bar.grid(
            row=2, column=1, columnspan=3, pady=(13, 0), sticky="w"
        )
        ttk.Checkbutton(
            source_bar,
            text="开放全文（OpenAlex 等）",
            style="TCheckbutton",
            variable=self.use_openalex,
        ).pack(side="left")
        ttk.Checkbutton(
            source_bar,
            text="Elsevier",
            style="TCheckbutton",
            variable=self.use_elsevier,
        ).pack(side="left", padx=(26, 0))
        ttk.Checkbutton(
            source_bar,
            text="学校机构来源",
            style="TCheckbutton",
            variable=self.use_webvpn,
        ).pack(side="left", padx=(26, 0))

        ttk.Label(options, text="输出格式", style="Card.TLabel").grid(
            row=3, column=0, pady=(13, 0), sticky="w"
        )
        format_bar = ttk.Frame(options, style="Surface.TFrame")
        format_bar.grid(
            row=3, column=1, columnspan=3, pady=(13, 0), sticky="w"
        )
        ttk.Radiobutton(
            format_bar,
            text="PDF",
            style="TRadiobutton",
            value="pdf",
            variable=self.output_format,
        ).pack(side="left")
        ttk.Radiobutton(
            format_bar,
            text="Elsevier 全文 XML",
            style="TRadiobutton",
            value="xml",
            variable=self.output_format,
        ).pack(side="left", padx=(26, 0))
        ttk.Checkbutton(
            format_bar,
            text="覆盖已有文件",
            style="TCheckbutton",
            variable=self.overwrite,
        ).pack(side="left", padx=(26, 0))

        ttk.Label(options, text="Elsevier 后备", style="Card.TLabel").grid(
            row=4, column=0, pady=(13, 0), sticky="w"
        )
        fallback_bar = ttk.Frame(options, style="Surface.TFrame")
        fallback_bar.grid(
            row=4, column=1, columnspan=3, pady=(13, 0), sticky="w"
        )
        aam_check = ttk.Checkbutton(
            fallback_bar,
            text="爱思唯尔采用 AAM",
            style="TCheckbutton",
            variable=self.elsevier_use_aam,
        )
        aam_check.pack(side="left")
        xml_check = ttk.Checkbutton(
            fallback_bar,
            text="爱思唯尔采用 XML 重建 PDF",
            style="TCheckbutton",
            variable=self.elsevier_use_xml_rebuild,
        )
        xml_check.pack(side="left", padx=(26, 0))
        ToolTip(
            aam_check,
            "出版版 API 失败后，优先尝试 Elsevier 官方 AAM。"
            "AAM 是作者接受稿，排版可能与出版版不同。",
        )
        ToolTip(
            xml_check,
            "前面的出版版 API 和 AAM 都失败后，使用官方全文 XML "
            "与图片重建 PDF；内容完整，但分页和排版可能与出版版不同。",
        )
        ttk.Label(
            options,
            text=(
                "默认：出版版 API → 学校机构；勾选后 AAM / XML 重建"
                "会插在学校机构之前"
            ),
            style="Card.TLabel",
            foreground="#64748B",
        ).grid(row=5, column=1, columnspan=3, pady=(7, 0), sticky="w")

        progress_row = ttk.Frame(options, style="Surface.TFrame")
        progress_row.grid(
            row=6, column=0, columnspan=4, pady=(13, 0), sticky="ew"
        )
        progress_row.columnconfigure(1, weight=1)
        ttk.Label(progress_row, text="下载进度", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.progress = ModernProgressBar(
            progress_row,
            mode="determinate",
            maximum=1,
            value=0,
        )
        self.progress.grid(row=0, column=1, padx=(12, 10), sticky="ew")
        ttk.Label(
            progress_row,
            textvariable=self.progress_text,
            style="Card.TLabel",
            width=18,
            anchor="e",
        ).grid(row=0, column=2, sticky="e")

        main = ttk.Panedwindow(tab, orient="horizontal")
        self.main_pane = main
        main.grid(row=1, column=0, padx=4, pady=(0, 8), sticky="nsew")
        main.bind("<Configure>", self._schedule_pane_ratio, add="+")

        input_box = ttk.LabelFrame(main, text="DOI（每行一个；支持空格/逗号分隔）", padding=10)
        input_box.rowconfigure(0, weight=1)
        input_box.columnconfigure(0, weight=1)
        self.doi_text = tk.Text(
            input_box,
            width=1,
            height=7,
            wrap="word",
            undo=True,
            font=("Consolas", 10),
            background="#FFFFFF",
            foreground="#1E293B",
            insertbackground="#2563EB",
            selectbackground="#BFDBFE",
            relief="flat",
            padx=10,
            pady=9,
            highlightthickness=1,
            highlightbackground="#DCE5F0",
            highlightcolor="#2563EB",
        )
        self.doi_text.grid(row=0, column=0, sticky="nsew")
        input_scroll = ttk.Scrollbar(input_box, orient="vertical", command=self.doi_text.yview)
        input_scroll.grid(row=0, column=1, sticky="ns")
        self.doi_text.configure(yscrollcommand=input_scroll.set)
        action_bar = ttk.Frame(input_box, style="Surface.TFrame")
        action_bar.grid(row=1, column=0, columnspan=2, pady=(9, 0), sticky="ew")
        self.download_button = ttk.Button(
            action_bar,
            text="开始下载",
            style="Accent.TButton",
            command=self.toggle_download,
        )
        self.download_button.pack(side="left")
        ttk.Button(action_bar, text="清空", command=lambda: self.doi_text.delete("1.0", "end")).pack(side="left", padx=8)
        main.add(input_box, weight=1)

        result_box = ttk.LabelFrame(main, text="下载结果", padding=8)
        result_box.rowconfigure(0, weight=1)
        result_box.columnconfigure(0, weight=1)
        columns = ("doi", "status", "source", "size", "elapsed", "message")
        self.result_tree = ttk.Treeview(
            result_box,
            columns=columns,
            show="headings",
            height=11,
            selectmode="extended",
        )
        headings = {
            "doi": "DOI",
            "status": "状态",
            "source": "来源",
            "size": "大小",
            "elapsed": "耗时",
            "message": "说明",
        }
        widths = {
            "doi": 185,
            "status": 60,
            "source": 100,
            "size": 68,
            "elapsed": 92,
            "message": 300,
        }
        for name in columns:
            self.result_tree.heading(name, text=headings[name])
            self.result_tree.column(
                name,
                width=widths[name],
                minwidth=55,
                stretch=name == "message",
            )
        self.result_tree.grid(row=0, column=0, sticky="nsew")
        result_scroll = ttk.Scrollbar(result_box, orient="vertical", command=self.result_tree.yview)
        result_scroll.grid(row=0, column=1, sticky="ns")
        self.result_tree.configure(yscrollcommand=result_scroll.set)
        self.result_tree.tag_configure("success", background="#F0FDF4", foreground="#166534")
        self.result_tree.tag_configure("failure", background="#FFF7ED", foreground="#9A3412")
        self.result_tree.bind("<Double-1>", self.open_selected_file)
        self.result_tree.bind("<Button-3>", self._show_result_context_menu)
        self.result_tree.bind("<Shift-F10>", self._show_result_context_menu)
        self.result_tree.bind("<Control-a>", self._select_all_results)
        self.result_context_menu = tk.Menu(self, tearoff=False)
        self.result_context_menu.add_command(
            label="重试下载", command=self._retry_selected_download
        )
        self.result_context_menu.add_command(
            label="复制链接", command=self._copy_selected_link
        )
        self.result_context_menu.add_separator()
        self.result_context_menu.add_command(label="复制 DOI", command=self._copy_selected_doi)
        main.add(result_box, weight=2)

        footer = ttk.Label(
            tab,
            textvariable=self.total_time_text,
            padding=(4, 0, 4, 2),
        )
        footer.grid(row=2, column=0, sticky="w")

    def _schedule_settings_scrollbar_update(
        self, _event: tk.Event[Any] | None = None
    ) -> None:
        if self._settings_scroll_job is not None:
            try:
                self.after_cancel(self._settings_scroll_job)
            except tk.TclError:
                pass
        self._settings_scroll_job = self.after_idle(self._update_settings_scrollbar)

    def _update_settings_scrollbar(self) -> None:
        self._settings_scroll_job = None
        try:
            bounds = self.settings_canvas.bbox(self._settings_canvas_window)
            content_height = 0 if not bounds else bounds[3] - bounds[1]
            viewport_height = self.settings_canvas.winfo_height()
            needs_scrollbar = content_height > viewport_height + 2
            if needs_scrollbar and not self._settings_scroll_visible:
                self.settings_scrollbar.grid(row=0, column=1, sticky="ns")
                self._settings_scroll_visible = True
            elif not needs_scrollbar and self._settings_scroll_visible:
                self.settings_canvas.yview_moveto(0)
                self.settings_scrollbar.grid_remove()
                self._settings_scroll_visible = False
        except tk.TclError:
            pass

    def _on_settings_content_configure(self, _event: tk.Event[Any]) -> None:
        self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all"))
        self._schedule_settings_scrollbar_update()

    def _on_settings_canvas_configure(self, event: tk.Event[Any]) -> None:
        self.settings_canvas.itemconfigure(
            self._settings_canvas_window,
            width=event.width,
        )
        self._schedule_settings_scrollbar_update()

    def _build_settings_tab(self) -> None:
        container = self.settings_tab
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        self.settings_canvas = tk.Canvas(
            container,
            background="#FFFFFF",
            borderwidth=0,
            highlightthickness=0,
        )
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        self.settings_scrollbar = ttk.Scrollbar(
            container,
            orient="vertical",
            command=self.settings_canvas.yview,
        )
        self.settings_scrollbar.grid(row=0, column=1, sticky="ns")
        self.settings_canvas.configure(yscrollcommand=self.settings_scrollbar.set)

        tab = ttk.Frame(self.settings_canvas, padding=(12, 14))
        tab.columnconfigure(0, weight=1)
        self._settings_canvas_window = self.settings_canvas.create_window(
            (0, 0),
            window=tab,
            anchor="nw",
        )
        tab.bind(
            "<Configure>",
            self._on_settings_content_configure,
            add="+",
        )
        self.settings_canvas.bind(
            "<Configure>",
            self._on_settings_canvas_configure,
            add="+",
        )
        self.settings_canvas.bind(
            "<MouseWheel>",
            lambda event: self.settings_canvas.yview_scroll(
                -1 if event.delta > 0 else 1,
                "units",
            ),
            add="+",
        )

        access = ttk.LabelFrame(tab, text="来源与机构", padding=14)
        access.grid(row=0, column=0, sticky="ew")
        access.columnconfigure(1, weight=1)
        ttk.Label(
            access,
            textvariable=self.status_text,
            style="Card.TLabel",
            foreground="#64748B",
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        source_actions = ttk.Frame(access, style="Surface.TFrame")
        source_actions.grid(
            row=0,
            column=2,
            rowspan=2,
            padx=(14, 0),
            sticky="ne",
        )
        source_actions.columnconfigure(0, weight=1, uniform="source_action")
        source_actions.columnconfigure(1, weight=1, uniform="source_action")
        self.check_sources_button = ttk.Button(
            source_actions,
            text="检查来源",
            width=14,
            command=self.check_sources,
        )
        self.check_sources_button.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.login_button = ttk.Button(
            source_actions,
            text="刷新机构登录",
            width=14,
            command=self.login_webvpn,
        )
        self.login_button.grid(row=0, column=1, padx=(6, 0), sticky="ew")

        ttk.Label(access, text="机构接入", style="Card.TLabel").grid(
            row=1, column=0, pady=(12, 0), sticky="nw"
        )
        school_bar = ttk.Frame(access, style="Surface.TFrame")
        school_bar.grid(row=1, column=1, padx=8, pady=(12, 0), sticky="ew")
        school_bar.columnconfigure(0, weight=1)
        self.school_combo = ttk.Combobox(
            school_bar,
            textvariable=self.school_query,
            state="normal",
            values=(),
        )
        self.school_combo.grid(row=0, column=0, sticky="ew")
        self.school_combo.bind("<KeyRelease>", self._filter_school_dropdown)
        self.school_combo.bind(
            "<<ComboboxSelected>>", self._select_school_from_combo
        )
        ttk.Label(
            school_bar,
            textvariable=self.school_detail_text,
            style="Card.TLabel",
            foreground="#64748B",
        ).grid(row=1, column=0, pady=(3, 0), sticky="w")
        self._school_tooltip = ToolTip(
            self.school_combo,
            "置顶的“校园网直连”无需选择学校或登录；适合已在校园网内的电脑。\n"
            "也可输入学校名、省份或接入类型搜索其他适配器。",
        )
        self.refresh_schools_button = ttk.Button(
            source_actions,
            text="刷新配置库",
            width=14,
            command=self.load_schools,
        )
        self.refresh_schools_button.grid(
            row=1,
            column=0,
            padx=(0, 6),
            pady=(8, 0),
            sticky="ew",
        )
        ttk.Button(
            source_actions,
            text="打开学校库",
            width=14,
            command=self.open_schools_file,
        ).grid(
            row=1,
            column=1,
            padx=(6, 0),
            pady=(8, 0),
            sticky="ew",
        )

        credentials = ttk.LabelFrame(tab, text="API 与联系信息", padding=14)
        credentials.grid(row=1, column=0, pady=(12, 0), sticky="ew")
        credentials.columnconfigure(1, weight=1)
        credentials.columnconfigure(4, weight=1)

        ttk.Label(credentials, text="Elsevier API Key", style="Card.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.elsevier_key_entry = ttk.Entry(
            credentials,
            textvariable=self.elsevier_api_key,
            show="*",
        )
        self.elsevier_key_entry.grid(
            row=0, column=1, padx=(10, 6), sticky="ew"
        )
        elsevier_apply_button = ttk.Button(
            credentials,
            text="申请",
            command=lambda: self.open_official_account_page("elsevier"),
        )
        elsevier_apply_button.grid(row=0, column=2, padx=(0, 18))

        ttk.Label(credentials, text="OpenAlex API Key", style="Card.TLabel").grid(
            row=0, column=3, sticky="w"
        )
        self.openalex_key_entry = ttk.Entry(
            credentials,
            textvariable=self.openalex_api_key,
            show="*",
        )
        self.openalex_key_entry.grid(
            row=0, column=4, padx=(10, 6), sticky="ew"
        )
        openalex_apply_button = ttk.Button(
            credentials,
            text="申请",
            command=lambda: self.open_official_account_page("openalex"),
        )
        openalex_apply_button.grid(row=0, column=5)

        ttk.Label(credentials, text="Crossref 邮箱", style="Card.TLabel").grid(
            row=1, column=0, pady=(12, 0), sticky="w"
        )
        self.crossref_email_entry = ttk.Entry(
            credentials,
            textvariable=self.crossref_email,
        )
        self.crossref_email_entry.grid(
            row=1, column=1, padx=(10, 6), pady=(12, 0), sticky="ew"
        )
        crossref_help_button = ttk.Button(
            credentials,
            text="注册说明",
            command=lambda: self.open_official_account_page("crossref"),
        )
        crossref_help_button.grid(row=1, column=2, padx=(0, 18), pady=(12, 0))
        ttk.Label(
            credentials,
            text="用于 REST API polite pool；无需 Crossref 账号",
            style="Card.TLabel",
            foreground="#64748B",
        ).grid(row=1, column=3, columnspan=2, pady=(12, 0), sticky="w")
        ttk.Checkbutton(
            credentials,
            text="显示 Key",
            style="Switch.TCheckbutton",
            variable=self.show_api_keys,
            command=self._toggle_key_visibility,
        ).grid(row=1, column=5, pady=(12, 0), sticky="e")

        ToolTip(
            elsevier_apply_button,
            "在 Elsevier Developer Portal 登录或注册并申请 API Key。",
        )
        ToolTip(
            openalex_apply_button,
            "在 OpenAlex 设置页登录或注册并获取 API Key。",
        )
        ToolTip(
            crossref_help_button,
            "Crossref REST API 无需注册；打开官方说明了解联系邮箱的用途。",
        )
        self._toggle_key_visibility()

        tuning = ttk.LabelFrame(tab, text="下载内核配置", padding=14)
        tuning.grid(row=2, column=0, pady=(12, 0), sticky="ew")
        tuning.columnconfigure(1, weight=1, uniform="config_value")
        tuning.columnconfigure(4, weight=1, uniform="config_value")
        tuning.columnconfigure(2, minsize=72)
        tuning.columnconfigure(5, minsize=72)

        fields = (
            ("下载请求超时（秒）", self.download_timeout_seconds, "5–600"),
            ("单篇安全上限（秒）", self.per_paper_timeout_seconds, "120–3600"),
            ("失败重试次数", self.max_retries, "0–10"),
            ("重试间隔（秒）", self.retry_delay_seconds, "0–60"),
            ("机构请求间隔（秒）", self.institution_delay_seconds, "0–60"),
            ("Elsevier 图片并发数", self.elsevier_figure_workers, "1–8"),
        )
        for index, (label, variable, hint) in enumerate(fields):
            row = index // 2
            column = 0 if index % 2 == 0 else 3
            ttk.Label(tuning, text=label, style="Card.TLabel").grid(
                row=row,
                column=column,
                padx=(0 if column == 0 else 24, 10),
                pady=(0 if row == 0 else 12, 0),
                sticky="w",
            )
            ttk.Entry(tuning, textvariable=variable).grid(
                row=row,
                column=column + 1,
                pady=(0 if row == 0 else 12, 0),
                sticky="ew",
            )
            ttk.Label(
                tuning,
                text=hint,
                style="Card.TLabel",
                foreground="#94A3B8",
            ).grid(
                row=row,
                column=column + 2,
                padx=(8, 0),
                pady=(0 if row == 0 else 12, 0),
                sticky="w",
            )

        footer = ttk.Frame(tuning, style="Surface.TFrame")
        footer.grid(row=3, column=0, columnspan=6, pady=(16, 0), sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(
            footer,
            textvariable=self.settings_status_text,
            style="Card.TLabel",
            foreground="#64748B",
            wraplength=720,
        ).grid(row=0, column=0, sticky="w")
        self.save_config_button = ttk.Button(
            footer,
            text="保存系统配置",
            style="Accent.TButton",
            command=self.save_system_config,
        )
        self.save_config_button.grid(row=0, column=1, padx=(12, 0), sticky="e")

        note = ttk.LabelFrame(tab, text="说明", padding=12)
        note.grid(row=3, column=0, pady=(12, 0), sticky="ew")
        ttk.Label(
            note,
            text=(
                "API Key 仅保存在本机 config.local.json，输入框默认隐藏；"
                "Crossref 邮箱用于礼貌访问接口，无需注册账号。"
                "Elsevier PDF 默认先尝试出版版 API，再尝试学校机构下载；"
                "AAM/XML 默认关闭，勾选后会在学校机构之前依次尝试。"
            ),
            style="Card.TLabel",
            foreground="#64748B",
            wraplength=900,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

    def reload_system_config(self) -> None:
        self._load_config_into_vars()
        if self.settings_status_text.get().startswith("配置读取失败"):
            messagebox.showerror("配置读取失败", self.settings_status_text.get())

    def _command(self, *arguments: str) -> list[str]:
        if self.download_kernel.suffix.casefold() == ".py":
            interpreter = "python" if getattr(sys, "frozen", False) else sys.executable
            return [interpreter, "-B", str(self.download_kernel), *arguments]
        return [str(self.download_kernel), *arguments]

    @staticmethod
    def _parse_json_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            message = completed.stderr.strip() or completed.stdout.strip() or "CLI 未返回 JSON"
            return {"ok": False, "reason": message, "returncode": completed.returncode}
        payload["returncode"] = completed.returncode
        if completed.stderr.strip():
            payload["diagnostic"] = completed.stderr.strip()
        return payload

    def _run_cli(self, *arguments: str) -> dict[str, Any]:
        command_timeout = 330 if arguments and arguments[0] == "login" else 60
        try:
            completed = subprocess.run(
                self._command(*arguments),
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                creationflags=windows_no_window_flags(),
                timeout=command_timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error_type": "cli_timeout",
                "reason": f"CLI 辅助操作超过 {command_timeout} 秒，已停止等待。",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "error_type": "python_not_found",
                "reason": "本机没有 Python 环境；请安装 Python 并确保系统命令 python 可用。",
            }
        return self._parse_json_output(completed)

    def _set_config_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for widget_name in (
            "save_config_button",
            "check_sources_button",
            "login_button",
            "refresh_schools_button",
            "elsevier_key_entry",
            "openalex_key_entry",
            "crossref_email_entry",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.configure(state=state)
        if hasattr(self, "school_combo"):
            self.school_combo.configure(state=state)

    def _start_worker(self, kind: str, function: Any) -> None:
        if self._busy:
            messagebox.showinfo("请稍候", "当前操作尚未完成。")
            return
        self._busy = True
        self._set_config_controls_enabled(False)
        self.download_button.configure(state="disabled", text="开始下载", style="Accent.TButton")
        self.progress.configure(mode="indeterminate", maximum=1, value=0)
        self.progress_text.set("正在处理…")
        self.progress.start(12)

        def target() -> None:
            try:
                payload = function()
            except FileNotFoundError:
                self._events.put(
                    (
                        "download_error",
                        {
                            "error_type": "python_not_found",
                            "reason": "本机没有 Python 环境；请安装 Python 并确保系统命令 python 可用。",
                        },
                    )
                )
            except Exception as exc:
                payload = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            self._events.put((kind, payload))

        threading.Thread(target=target, daemon=True).start()

    def _finish_worker(self) -> None:
        self._busy = False
        self._download_active = False
        self._stop_requested = False
        with self._process_lock:
            self._download_process = None
        self.download_button.configure(
            state="normal", text="开始下载", style="Accent.TButton"
        )
        self._set_config_controls_enabled(True)
        self.progress.stop()
        self.progress.configure(mode="determinate")
        self._current_item_doi = ""
        self._current_item_started_at = None

    def _start_download_worker(self, arguments: list[str], total: int) -> None:
        if self._busy:
            messagebox.showinfo("请稍候", "当前操作尚未完成。")
            return
        self._busy = True
        self._download_active = True
        self._stop_requested = False
        self._set_config_controls_enabled(False)
        self.download_button.configure(
            state="normal", text="停止下载", style="Stop.TButton"
        )
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(1, total), value=0)
        self.progress_text.set(f"0 / {total}")

        def target() -> None:
            saw_terminal = False
            diagnostics = ""
            process: subprocess.Popen[str] | None = None
            stderr_lines: list[str] = []
            stderr_thread: threading.Thread | None = None
            try:
                process = subprocess.Popen(
                    self._command(*arguments),
                    cwd=str(APP_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=windows_no_window_flags(),
                )
                with self._process_lock:
                    self._download_process = process
                    stop_immediately = self._stop_requested
                if stop_immediately:
                    self._force_stop_process(process)
                if process.stderr is not None:
                    def drain_stderr() -> None:
                        assert process is not None and process.stderr is not None
                        for stderr_line in process.stderr:
                            stderr_lines.append(stderr_line)
                            if len(stderr_lines) > 300:
                                del stderr_lines[:100]

                    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
                    stderr_thread.start()
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        self._events.put(
                            (
                                "download_error",
                                {"reason": f"CLI 返回了无法解析的进度事件：{line[:160]}"},
                            )
                        )
                        continue
                    event_name = str(payload.get("event") or "")
                    if event_name == "start":
                        self._events.put(("download_start", payload))
                    elif event_name == "item_start":
                        self._events.put(("download_item_start", payload))
                    elif event_name == "result":
                        self._events.put(("download_result", payload))
                    elif event_name == "challenge":
                        self._events.put(("download_challenge", payload))
                    elif event_name == "result_update":
                        self._events.put(("download_result_update", payload))
                    elif event_name == "complete":
                        saw_terminal = True
                        if not self._stop_requested:
                            self._events.put(("download_complete", payload))
                    elif event_name == "error":
                        saw_terminal = True
                        self._events.put(("download_error", payload))
                try:
                    returncode = process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._force_stop_process(process)
                    returncode = process.returncode if process.returncode is not None else -9
                if stderr_thread is not None:
                    stderr_thread.join(timeout=2)
                diagnostics = "".join(stderr_lines).strip()
                if self._stop_requested:
                    self._events.put(
                        (
                            "download_stopped",
                            {
                                "completed": self._download_completed,
                                "total": total,
                                "returncode": returncode,
                            },
                        )
                    )
                elif not saw_terminal:
                    self._events.put(
                        (
                            "download_error",
                            {
                                "reason": diagnostics or f"CLI 提前退出，退出码 {returncode}",
                                "returncode": returncode,
                            },
                        )
                    )
            except Exception as exc:
                if self._stop_requested:
                    self._events.put(
                        ("download_stopped", {"completed": self._download_completed, "total": total})
                    )
                else:
                    self._events.put(
                        ("download_error", {"reason": f"{type(exc).__name__}: {exc}"})
                    )
            finally:
                with self._process_lock:
                    if self._download_process is process:
                        self._download_process = None

        threading.Thread(target=target, daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "schools":
                    self._finish_worker()
                    self._handle_schools(payload)
                elif kind == "school_select":
                    self._finish_worker()
                    self._handle_school_select(payload)
                elif kind == "check":
                    self._finish_worker()
                    self._handle_check(payload)
                elif kind == "login":
                    self._finish_worker()
                    self._handle_login(payload)
                elif kind == "download_start":
                    self._handle_download_start(payload)
                elif kind == "download_item_start":
                    self._handle_download_item_start(payload)
                elif kind == "download_result":
                    self._handle_download_result(payload)
                elif kind == "download_challenge":
                    self._handle_download_challenge(payload)
                elif kind == "download_result_update":
                    self._handle_download_result_update(payload)
                elif kind == "download_complete":
                    self._finish_worker()
                    self._handle_download_complete(payload)
                elif kind == "download_stopped":
                    self._finish_worker()
                    self._handle_download_stopped(payload)
                elif kind == "download_error":
                    self._finish_worker()
                    self._handle_download_error(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def load_schools(self) -> None:
        self.school_detail_text.set("正在读取内置 schools.json 学校配置……")
        self._start_worker(
            "schools",
            lambda: self._run_cli(
                "schools", "--config", str(self._config_file_path()), "--json"
            ),
        )

    def _handle_schools(self, payload: dict[str, Any]) -> None:
        if not payload.get("ok"):
            self.school_detail_text.set("学校配置库读取失败")
            self.status_text.set(str(payload.get("reason") or "学校配置库读取失败"))
            if self._loading_schools_initially:
                self._loading_schools_initially = False
                self.after(80, self.check_sources)
            return
        self._school_entries = [
            item for item in (payload.get("schools") or []) if isinstance(item, dict)
        ]
        self._school_by_display = {
            str(item.get("display") or item.get("name") or ""): item
            for item in self._school_entries
            if item.get("name")
        }
        displays = list(self._school_by_display)
        self.school_combo.configure(values=displays)
        catalog_value = payload.get("catalog_path")
        if catalog_value:
            self._school_catalog_path = Path(str(catalog_value))
        selected_id = str(payload.get("selected_school_id") or "")
        selected_name = str(payload.get("selected_school_name") or "")
        selected = next(
            (
                item
                for item in self._school_entries
                if str(item.get("id") or "") == selected_id
                or str(item.get("name") or "") == selected_name
            ),
            None,
        )
        if selected is not None:
            self._show_school_entry(selected)
        elif displays:
            self.school_query.set(selected_name or "输入学校名搜索")
            self.school_detail_text.set(
                f"当前学校不在配置库中；共加载 {len(displays)} 所学校"
            )
        else:
            self.school_query.set("未找到学校配置")
            self.school_detail_text.set("请编辑 schools.json 后刷新")
        if self._loading_schools_initially:
            self._loading_schools_initially = False
            self.after(80, self.check_sources)

    def _show_school_entry(self, entry: dict[str, Any]) -> None:
        display = str(entry.get("display") or entry.get("name") or "")
        self.school_query.set(display)
        origin = {
            "builtin": "内置（无需 schools.json）",
            "catalog": "内置学校库",
            "local": "本地配置",
        }.get(str(entry.get("origin") or ""), "配置库")
        detail = " · ".join(
            value
            for value in (
                str(entry.get("province") or ""),
                str(entry.get("type_label") or entry.get("type") or ""),
                origin,
            )
            if value
        )
        self.school_detail_text.set(detail)

    def _filter_school_dropdown(self, event: tk.Event[Any]) -> None:
        if event.keysym in {"Up", "Down", "Left", "Right", "Escape", "Tab"}:
            return
        query = self.school_query.get().strip().casefold()
        if not query:
            matches = list(self._school_by_display)
        else:
            matches = [
                display
                for display, entry in self._school_by_display.items()
                if query in display.casefold()
                or query in str(entry.get("base_url") or "").casefold()
                or query in str(entry.get("gateway") or "").casefold()
            ]
        self.school_combo.configure(values=matches)
        if event.keysym == "Return" and matches:
            self.school_query.set(matches[0])
            self._select_school_from_combo()

    def _select_school_from_combo(self, _event: tk.Event[Any] | None = None) -> None:
        value = self.school_query.get().strip()
        entry = self._school_by_display.get(value)
        if entry is None:
            matches = [
                item
                for display, item in self._school_by_display.items()
                if value.casefold() in display.casefold()
            ]
            if not matches:
                self.school_detail_text.set(
                    "未找到该学校；可修改 schools.json 后刷新配置库"
                )
                return
            entry = matches[0]
            self._show_school_entry(entry)
        school_id = str(entry.get("id") or entry.get("name") or "")
        if not self.save_system_config(silent=True):
            self.notebook.select(self.settings_tab)
            messagebox.showerror("配置无效", self.settings_status_text.get())
            return
        self.school_detail_text.set("正在应用学校配置……")
        self._start_worker(
            "school_select",
            lambda: self._run_cli(
                "select-school",
                school_id,
                "--config",
                str(self._config_file_path()),
                "--json",
            ),
        )

    def _handle_school_select(self, payload: dict[str, Any]) -> None:
        selected = payload.get("selected") or {}
        if not payload.get("ok"):
            self.school_detail_text.set("学校配置应用失败")
            messagebox.showerror(
                "学校选择失败", str(payload.get("reason") or "未知错误")
            )
            return
        self._show_school_entry(selected)
        self._expired_prompted = False
        self.status_text.set(
            f"已选择 {selected.get('name')} · {selected.get('type_label') or selected.get('type')}"
        )
        self.after(80, self.check_sources)

    def open_schools_file(self) -> None:
        path = self._school_catalog_path
        if not path.exists():
            messagebox.showerror("配置库不存在", f"找不到：{path}")
            return
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def check_sources(self) -> None:
        self.status_text.set("正在检查 API 和所选机构接入……")
        self._start_worker(
            "check",
            lambda: self._run_cli(
                "check", "--config", str(self._config_file_path()), "--json"
            ),
        )

    def _handle_check(self, payload: dict[str, Any]) -> None:
        providers = payload.get("providers") or {}
        oa = providers.get("openalex") or {}
        elsevier = providers.get("elsevier") or {}
        institution = providers.get("institution") or providers.get("webvpn") or {}
        school_name = str(institution.get("school_name") or "学校")
        access_label = str(
            institution.get("access_type_label")
            or institution.get("access_type")
            or "机构接入"
        )
        if institution.get("access_type") == "direct":
            institution_status = "校园网直连可尝试（权限以实际下载为准）"
        elif institution.get("valid"):
            institution_status = f"{school_name} {access_label} 会话可用"
        elif institution.get("can_attempt"):
            institution_status = (
                f"{school_name} {access_label} 未验证（可尝试当前网络）"
            )
        else:
            institution_status = f"{school_name} {access_label} 需处理"
        parts = [
            "OpenAlex 可用" if oa.get("enabled") else "OpenAlex 不可用",
            "Elsevier Key 已配置" if elsevier.get("has_api_key") else "Elsevier Key 缺失",
            institution_status,
        ]
        self.status_text.set(" · ".join(parts))
        self.progress_text.set("等待任务")
        self.progress.configure(value=0, maximum=1)
        if (
            not institution.get("valid")
            and institution.get("login_supported", True)
            and not self._expired_prompted
        ):
            self._expired_prompted = True
            if messagebox.askyesno(
                "学校登录已失效",
                f"检测到 {school_name} {access_label} 会话不可用。"
                "是否现在打开登录窗口手动登录？\n\n"
                "即使暂不登录，OpenAlex 和 Elsevier API 仍可使用。",
            ):
                self.login_webvpn()

    def login_webvpn(self) -> None:
        self.status_text.set("正在启动所选学校的机构登录适配器……")
        self._start_worker(
            "login",
            lambda: self._run_cli(
                "login", "--config", str(self._config_file_path()), "--json"
            ),
        )

    def _handle_login(self, payload: dict[str, Any]) -> None:
        self.progress_text.set("等待任务")
        self.progress.configure(value=0, maximum=1)
        if payload.get("ok"):
            self._expired_prompted = False
            if payload.get("no_login_required"):
                message = str(
                    payload.get("reason")
                    or "校园网直连无需登录，可以直接开始下载。"
                )
                self.status_text.set(message)
                messagebox.showinfo("无需登录", message)
            elif payload.get("external_login_required"):
                message = str(
                    payload.get("reason")
                    or "请先在学校校园 VPN 客户端中完成连接，然后开始下载。"
                )
                self.status_text.set(message)
                messagebox.showinfo("需要外部校园 VPN", message)
            else:
                school = str(payload.get("school_name") or "学校")
                access = str(
                    payload.get("access_type_label")
                    or payload.get("access_type")
                    or "机构"
                )
                self.status_text.set(f"{school} {access} 会话已刷新并验证成功")
                messagebox.showinfo(
                    "登录成功", f"{school} {access} 会话已经可以用于下载。"
                )
        else:
            self.status_text.set("学校登录未完成")
            messagebox.showerror("登录失败", str(payload.get("reason") or payload.get("session") or "未知错误"))

    def _selected_sources(self) -> list[str]:
        sources: list[str] = []
        if self.use_openalex.get():
            sources.append("openalex")
        if self.use_elsevier.get():
            sources.append("elsevier")
        if self.use_webvpn.get():
            sources.append("webvpn")
        return sources

    def _dois(self) -> list[str]:
        value = self.doi_text.get("1.0", "end").strip()
        for separator in (",", ";", "\t"):
            value = value.replace(separator, "\n")
        output: list[str] = []
        for line in value.splitlines():
            output.extend(token for token in line.split() if token)
        return list(dict.fromkeys(output))

    def _download_arguments(self, dois: list[str], sources: list[str]) -> list[str]:
        output_dir = Path(self.output_dir.get()).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        arguments = [
            "download",
            *dois,
            "--config",
            str(self._config_file_path()),
            "--output-dir",
            str(output_dir),
            "--sources",
            ",".join(sources),
            "--format",
            self.output_format.get(),
        ]
        if self.overwrite.get():
            arguments.append("--overwrite")
        arguments.append("--interactive-browser")
        arguments.append("--json-lines")
        return arguments

    def start_download(self) -> None:
        dois = self._dois()
        sources = self._selected_sources()
        if not dois:
            messagebox.showwarning("缺少 DOI", "请至少输入一个 DOI。")
            return
        if not sources:
            messagebox.showwarning("缺少来源", "请至少选择一个下载来源。")
            return
        if not self.save_system_config(silent=True):
            self.notebook.select(self.settings_tab)
            messagebox.showerror("配置无效", self.settings_status_text.get())
            return
        arguments = self._download_arguments(dois, sources)
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)
        self._result_paths.clear()
        self._result_links.clear()
        self._result_items.clear()
        self._download_total = len(dois)
        self._download_completed = 0
        self._download_successes = 0
        self._batch_started_at = time.monotonic()
        self.total_time_text.set("本次任务总耗时：正在计时……")
        self.status_text.set(f"正在下载 {len(dois)} 篇……")
        self._start_download_worker(arguments, len(dois))

    def toggle_download(self) -> None:
        if self._download_active:
            self.stop_download()
        else:
            self.start_download()

    def stop_download(self) -> None:
        if not self._download_active:
            return
        self._stop_requested = True
        self.download_button.configure(
            state="disabled", text="正在停止…", style="Stop.TButton"
        )
        self.status_text.set(
            f"正在立即停止下载 · 已完成 {self._download_completed}/{self._download_total}"
        )
        with self._process_lock:
            process = self._download_process
        if process is not None and process.poll() is None:
            threading.Thread(
                target=self._force_stop_process,
                args=(process,),
                daemon=True,
            ).start()

    @staticmethod
    def _force_stop_process(process: subprocess.Popen[str]) -> None:
        """Terminate the CLI and any renderer child processes without a graceful wait."""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                # taskkill can return a non-zero status without raising when it
                # is restricted. Always verify and kill the parent directly.
                if process.poll() is None:
                    process.kill()
            else:
                process.kill()
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    def _handle_download_start(self, payload: dict[str, Any]) -> None:
        self._download_total = int(payload.get("total") or self._download_total or 1)
        self.progress.configure(maximum=max(1, self._download_total), value=0)
        self.progress_text.set(f"0 / {self._download_total}")

    def _handle_download_item_start(self, payload: dict[str, Any]) -> None:
        doi = str(payload.get("doi") or "")
        timeout = int(payload.get("timeout_seconds") or 60)
        self._current_item_doi = doi
        self._current_item_started_at = time.monotonic()
        self._current_item_timeout = timeout
        self._update_current_item_elapsed(doi)

    def _update_current_item_elapsed(self, doi: str) -> None:
        if (
            not self._download_active
            or doi != self._current_item_doi
            or self._current_item_started_at is None
        ):
            return
        elapsed = time.monotonic() - self._current_item_started_at
        self.status_text.set(
            f"正在处理 {doi} · 已用 {format_elapsed(elapsed)} · "
            f"安全上限 {self._current_item_timeout} 秒"
        )
        self.after(1000, self._update_current_item_elapsed, doi)

    def _handle_download_result(self, payload: dict[str, Any]) -> None:
        result = payload.get("result") or {}
        ok = bool(result.get("ok"))
        doi = str(result.get("doi") or "")
        if doi == self._current_item_doi:
            self._current_item_doi = ""
            self._current_item_started_at = None
        self._download_completed = int(
            payload.get("completed") or self._download_completed + 1
        )
        self._download_total = int(payload.get("total") or self._download_total or 1)
        self._download_successes += int(ok)
        failures = self._download_completed - self._download_successes
        self.progress.configure(
            maximum=max(1, self._download_total),
            value=self._download_completed,
        )
        self.progress_text.set(
            f"{self._download_completed} / {self._download_total}"
        )
        self.status_text.set(
            f"已完成 {self._download_completed}/{self._download_total} · "
            f"成功 {self._download_successes} · 失败 {failures}"
        )

        size = result.get("bytes") or 0
        size_text = f"{size / 1024 / 1024:.1f} MB" if size else ""
        artifact_value = result.get("path") if ok else result.get("error_log_path")
        result_path = Path(str(artifact_value)) if artifact_value else None
        elapsed_text = format_elapsed(result.get("elapsed_s"))
        message = "" if ok else str(result.get("reason") or "")
        item_id = self.result_tree.insert(
            "",
            "end",
            values=(
                result.get("doi", ""),
                "成功" if ok else "失败",
                result.get("source", ""),
                size_text,
                elapsed_text,
                message,
            ),
            tags=("success" if ok else "failure",),
        )
        if result_path:
            self._result_paths[item_id] = result_path
        self._result_links[item_id] = self._result_link(result)
        if doi:
            self._result_items[doi] = item_id
        children = self.result_tree.get_children()
        if children:
            self.result_tree.see(children[-1])

    def _handle_download_challenge(self, payload: dict[str, Any]) -> None:
        host = str(payload.get("host") or "出版社")
        count = int(payload.get("count") or 1)
        self.status_text.set(
            f"普通下载已全部完成 · {host} 有 {count} 篇需手动处理，"
            "请在失败条目右键复制链接"
        )
        self.progress_text.set("等待手动处理")

    def _handle_download_result_update(self, payload: dict[str, Any]) -> None:
        result = payload.get("result") or {}
        doi = str(result.get("doi") or "")
        item_id = self._result_items.get(doi)
        if not item_id:
            return
        previous_values = self.result_tree.item(item_id, "values") or ()
        previous_ok = len(previous_values) > 1 and str(previous_values[1]) == "成功"
        ok = bool(result.get("ok"))
        if ok != previous_ok:
            self._download_successes += 1 if ok else -1
        failures = self._download_completed - self._download_successes
        size = result.get("bytes") or 0
        size_text = f"{size / 1024 / 1024:.1f} MB" if size else ""
        artifact_value = result.get("path") if ok else result.get("error_log_path")
        result_path = Path(str(artifact_value)) if artifact_value else None
        elapsed_text = format_elapsed(result.get("elapsed_s"))
        message = "人工验证后下载成功" if ok else str(result.get("reason") or "")
        self.result_tree.item(
            item_id,
            values=(
                doi,
                "成功" if ok else "失败",
                result.get("source", ""),
                size_text,
                elapsed_text,
                message,
            ),
            tags=("success" if ok else "failure",),
        )
        self._result_paths.pop(item_id, None)
        if result_path:
            self._result_paths[item_id] = result_path
        self._result_links[item_id] = self._result_link(result)
        self.status_text.set(
            f"人工验证回写完成 · 成功 {self._download_successes} · 失败 {failures}"
        )
        self.progress_text.set(f"{self._download_completed} / {self._download_total}")

    def _set_total_elapsed(self, seconds: Any = None) -> None:
        if seconds is None and self._batch_started_at is not None:
            seconds = time.monotonic() - self._batch_started_at
        elapsed_text = format_elapsed(seconds) or "—"
        self.total_time_text.set(f"本次任务总耗时：{elapsed_text}")
        self._batch_started_at = None

    def _handle_download_complete(self, payload: dict[str, Any]) -> None:
        total = int(payload.get("total") or self._download_total)
        successes = int(payload.get("successes") or 0)
        failures = int(payload.get("failures") or 0)
        self.progress.configure(maximum=max(1, total), value=total)
        self.progress_text.set(f"{total} / {total}")
        self.status_text.set(
            f"下载完成 · 成功 {successes} · 失败 {failures}"
        )
        self._set_total_elapsed(payload.get("elapsed_s"))

    def _handle_download_stopped(self, payload: dict[str, Any]) -> None:
        completed = int(payload.get("completed") or self._download_completed)
        total = int(payload.get("total") or self._download_total)
        self.progress.configure(maximum=max(1, total), value=completed)
        self.progress_text.set(f"{completed} / {total}（已停止）")
        self.status_text.set(
            f"下载已停止 · 已完成 {completed}/{total} · 未继续等待当前下载"
        )
        self._set_total_elapsed(payload.get("elapsed_s"))

    def _handle_download_error(self, payload: dict[str, Any]) -> None:
        self.status_text.set("下载任务异常结束")
        self._set_total_elapsed(payload.get("elapsed_s"))
        messagebox.showerror(
            "下载失败",
            str(payload.get("reason") or "CLI 下载进程异常结束"),
        )

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get())
        if selected:
            self.output_dir.set(selected)

    def open_output_dir(self) -> None:
        path = Path(self.output_dir.get()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def open_selected_file(self, _event: tk.Event[Any]) -> None:
        selected = self.result_tree.selection()
        if not selected:
            return
        path = self._result_paths.get(selected[0])
        if path is None:
            return
        if not path.is_file():
            self.status_text.set(f"文件已不存在：{path.name}")
            return
        if os.name == "nt":
            subprocess.Popen(
                ["explorer.exe", "/select,", str(path.resolve())],
                creationflags=windows_no_window_flags(),
            )
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])

    def _show_result_context_menu(self, event: tk.Event[Any]) -> str:
        if getattr(event, "num", None) == 3:
            item_id = self.result_tree.identify_row(event.y)
            if not item_id:
                return "break"
            # Keep an existing multi-selection when the user right-clicks one
            # of its rows.  A right-click outside the selection starts a new
            # single-row selection, matching normal Windows list behavior.
            if item_id not in self.result_tree.selection():
                self.result_tree.selection_set(item_id)
            self.result_tree.focus(item_id)
            x_root, y_root = event.x_root, event.y_root
        else:
            selected = self.result_tree.selection()
            if not selected:
                return "break"
            box = self.result_tree.bbox(selected[0])
            x_root = self.result_tree.winfo_rootx() + (box[0] if box else 0) + 12
            y_root = self.result_tree.winfo_rooty() + (box[1] if box else 0) + 24
        selected = self.result_tree.selection()
        selected_count = len(selected)
        retry_state = (
            "disabled"
            if self._busy or self._download_active or not self._selected_result_dois()
            else "normal"
        )
        link_state = "normal" if self._selected_result_links() else "disabled"
        self.result_context_menu.entryconfigure(
            0,
            label=(
                f"重试下载（{selected_count} 项）"
                if selected_count > 1
                else "重试下载"
            ),
        )
        self.result_context_menu.entryconfigure(
            1,
            label=(
                f"复制链接（{selected_count} 项）"
                if selected_count > 1
                else "复制链接"
            ),
        )
        self.result_context_menu.entryconfigure(
            3,
            label=(
                f"复制 DOI（{selected_count} 项）"
                if selected_count > 1
                else "复制 DOI"
            ),
        )
        self.result_context_menu.entryconfigure(0, state=retry_state)
        self.result_context_menu.entryconfigure(1, state=link_state)
        try:
            self.result_context_menu.tk_popup(x_root, y_root)
        finally:
            self.result_context_menu.grab_release()
        return "break"

    def _select_all_results(self, _event: tk.Event[Any] | None = None) -> str:
        children = self.result_tree.get_children()
        if children:
            self.result_tree.selection_set(*children)
            self.result_tree.focus(children[0])
        return "break"

    @staticmethod
    def _result_link(result: dict[str, Any]) -> str:
        manual_url = str(result.get("manual_download_url") or "").strip()
        if manual_url.startswith(("https://", "http://")):
            return manual_url
        doi = str(result.get("doi") or "").strip()
        if not doi:
            return ""
        return "https://doi.org/" + urllib.parse.quote(doi, safe="/()")

    def _copy_selected_link(self) -> None:
        links = self._selected_result_links()
        if not links:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(links))
        self.update_idletasks()
        self.status_text.set(f"已复制 {len(links)} 个下载链接")

    def _selected_result_dois(self) -> list[str]:
        selected = set(self.result_tree.selection())
        dois: list[str] = []
        for item_id in self.result_tree.get_children():
            if item_id not in selected:
                continue
            values = self.result_tree.item(item_id, "values") or ()
            doi = str(values[0]).strip() if values else ""
            if doi and doi not in dois:
                dois.append(doi)
        return dois

    def _selected_result_links(self) -> list[str]:
        selected = set(self.result_tree.selection())
        links: list[str] = []
        for item_id in self.result_tree.get_children():
            if item_id not in selected:
                continue
            link = self._result_links.get(item_id, "").strip()
            if link and link not in links:
                links.append(link)
        return links

    def _retry_selected_download(self) -> None:
        if self._busy or self._download_active:
            messagebox.showinfo("请稍候", "当前任务完成后才能重试。")
            return
        selected = self.result_tree.selection()
        if not selected:
            return
        selected_items = list(selected)
        dois = self._selected_result_dois()
        if not dois:
            return
        sources = self._selected_sources()
        if not sources:
            messagebox.showwarning("缺少来源", "请至少选择一个下载来源。")
            return
        if not self.save_system_config(silent=True):
            self.notebook.select(self.settings_tab)
            messagebox.showerror("配置无效", self.settings_status_text.get())
            return

        arguments = self._download_arguments(dois, sources)
        for item_id in selected_items:
            values = self.result_tree.item(item_id, "values") or ()
            doi = str(values[0]).strip() if values else ""
            self.result_tree.delete(item_id)
            self._result_paths.pop(item_id, None)
            self._result_links.pop(item_id, None)
            if doi and self._result_items.get(doi) == item_id:
                self._result_items.pop(doi, None)
        self._download_total = len(dois)
        self._download_completed = 0
        self._download_successes = 0
        self._batch_started_at = time.monotonic()
        self.total_time_text.set("本次任务总耗时：正在计时……")
        if len(dois) == 1:
            self.status_text.set(f"正在重试 {dois[0]}……")
        else:
            self.status_text.set(f"正在重试 {len(dois)} 篇……")
        self._start_download_worker(arguments, len(dois))

    def _copy_selected_doi(self) -> None:
        dois = self._selected_result_dois()
        if not dois:
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(dois))
        self.update_idletasks()
        self.status_text.set(f"已复制 {len(dois)} 个 DOI")

    def _on_close(self) -> None:
        if self._download_active:
            self._stop_requested = True
            with self._process_lock:
                process = self._download_process
            if process is not None and process.poll() is None:
                threading.Thread(
                    target=self._force_stop_process,
                    args=(process,),
                    daemon=True,
                ).start()
        self.destroy()


def main() -> int:
    kernel = resolve_download_kernel()
    if not kernel.exists():
        messagebox.showerror(
            "缺少下载内核",
            "同目录下既没有 literature_download_cli.py，"
            "也没有 literature_download_cli.exe。",
        )
        return 2
    python_error = system_python_error(kernel)
    if python_error:
        messagebox.showerror("缺少 Python 环境", python_error)
        return 2
    app = DownloadApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
