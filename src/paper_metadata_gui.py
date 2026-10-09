#!/usr/bin/env python3
"""Tkinter PDF library and Zotero RIS exporter.

Python 3.10+, requests; optional pypdf for recovering damaged DOI filenames.
Shares config.local.json with the downloader; backend is independent of Tk.
"""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import queue
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser

try:
    from paper_metadata_core import (
        FIELDS, ORDER, PROVIDER_LABELS, LABELS, clean, doi_normalize,
        provider_available, enabled_providers, missing, status_of, missing_text,
        trusted_record, complete_metadata,
    )
except ImportError:
    if __name__ == '__main__':
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('无法加载模块', '请将 paper_metadata_core.py 与本脚本放在同一目录。\n系统 Python 需安装 requests；PDF 提取需 pypdf。')
        root.destroy()
        raise SystemExit(1)
    raise

from suite_paths import APP_DIR
# External writable configuration stays beside the script/executable. Bundle
# resources, if added later, must use this separate read-only resource root.
RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', APP_DIR)).resolve()
COLUMNS = ('status', 'title', 'doi', 'authors', 'year', 'journal', 'volume', 'issue',
           'pages', 'article_number', 'date', 'journal_abbreviation', 'issn', 'language', 'publisher', 'abstract', 'updated_at')
DEFAULT_COLUMNS = COLUMNS[:10]
FIXED_COLUMNS = ('title', 'doi')
COLUMN_LABELS = {**LABELS, 'status': '状态', 'updated_at': '上次补全时间'}


def file_stem(doi):
    return re.sub(r'[^A-Za-z0-9._-]+', '_', doi).strip('._').lower()


def recover_doi(path):
    """Do not guess characters lost by the downloader's filename sanitizer."""
    stem = path.stem
    match = re.fullmatch(r'(10\.\d{4,9})[_/](.+)', stem, re.I)
    if match and '_' not in match[2] and not re.search(r'[<>:;#()]', match[2]):
        return doi_normalize(match[1] + '/' + match[2]), 'filename'
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        text = '\n'.join(str(v) for v in (reader.metadata or {}).values())
        text += '\n' + '\n'.join(p.extract_text() or '' for p in reader.pages[:2])
        for page in reader.pages[:2]:
            for annotation in page.get('/Annots', []):
                action = annotation.get_object().get('/A') or {}
                if hasattr(action, 'get_object'):
                    action = action.get_object()
                uri = action.get('/URI')
                if uri:
                    text += '\n' + urllib.parse.unquote(str(uri))
        candidates = set()
        for raw in re.findall(r'10\.\d{4,9}/[^\s"]+', text, re.I):
            candidate = raw.rstrip('.,')
            # Parentheses are valid DOI characters; strip only an unmatched
            # closing delimiter surrounding a citation, not a balanced suffix.
            for opening, closing in (('(', ')'), ('[', ']'), ('{', '}')):
                while candidate.endswith(closing) and candidate.count(closing) > candidate.count(opening):
                    candidate = candidate[:-1]
            doi = doi_normalize(candidate)
            if doi and file_stem(doi) == file_stem(stem):
                candidates.add(doi)
        if len(candidates) == 1:
            return candidates.pop(), 'pdf'
    except Exception:
        pass
    return '', '需要手动填写 DOI（文件名中的标点已丢失或 PDF 无可提取文本）'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write(value)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_settings(base):
    base = Path(base).resolve()
    path = base / 'config.local.json'
    data = read_json(path) if path.exists() else {}
    metadata = data.get('metadata') or {}
    from suite_settings import pdf_directory
    folder = pdf_directory(data, base)
    return {'contact_email': data.get('contact_email', ''),
            'api_keys': dict(data.get('api_keys') or {}),
            'timeout': (data.get('network') or {}).get('timeout_seconds', 25),
            'pdf_dir': str(folder if folder.is_absolute() else base / folder),
            'enabled_apis': metadata.get('enabled_apis', list(ORDER)),
            'visible_columns': metadata.get('visible_columns', list(DEFAULT_COLUMNS)),
            'sort_column': metadata.get('sort_column', ''),
            'sort_descending': bool(metadata.get('sort_descending', False))}


def save_settings(base, changes):
    """Read fresh, merge only edited fields, preserve every downloader option."""
    path = Path(base).resolve() / 'config.local.json'
    data = read_json(path) if path.exists() else {}
    if 'contact_email' in changes:
        data['contact_email'] = changes['contact_email']
    if changes.get('api_keys'):
        data.setdefault('api_keys', {}).update(changes['api_keys'])
    if 'pdf_dir' in changes:
        data.setdefault('metadata', {})['pdf_dir'] = str(Path(changes['pdf_dir']).resolve())
    for key in ('enabled_apis', 'visible_columns', 'sort_column', 'sort_descending'):
        if key in changes:
            data.setdefault('metadata', {})[key] = changes[key]
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return load_settings(base)


def visible_columns(values):
    return [name for name in COLUMNS if name in values or name in FIXED_COLUMNS]


def column_value(record, column):
    if column == 'status':
        return status_of(record)
    if column == 'authors':
        return '; '.join(record.get('authors') or [])
    if column == 'title':
        return record.get('title') or Path(next(iter(record.get('files') or []), '')).name
    return str(record.get(column) or '')


def sorted_records(records, column, descending=False):
    """Natural ordering (2 before 10); absent values stay last both ways."""
    if column not in COLUMNS:
        return list(records)
    present, absent = [], []
    for record in records:
        (present if column_value(record, column).strip() else absent).append(record)
    def key(record):
        return tuple((1, int(token)) if token.isdigit() else (0, token.casefold())
                     for token in re.split(r'(\d+)', column_value(record, column)))
    return sorted(present, key=key, reverse=descending) + absent


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS papers (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def all(self):
        with self.connect() as db:
            return [dict(json.loads(data), id=pk) for pk, data in db.execute('SELECT id,data FROM papers ORDER BY id DESC')]

    def save(self, record):
        record = copy.deepcopy(record)
        pk = record.pop('id', None)
        with self.connect() as db:
            data = json.dumps(record, ensure_ascii=False)
            if pk:
                db.execute('UPDATE papers SET data=? WHERE id=?', (data, pk))
            else:
                pk = db.execute('INSERT INTO papers(data) VALUES(?)', (data,)).lastrowid
        return dict(record, id=pk)

    def import_record(self, doi='', path=None, source='manual'):
        papers = self.all()
        target = str(Path(path).resolve()) if path else ''
        for paper in papers:
            if target and any(os.path.normcase(p) == os.path.normcase(target) for p in paper.get('files', [])):
                return paper, False
        for paper in papers:
            if doi and paper.get('doi') == doi:
                if target and target not in paper.get('files', []):
                    paper.setdefault('files', []).append(target)
                    return self.save(paper), True
                return paper, False
        return self.save({'doi': doi, 'doi_source': source, 'files': [target] if target else [],
                          'field_sources': {}}), True

    def link_local_pdfs(self, records, folder):
        """Attach DOI-named PDFs using known DOIs, without reversing lost punctuation."""
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            return records, 0
        papers = self.all()
        by_id = {paper['id']: paper for paper in papers}
        doi_keys = {}
        for paper in papers:
            doi = doi_normalize(paper.get('doi'))
            if doi:
                doi_keys.setdefault(file_stem(doi), set()).add(doi)
        files = {}
        for path in folder.rglob('*'):
            if path.is_file() and path.suffix.lower() == '.pdf':
                files.setdefault(file_stem(path.stem), []).append(path.resolve())
        linked = 0
        result = []
        for selected in records:
            paper = copy.deepcopy(by_id.get(selected.get('id'), selected))
            doi = doi_normalize(paper.get('doi'))
            key = file_stem(doi) if doi else ''
            # Two DOIs can collapse to one sanitized filename. Never choose
            # between known collisions automatically.
            candidates = files.get(key, []) if doi_keys.get(key) == {doi} else []
            known = {os.path.normcase(str(Path(p).resolve())) for p in paper.get('files', [])}
            changed = False
            for path in candidates:
                if os.path.normcase(str(path)) in known:
                    continue
                with path.open('rb') as stream:
                    if b'%PDF-' not in stream.read(1024):
                        continue
                paper.setdefault('files', []).append(str(path))
                known.add(os.path.normcase(str(path)))
                linked += 1
                changed = True
            result.append(self.save(paper) if changed else paper)
        return result, linked

    def scan(self, folder, stop, progress):
        folder = Path(folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError('PDF 目录不存在')
        files = sorted(p for p in folder.rglob('*') if p.is_file() and p.suffix.lower() == '.pdf')
        papers = self.all()
        known = {os.path.normcase(str(Path(f).resolve())): p for p in papers for f in p.get('files', [])}
        by_doi = {}
        doi_keys = {}
        for paper in papers:
            doi = doi_normalize(paper.get('doi'))
            if doi:
                by_doi.setdefault(doi, paper)
                doi_keys.setdefault(file_stem(doi), set()).add(doi)
        added = linked = scanned = 0
        for i, path in enumerate(files, 1):
            if stop.is_set():
                break
            path_key = os.path.normcase(str(path.resolve()))
            previous = known.get(path_key)
            if previous is None or not previous.get('doi'):
                # A searched DOI retains punctuation that a downloaded filename
                # loses. Compare its sanitized name instead of guessing a DOI.
                matches = doi_keys.get(file_stem(path.stem), set())
                if len(matches) == 1:
                    with path.open('rb') as stream:
                        is_pdf = b'%PDF-' in stream.read(1024)
                    doi, source = (next(iter(matches)), 'library_filename') if is_pdf else ('', '非 PDF 文件')
                else:
                    doi, source = recover_doi(path)
                target = by_doi.get(doi_normalize(doi)) if doi else None
                if target is not None:
                    target = copy.deepcopy(target)
                    attached = {os.path.normcase(str(Path(f).resolve())) for f in target.get('files', [])}
                    if path_key not in attached:
                        target.setdefault('files', []).append(str(path.resolve()))
                        target = self.save(target)
                        linked += 1
                    paper = target
                elif previous is not None:
                    previous.update(doi=doi, doi_source=source)
                    paper = self.save(previous)
                    added += int(bool(doi))
                else:
                    paper, changed = self.import_record(doi, path, source)
                    added += int(changed)
                known[path_key] = paper
                if paper.get('doi'):
                    normalized = doi_normalize(paper['doi'])
                    by_doi[normalized] = paper
                    doi_keys.setdefault(file_stem(normalized), set()).add(normalized)
            progress({'completed': i, 'total': len(files), 'current': path.name, 'stage': '扫描 PDF'})
            scanned = i
        return f'{"已停止扫描" if stop.is_set() else "扫描结束"}：已扫描 {scanned}/{len(files)} 个 PDF，新增或补全 {added} 条，为已有文献关联 {linked} 个 PDF'


def ris_text(records):
    blocks, skipped = [], 0
    seen = set()
    for record in records:
        record = trusted_record(record)
        if not record.get('title') or not record.get('doi'):
            skipped += 1
            continue
        doi = record['doi']
        if doi in seen:
            continue
        seen.add(doi)
        lines = ['TY  - JOUR']
        def add(tag, value):
            if value:
                lines.append(f'{tag}  - {clean(value)}')
        add('TI', record.get('title'))
        for author in record.get('authors', []):
            add('AU', author)
        for tag, field in [('PY', 'year'), ('JF', 'journal'), ('T2', 'journal'), ('VL', 'volume'), ('IS', 'issue'),
                           ('DO', 'doi'), ('SN', 'issn'), ('AB', 'abstract'), ('PB', 'publisher'),
                           ('J2', 'journal_abbreviation'), ('LA', 'language')]:
            add(tag, record.get(field))
        date = str(record.get('date') or record.get('year') or '')
        if record.get('year') and not date.startswith(str(record['year'])):
            date = str(record['year'])
        add('DA', date.replace('-', '/'))
        pages = record.get('pages')
        if pages:
            parts = re.split(r'[-–]', pages, maxsplit=1)
            add('SP', parts[0])
            if len(parts) == 2:
                add('EP', parts[1])
        else:
            add('SP', record.get('article_number'))
        if record.get('article_number'):
            add('N1', 'Article number: ' + record['article_number'])
        add('UR', 'https://doi.org/' + doi)
        for file in record.get('files', []):
            if Path(file).is_file():
                add('L1', Path(file).resolve().as_uri())
        if missing(record):
            add('N1', 'Missing metadata: ' + ', '.join(missing(record)))
        lines.append('ER  - ')
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks) + ('\n' if blocks else ''), len(blocks), skipped


def request_log(record):
    """Human-readable, credential-free provider and individual request timings."""
    lines = []
    strategies = {'elsevier_direct': 'Elsevier 官方来源优先',
                  'elsevier_fallback': 'Elsevier 不可用或关键字段不足，继续回退',
                  'standard': '按已启用来源逐级补全'}
    if record.get('metadata_strategy') in strategies:
        lines.append('查询策略：' + strategies[record['metadata_strategy']])
    timed_requests = []
    total = record.get('metadata_elapsed_seconds')
    if isinstance(total, (int, float)):
        lines.append(f'本篇补全用时：{total:.2f} 秒')
    for attempt in record.get('attempts', []):
        provider = PROVIDER_LABELS.get(attempt['provider'], attempt['provider'])
        elapsed = attempt.get('elapsed_seconds')
        timing = f'（{elapsed:.2f} 秒）' if isinstance(elapsed, (int, float)) else ''
        lines.append(provider + timing + '：' + attempt['result'])
        for item in attempt.get('requests', []):
            duration = item.get('elapsed_seconds', 0)
            view = item.get('view') or ''
            status = f"HTTP {item['http_status']}" if item.get('http_status') is not None else item.get('outcome', '')
            lines.append(f"  {item.get('method', 'GET')} {view} · {status} · {duration:.2f} 秒")
            timed_requests.append((duration, provider, view))
    if timed_requests:
        seconds, provider, view = max(timed_requests)
        lines.append(f'最慢请求：{provider} {view}，{seconds:.2f} 秒')
    elif record.get('attempts'):
        lines.append('本记录未保存单次请求耗时；后续补全会自动记录。')
    for item in record.get('skipped_sources', []):
        lines.append(PROVIDER_LABELS.get(item['provider'], item['provider']) + '：跳过，' + item['reason'])
    return '\n'.join(lines) or '尚未查询'


def _resolve_worker(config, paper, disabled, channel):
    """Disposable network worker; parent owns SQLite and can stop it immediately."""
    disabled_sources = set(disabled)
    try:
        result = complete_metadata(paper['doi'], config=config,
            sources=config.get('enabled_apis', ORDER), existing=paper,
            disabled_sources=disabled_sources,
            on_progress=lambda stage: channel.put(('stage', stage)),
            on_checkpoint=lambda record: channel.put(('checkpoint', (record, list(disabled_sources)))))
        channel.put(('done', (result, list(disabled_sources))))
    except Exception as error:
        channel.put(('error', type(error).__name__))


def run_batch(records, library, config, stop, emit, worker_target=None):
    from suite_metadata_runner import run_batch as batch
    return batch(records, library, config, stop, emit)


def enable_dpi_awareness():
    if os.name != 'nt':
        return
    import ctypes
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def run_gui(base, parent=None):
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, filedialog, messagebox

    enable_dpi_awareness()
    base = Path(base).resolve()

    class MultilineSearchEntry(ttk.Frame):
        """Multi-line contents in the same pixel height as the former ttk.Entry."""
        def __init__(self, master, textvariable):
            probe = ttk.Entry(master)
            probe.update_idletasks()
            height = probe.winfo_reqheight()
            entry_font = probe.cget('font')
            probe.destroy()
            super().__init__(master, height=height, width=1, takefocus=False)
            self.pack_propagate(False)
            self.variable = textvariable
            style = ttk.Style(self)
            line_height = tkfont.Font(font=entry_font).metrics('linespace')
            self.text = tk.Text(self, height=1, width=1, wrap='none',
                font=entry_font, undo=True, borderwidth=0, highlightthickness=1,
                background=style.lookup('TEntry', 'fieldbackground') or 'SystemWindow',
                foreground=style.lookup('TEntry', 'foreground') or 'SystemWindowText',
                highlightbackground='#B8B8B8', highlightcolor='#0078D7',
                padx=4, pady=max(0, (height - line_height - 2) // 2))
            self.text.pack(fill='both', expand=True)
            self.text.bind('<<Modified>>', self._on_modified)
            self.text.bind('<Control-a>', self._select_all)
            self.text.bind('<Tab>', lambda event: self._move_focus(False))
            self.text.bind('<Shift-Tab>', lambda event: self._move_focus(True))
            self._trace = self.variable.trace_add('write', self._from_variable)
            self._from_variable()
            self.bind('<Destroy>', self._destroy_trace, add='+')

        def _on_modified(self, event=None):
            if not self.text.edit_modified():
                return
            self.text.edit_modified(False)
            value = self.text.get('1.0', 'end-1c')
            if value != self.variable.get():
                self.variable.set(value)

        def _from_variable(self, *_):
            value = self.variable.get()
            if self.text.get('1.0', 'end-1c') != value:
                self.text.delete('1.0', 'end')
                self.text.insert('1.0', value)
                self.text.edit_modified(False)

        def _select_all(self, event=None):
            self.text.tag_add('sel', '1.0', 'end-1c')
            return 'break'

        def _move_focus(self, backwards):
            widget = self.text.tk_focusPrev() if backwards else self.text.tk_focusNext()
            widget.focus_set()
            return 'break'

        def focus_set(self):
            self.text.focus_set()

        def _destroy_trace(self, event):
            if event.widget is self:
                self.variable.trace_remove('write', self._trace)

    class App(ttk.Frame):
        def __init__(self):
            super().__init__(parent)
            self.ui_scale = self.winfo_fpixels('1i') / 96.0
            self.panel_background = '#FFFFFF'
            self.option_add('*Font', 'TkDefaultFont')
            self.option_add('*Text.background', 'SystemWindow')
            self.option_add('*Text.foreground', 'SystemWindowText')
            self.option_add('*Text.highlightThickness', 0)
            self.library = Library(base / 'metadata_library.sqlite3')
            self.config_data = load_settings(base)
            self.sort_column = self.config_data['sort_column']
            self.sort_descending = self.config_data['sort_descending']
            self.events = queue.Queue()
            self.stop = threading.Event()
            self.busy = self.closing = False
            self.context_item = ''
            self.build_ui()
            self.refresh()
            self.after(100, self.poll)

        def size_window(self, window, width, height):
            width = min(round(width * self.ui_scale), self.winfo_screenwidth() - 80)
            height = min(round(height * self.ui_scale), self.winfo_screenheight() - 100)
            window.geometry(f'{width}x{height}+{max(0, (self.winfo_screenwidth()-width)//2)}'
                            f'+{max(0, (self.winfo_screenheight()-height)//2-20)}')

        def dialog(self, title, width=760, height=580):
            window = tk.Toplevel(self)
            window.title(title)
            window.configure(background=self.panel_background)
            self.size_window(window, width, height)
            window.transient(self.winfo_toplevel())
            window.grab_set()
            return window

        def build_ui(self):
            gap = max(8, round(8 * self.ui_scale))
            main = ttk.Frame(self, padding=gap * 2)
            main.pack(fill='both', expand=True)
            main.columnconfigure(0, weight=1)
            main.rowconfigure(3, weight=1)
            self.folder = tk.StringVar(value=self.config_data['pdf_dir'])
            api_row = ttk.Frame(main)
            api_row.grid(row=1, column=0, sticky='ew', pady=(gap, 0))
            ttk.Label(api_row, text='数据源').pack(side='left', padx=(0, gap * 2))
            self.api_vars, self.api_checks = {}, {}
            for provider in ORDER:
                variable = tk.BooleanVar()
                check = ttk.Checkbutton(api_row, variable=variable, command=self.save_api_selection)
                check.pack(side='left', padx=(0, gap * 2))
                self.api_vars[provider], self.api_checks[provider] = variable, check
            self.sync_api_controls()
            self.settings_button = ttk.Button(api_row, text='配置', width=10, command=self.settings_dialog)
            self.settings_button.pack(side='right')
            actions = ttk.Frame(main)
            actions.grid(row=2, column=0, sticky='ew', pady=gap * 2)
            actions.columnconfigure(6, weight=1)
            self.enrich_button = ttk.Button(actions, text='补全字段', width=10, command=self.enrich)
            self.enrich_button.grid(row=0, column=1)
            self.stop_button = ttk.Button(actions, text='停止', width=8, command=self.stop_task, state='disabled')
            self.stop_button.grid(row=0, column=2, padx=gap)
            self.export_button = ttk.Button(actions, text='导出 RIS', width=10, command=self.export)
            self.export_button.grid(row=0, column=3)
            ttk.Separator(actions, orient='vertical').grid(row=0, column=4, sticky='ns', padx=gap * 2, pady=3)
            ttk.Label(actions, text='搜索').grid(row=0, column=5, padx=(0, gap))
            self.query = tk.StringVar()
            self.search_entry = MultilineSearchEntry(actions, textvariable=self.query)
            self.search_entry.grid(row=0, column=6, sticky='ew', padx=(0, gap))
            self.filter = tk.StringVar(value='全部状态')
            ttk.Combobox(actions, state='readonly', textvariable=self.filter, width=13,
                         values=['全部状态', '待补全', '缺失部分', '已补全', '需填写 DOI']).grid(row=0, column=7)
            self.query.trace_add('write', lambda *_: self.refresh())
            self.filter.trace_add('write', lambda *_: self.refresh())
            self.scan_button = ttk.Button(actions, text='扫描 PDF', width=10, command=self.scan)
            self.scan_button.grid(row=0, column=0, padx=(0, gap))
            table = ttk.Frame(main)
            table.grid(row=3, column=0, sticky='nsew')
            table.columnconfigure(0, weight=1)
            table.rowconfigure(0, weight=1)
            self.tree = ttk.Treeview(table, columns=COLUMNS, show='headings', selectmode='extended')
            widths = {'status': 90, 'title': 300, 'doi': 200, 'authors': 150, 'year': 55,
                      'journal': 165, 'volume': 45, 'issue': 45, 'pages': 100, 'article_number': 95,
                      'date': 100, 'journal_abbreviation': 150, 'issn': 110, 'language': 65,
                      'publisher': 140, 'abstract': 350, 'updated_at': 175}
            self.column_vars = {}
            displayed = visible_columns(self.config_data['visible_columns'])
            self.column_menu = tk.Menu(self, tearoff=False)
            for key in COLUMNS:
                width = widths[key]
                label = COLUMN_LABELS[key].split('（')[0]
                self.tree.heading(key, text=label, command=lambda column=key: self.sort_by(column))
                alignment = 'center' if key in ('status', 'year', 'volume', 'issue', 'pages', 'article_number', 'date', 'language', 'updated_at') else 'w'
                self.tree.column(key, width=round(width * self.ui_scale), minwidth=45,
                                 stretch=key == 'title', anchor=alignment)
                self.column_vars[key] = tk.BooleanVar(value=key in displayed)
                self.column_menu.add_checkbutton(label=label + ('（固定）' if key in FIXED_COLUMNS else ''),
                    variable=self.column_vars[key], state='disabled' if key in FIXED_COLUMNS else 'normal',
                    command=self.apply_columns)
            self.tree.configure(displaycolumns=displayed)
            self.context_menu = tk.Menu(self, tearoff=False)
            self.context_menu.add_command(label='复制标题', command=lambda: self.copy_field('title'), state='disabled')
            self.context_menu.add_command(label='复制 DOI', command=lambda: self.copy_field('doi'), state='disabled')
            self.context_menu.add_command(label='详情 / 编辑', command=self.edit, state='disabled')
            self.tree.grid(row=0, column=0, sticky='nsew')
            ybar = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
            ybar.grid(row=0, column=1, sticky='ns')
            xbar = ttk.Scrollbar(table, orient='horizontal', command=self.tree.xview)
            xbar.grid(row=1, column=0, sticky='ew')
            self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
            self.tree.bind('<Control-a>', self.select_all)
            self.tree.bind('<Double-1>', self.open_pdf)
            self.tree.bind('<Button-3>', self.show_context_menu)
            self.tree.bind('<<TreeviewSelect>>', lambda _: self.update_selection())
            info = ttk.Frame(main)
            info.grid(row=4, column=0, sticky='ew', pady=(gap, gap))
            self.count_text = tk.StringVar()
            ttk.Label(info, textvariable=self.count_text).pack(side='left')
            ttk.Label(info, text='双击打开 PDF · 右键条目查看详情 · 右键表头设置列').pack(side='right')
            progress_row = ttk.Frame(main)
            progress_row.grid(row=5, column=0, sticky='ew')
            self.progress = ttk.Progressbar(progress_row, mode='determinate', maximum=1, value=0)
            self.progress.pack(side='left', fill='x', expand=True)
            self.progress_text = tk.StringVar(value='0 / 0')
            ttk.Label(progress_row, textvariable=self.progress_text, width=20, anchor='e').pack(side='right')
            self.status = tk.StringVar(value='就绪 · Ctrl+A 全选当前列表，点击表头切换排序。')
            self.status_label = ttk.Label(main, textvariable=self.status, anchor='w')
            self.status_label.grid(row=6, column=0, sticky='ew', pady=(gap, 0))
            self.mutation_controls = (self.enrich_button, self.scan_button,
                self.settings_button, self.export_button)

        def refresh(self):
            selected = set(self.tree.selection())
            scroll_position = self.tree.yview()[0]
            self.papers = {str(p['id']): trusted_record(p) for p in self.library.all()}
            # Each non-empty line is an alternative query; words within a line
            # retain the previous AND semantics. Blank lines do not match all.
            queries = [line.casefold().split() for line in self.query.get().splitlines() if line.strip()]
            self.tree.delete(*self.tree.get_children())
            self.visible = []
            for paper in sorted_records(self.papers.values(), self.sort_column, self.sort_descending):
                pk = str(paper['id'])
                haystack = ' '.join(str(paper.get(k, '')) for k in ('title', 'authors', 'doi', 'journal')).casefold()
                status = status_of(paper)
                if (queries and not any(all(t in haystack for t in tokens) for tokens in queries)) or self.filter.get() not in ('全部状态', status):
                    continue
                self.visible.append(paper)
                values = [column_value(paper, key) for key in COLUMNS]
                self.tree.insert('', 'end', iid=pk, values=values)
                if pk in selected:
                    self.tree.selection_add(pk)
            for column in COLUMNS:
                label = COLUMN_LABELS[column].split('（')[0]
                if column == self.sort_column:
                    label += ' ▼' if self.sort_descending else ' ▲'
                self.tree.heading(column, text=label)
            self.tree.yview_moveto(scroll_position)
            self.update_selection()

        def save_preferences(self, changes):
            try:
                self.config_data = save_settings(base, changes)
                return True
            except Exception as error:
                self.status.set('设置暂未保存：' + str(error))
                return False

        def sort_by(self, column):
            self.sort_descending = not self.sort_descending if self.sort_column == column else False
            self.sort_column = column
            self.refresh()
            self.tree.yview_moveto(0)
            self.save_preferences({'sort_column': column, 'sort_descending': self.sort_descending})

        def show_context_menu(self, event):
            region = self.tree.identify_region(event.x, event.y)
            if region == 'heading':
                self.context_item = ''
                try:
                    self.column_menu.tk_popup(event.x_root, event.y_root)
                finally:
                    self.column_menu.grab_release()
                return
            if region not in ('cell', 'tree'):
                return
            row = self.tree.identify_row(event.y)
            if not row:
                return
            self.context_item = row
            if row:
                if row not in self.tree.selection():
                    self.tree.selection_set(row)
                self.tree.focus(row)
                self.update_selection()
            self.context_menu.entryconfigure(2, state='normal' if row and not self.busy else 'disabled')
            record = self.papers.get(row, {})
            self.context_menu.entryconfigure(0, state='normal' if record.get('title') else 'disabled')
            self.context_menu.entryconfigure(1, state='normal' if record.get('doi') else 'disabled')
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

        def copy_field(self, field):
            record = self.papers.get(self.context_item, {})
            value = record.get(field)
            if value:
                self.clipboard_clear()
                self.clipboard_append(str(value))
                self.status.set('已复制' + LABELS[field])

        def apply_columns(self):
            for key in FIXED_COLUMNS:
                self.column_vars[key].set(True)
            displayed = visible_columns([key for key, value in self.column_vars.items() if value.get()])
            self.tree.configure(displaycolumns=displayed)
            self.save_preferences({'visible_columns': displayed})

        def sync_api_controls(self):
            active = enabled_providers(self.config_data)
            for provider in ORDER:
                available = provider_available(self.config_data, provider)
                label = PROVIDER_LABELS[provider]
                if provider == 'crossref':
                    label += '（免 Key）'
                elif not available:
                    label += '（未配置 Key）'
                self.api_vars[provider].set(provider in active)
                self.api_checks[provider].configure(text=label,
                    state='normal' if available and not self.busy else 'disabled')

        def save_api_selection(self):
            active = [p for p in ORDER if self.api_vars[p].get() and provider_available(self.config_data, p)]
            self.config_data['enabled_apis'] = active
            self.save_preferences({'enabled_apis': active})
            self.sync_api_controls()

        def selection(self):
            return [self.papers[pk] for pk in self.tree.selection() if pk in self.papers]

        def select_all(self, _event=None):
            self.tree.selection_set(self.tree.get_children())
            self.update_selection()
            return 'break'

        def update_selection(self):
            self.count_text.set(f'{len(self.visible)} 条 · 已选 {len(self.tree.selection())} 条')

        def choose_folder(self):
            folder = filedialog.askdirectory(parent=self, initialdir=self.folder.get())
            if folder:
                self.folder.set(folder)

        def set_busy(self, busy):
            self.busy = busy
            for widget in self.mutation_controls:
                widget.configure(state='disabled' if busy else 'normal')
            self.stop_button.configure(state='normal' if busy else 'disabled')
            self.sync_api_controls()

        def start(self, function):
            if self.busy:
                return
            self.stop.clear()
            self.set_busy(True)
            self.progress.configure(value=0, maximum=1)
            self.progress_text.set('0 / 0')
            self.status.set('正在准备……')
            def worker():
                try:
                    self.events.put(('done', function()))
                except Exception as error:
                    self.events.put(('error', str(error)))
            threading.Thread(target=worker, daemon=True).start()

        def emit(self, kind, data):
            self.events.put((kind, data))

        def poll(self):
            refresh = False
            try:
                while True:
                    kind, data = self.events.get_nowait()
                    if kind == 'progress':
                        total, count = data['total'], data['completed']
                        self.progress.configure(maximum=max(1, total), value=count)
                        self.progress_text.set(f'{count} / {total}  ({count / total:.0%})' if total else '0 / 0')
                        if not self.stop.is_set():
                            self.status.set(data['stage'] + (' · ' + data['current'] if data.get('current') else ''))
                    elif kind == 'record':
                        refresh = True
                    elif kind in ('done', 'error'):
                        self.set_busy(False)
                        self.status.set(data)
                        refresh = True
                        if kind == 'error':
                            messagebox.showerror('任务失败', data, parent=self)
            except queue.Empty:
                pass
            if refresh:
                self.refresh()
            if self.closing and not self.busy:
                self.destroy()
                return
            self.after(100, self.poll)

        def stop_task(self):
            if self.busy:
                self.stop.set()
                self.stop_button.configure(state='disabled')
                self.status.set('正在停止，已补全内容会保留……')

        def scan(self):
            if self.busy:
                return
            self.config_data = load_settings(base)
            self.folder.set(self.config_data['pdf_dir'])
            folder = Path(self.folder.get()).expanduser()
            if not folder.is_absolute():
                folder = base / folder
            if not folder.is_dir():
                messagebox.showerror('目录不存在', '请选择有效的 PDF 目录。', parent=self)
                return
            try:
                self.config_data = save_settings(base, {'pdf_dir': str(folder)})
            except Exception as error:
                messagebox.showerror('配置保存失败', str(error), parent=self)
                return
            self.start(lambda: self.library.scan(folder, self.stop, lambda data: self.emit('progress', data)))

        def enrich(self):
            if self.busy:
                return
            records = [p for p in self.selection() if p.get('doi') and missing(p)]
            if not records:
                messagebox.showinfo('没有待补全条目', '请选中有 DOI 且仍有缺失字段的记录。筛选后可用 Ctrl+A 全选。', parent=self)
                return
            try:
                self.config_data = load_settings(base)  # See downloader key changes immediately.
                self.sync_api_controls()
            except Exception as error:
                messagebox.showerror('配置读取失败', str(error), parent=self)
                return
            if not enabled_providers(self.config_data):
                messagebox.showinfo('未启用 API', '请先在上方勾选可用的 API。', parent=self)
                return
            config = copy.deepcopy(self.config_data)
            self.start(lambda: run_batch(records, self.library, config, self.stop, self.emit))

        def settings_dialog(self):
            if self.busy:
                return
            try:
                current = load_settings(base)
            except Exception as error:
                messagebox.showerror('读取配置失败', str(error), parent=self)
                return
            window = self.dialog('共用下载器配置', 720, 440)
            form = ttk.Frame(window, padding=16)
            form.pack(fill='both', expand=True)
            form.columnconfigure(1, weight=1)
            variables = {}
            specs = [('contact_email', '联系邮箱'), ('openalex', 'OpenAlex Key'),
                     ('elsevier', 'Elsevier Key'), ('wos', 'WOS Starter Key'),
                     ('crossref', 'Crossref Plus Key（可空）'), ('elsevier_inst_token', 'Elsevier InstToken（可空）')]
            for row, (key, label) in enumerate(specs):
                ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', pady=8, padx=(0, 12))
                value = current.get(key, '') if key == 'contact_email' else current['api_keys'].get(key, '')
                variables[key] = tk.StringVar(value=value)
                ttk.Entry(form, textvariable=variables[key], show='' if key == 'contact_email' else '•').grid(row=row, column=1, sticky='ew', pady=8)
            ttk.Label(form, text='Elsevier 论文优先官方 API；其他论文按勾选来源补全。', wraplength=650).grid(row=6, column=0, columnspan=2, sticky='w', pady=(12, 4))
            ttk.Label(form, text='共用 config.local.json；下载器中的密钥修改会在下次补全时自动读取。',
                      wraplength=round(650*self.ui_scale)).grid(row=7, column=0, columnspan=2, sticky='w')
            def save():
                changes = {'api_keys': {}}
                for key, var in variables.items():
                    old = current.get(key, '') if key == 'contact_email' else current['api_keys'].get(key, '')
                    if var.get().strip() != old:
                        if key == 'contact_email':
                            changes[key] = var.get().strip()
                        else:
                            changes['api_keys'][key] = var.get().strip()
                try:
                    self.config_data = save_settings(base, changes)
                    self.sync_api_controls()
                    self.status.set('已保存到下载器的 config.local.json')
                    window.destroy()
                except Exception as error:
                    messagebox.showerror('保存失败', str(error), parent=window)
            ttk.Button(form, text='保存', command=save).grid(row=8, column=1, sticky='e', pady=16)

        def edit(self):
            if self.busy or not self.selection():
                return
            original = self.papers.get(self.context_item) if self.context_item in self.tree.selection() else self.selection()[0]
            window = self.dialog('编辑与详情', 880, 730)
            area = ttk.Frame(window)
            area.pack(fill='both', expand=True)
            canvas = tk.Canvas(area, background=self.panel_background, highlightthickness=0)
            canvas.pack(side='left', fill='both', expand=True)
            scrollbar = ttk.Scrollbar(area, orient='vertical', command=canvas.yview)
            scrollbar.pack(side='right', fill='y')
            canvas.configure(yscrollcommand=scrollbar.set)
            form = ttk.Frame(canvas, padding=12)
            form_id = canvas.create_window((0, 0), window=form, anchor='nw')
            form.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda event: canvas.itemconfigure(form_id, width=event.width))
            form.columnconfigure(1, weight=1)
            ttk.Label(form, text='状态：' + status_of(original) + '\n缺失字段：' + missing_text(original),
                      wraplength=round(760 * self.ui_scale), justify='left').grid(
                          row=0, column=0, columnspan=3, sticky='w', pady=(0, 12))
            inputs = {}
            for row, field in enumerate(('doi',) + FIELDS, 1):
                ttk.Label(form, text=LABELS[field]).grid(row=row, column=0, sticky='nw', padx=(0, 12), pady=5)
                entry = tk.Text(form, height=3 if field in ('authors', 'abstract') else 1, wrap='word')
                entry.grid(row=row, column=1, sticky='ew', pady=5)
                entry.insert('1.0', '\n'.join(original.get(field, [])) if field == 'authors' else original.get(field, ''))
                inputs[field] = entry
                source = original.get('field_sources', {}).get(field, '')
                ttk.Label(form, text='人工' if source == 'manual' else PROVIDER_LABELS.get(source, source)).grid(
                    row=row, column=2, sticky='nw', padx=(8, 0), pady=5)
            info_row = len(FIELDS) + 2
            ttk.Label(form, text='PDF 与查询记录').grid(row=info_row, column=0, columnspan=3, sticky='w', pady=(12, 5))
            detail_frame = ttk.Frame(form)
            detail_frame.grid(row=info_row + 1, column=0, columnspan=3, sticky='ew')
            detail = tk.Text(detail_frame, height=9, wrap='word')
            detail.pack(side='left', fill='both', expand=True)
            detail_scroll = ttk.Scrollbar(detail_frame, orient='vertical', command=detail.yview)
            detail_scroll.pack(side='right', fill='y')
            detail.configure(yscrollcommand=detail_scroll.set)
            detail.insert('1.0', 'DOI 识别：' + original.get('doi_source', '') +
                '\n最后补全：' + original.get('updated_at', '尚未查询') + '\n\nPDF：\n' +
                ('\n'.join(original.get('files', [])) or '未关联 PDF') + '\n\n查询记录：\n' +
                request_log(original))
            detail.configure(state='disabled')
            def save():
                paper = copy.deepcopy(original)
                doi = doi_normalize(inputs['doi'].get('1.0', 'end').strip())
                if not doi:
                    messagebox.showerror('DOI 无效', '请填写完整 DOI。', parent=window)
                    return
                if any(p['id'] != paper['id'] and p.get('doi') == doi for p in self.library.all()):
                    messagebox.showerror('DOI 已存在', '此 DOI 已有记录，请编辑已有记录。', parent=window)
                    return
                if doi != paper.get('doi') and any(paper.get(f) for f in FIELDS):
                    messagebox.showerror('已有元数据', '已有元数据的记录不能更换 DOI，请另行添加。', parent=window)
                    return
                paper.update(doi=doi, doi_source='manual')
                for field in FIELDS:
                    value = inputs[field].get('1.0', 'end').strip()
                    value = [clean(a) for a in value.splitlines() if clean(a)] if field == 'authors' else clean(value)
                    if field == 'year' and value and not re.fullmatch(r'\d{4}', value):
                        messagebox.showerror('年份无效', '请填写四位年份或留空。', parent=window)
                        return
                    if value != paper.get(field, [] if field == 'authors' else ''):
                        paper[field] = value
                        paper.setdefault('field_sources', {})[field] = 'manual'
                self.library.save(paper)
                self.refresh()
                window.destroy()
            ttk.Button(window, text='保存', command=save).pack(anchor='e', padx=12, pady=10)

        def open_pdf(self, event=None):
            if event is not None:
                row = self.tree.identify_row(event.y)
                if not row:
                    return
                self.tree.selection_set(row)
            if not self.selection():
                return
            path = next((Path(p) for p in self.selection()[0].get('files', []) if Path(p).is_file()), None)
            if not path:
                messagebox.showinfo('PDF 不存在', '没有关联的本地 PDF，或文件已被移动，请重新扫描目录。', parent=self)
                return
            try:
                os.startfile(str(path)) if os.name == 'nt' else webbrowser.open(path.as_uri())
            except OSError as error:
                messagebox.showerror('无法打开 PDF', str(error), parent=self)

        def export(self):
            if self.busy:
                return
            records = self.selection()
            if not records:
                messagebox.showinfo('请选择文献', '选中需要导出的条目，Ctrl+A 可全选当前列表。', parent=self)
                return
            try:
                records, linked = self.library.link_local_pdfs(records, self.folder.get())
            except (OSError, sqlite3.Error) as error:
                messagebox.showerror('PDF 关联检查失败', str(error), parent=self)
                return
            text, count, skipped = ris_text(records)
            if not count:
                messagebox.showinfo('没有可导出条目', '先补全标题和 DOI。', parent=self)
                return
            target = filedialog.asksaveasfilename(parent=self, defaultextension='.ris',
                initialfile='论文文献库.ris', filetypes=[('Zotero / RIS', '*.ris')])
            if target:
                try:
                    atomic_text(target, text)
                    self.status.set(f'已导出 {count} 条，跳过 {skipped} 条，新关联 {linked} 个 PDF。可在 Zotero 中导入 RIS。')
                except OSError as error:
                    messagebox.showerror('导出失败', str(error), parent=self)

        def close_app(self):
            if self.busy:
                self.closing = True
                self.stop_task()
            else:
                self.destroy()

    return App()


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=APP_DIR)
    args = parser.parse_args()
    base = args.data_dir.resolve()
    run_gui(base).mainloop()
    return 0


if __name__ == '__main__':
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except Exception as error:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('文献库无法启动', f'{type(error).__name__}: {error}')
        root.destroy()
        raise SystemExit(1)
