from suite_search import *
# Independent online-only UI. No SQLite, PDF scanning or local library imports.
import os
import tempfile
import queue
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from tkinter import font as tkfont

COLUMNS = ('status', 'title', 'doi', 'authors', 'year', 'journal', 'volume', 'issue',
           'pages', 'article_number', 'date', 'journal_abbreviation', 'issn', 'language',
           'publisher', 'abstract', 'sources')
DEFAULT_COLUMNS = COLUMNS[:10]
COLUMN_LABELS = {**LABELS, 'status': '状态', 'sources': '来源'}


def save_config_changes(path, *, keys=None, email=None, preferences=None, pdf_dir=None):
    """Merge changed settings with the latest file; preserve downloader options."""
    target = resolve_config_path(path)
    data = json.loads(target.read_text(encoding='utf-8-sig')) if target.exists() else {}
    if not isinstance(data, dict):
        raise ValueError('配置应为 JSON 对象')
    if keys:
        data.setdefault('api_keys', {}).update(keys)
    if email is not None:
        data['contact_email'] = email
    if pdf_dir is not None:
        data.setdefault('paths', {})['pdf_dir'] = str(pdf_dir)
    if preferences:
        data.setdefault('literature_search', {}).update(preferences)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix=target.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return target


def enable_dpi_awareness():
    if sys.platform == 'win32':
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



class SearchPanel(ttk.Frame):
    def __init__(self, master, controller, config_path):
        super().__init__(master)
        self.controller = controller
        self.ui_scale = self.winfo_fpixels('1i') / 96
        self.config_path = resolve_config_path(config_path)
        self.style = ttk.Style(self)
        self.panel_background = '#FFFFFF'
        self.records, self.result = [], {}
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = self.closed = False
        self.query = tk.StringVar()
        self.mode = tk.StringVar(value='自动识别')
        self.limit = tk.StringVar(value='20')
        self.status = tk.StringVar(value='输入标题、关键词或 DOI；多选条目右键可拓展种子文献。')
        self.count_text = tk.StringVar(value='本次在线结果 0 条')
        self.sort_column, self.descending = '', False
        self.config_data = self.load_config()
        self.build_ui()
        self.sync_sources()
        self.row_menu.add_separator()
        self.row_menu.add_command(label='种子文献拓展', command=self.start_expand)
        self.expand_menu_index = self.row_menu.index('end')
        self.row_menu.add_command(label='发送到元数据补全', command=lambda:self.controller.queue_metadata(self.selected()))
        self.row_menu.add_command(label='发送到文献下载', command=lambda:self.controller.queue_download(self.selected()))
        self.after_id = self.after(80,self.poll)
        self.after_idle(self.search_entry.focus_set)

    def load_config(self):
        return read_config(self.config_path) if self.config_path.exists() else {}

    def size_window(self, window, width, height):
        width = min(round(width*self.ui_scale), self.winfo_screenwidth()-80)
        height = min(round(height*self.ui_scale), self.winfo_screenheight()-100)
        window.geometry(f'{width}x{height}+{max(0,(self.winfo_screenwidth()-width)//2)}+{max(0,(self.winfo_screenheight()-height)//2-20)}')

    def build_ui(self):
        gap = max(8, round(8*self.ui_scale))
        main = ttk.Frame(self, padding=gap*2)
        main.pack(fill='both', expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)
        row = ttk.Frame(main)
        row.grid(row=0, column=0, sticky='ew')
        row.columnconfigure(1, weight=1)
        ttk.Label(row, text='在线检索').grid(row=0, column=0, padx=(0,gap))
        self.search_entry = ttk.Entry(row, textvariable=self.query)
        self.search_entry.grid(row=0, column=1, sticky='ew', padx=(0,gap))
        self.search_entry.bind('<Return>', self.start_search)
        self.mode_box = ttk.Combobox(row, textvariable=self.mode, values=['自动识别','标题','关键词','DOI'], state='readonly', width=9)
        self.mode_box.grid(row=0, column=2, padx=(0,gap))
        self.search_button = ttk.Button(row, text='检索', width=10, command=self.start_search)
        self.search_button.grid(row=0, column=3)
        api = ttk.Frame(main)
        api.grid(row=1, column=0, sticky='ew', pady=(gap,0))
        ttk.Label(api, text='数据源').pack(side='left', padx=(0,gap*2))
        self.api_vars, self.api_checks = {}, {}
        for source in ORDER:
            self.api_vars[source] = tk.BooleanVar()
            check = ttk.Checkbutton(api, variable=self.api_vars[source])
            check.pack(side='left', padx=(0,gap*2))
            self.api_checks[source] = check
        self.config_button = ttk.Button(api, text='配置', width=10, command=self.settings_dialog)
        self.config_button.pack(side='right')
        bar = ttk.Frame(main)
        bar.grid(row=2, column=0, sticky='ew', pady=gap*2)
        self.enrich_button = ttk.Button(bar, text='补全字段', width=10, command=self.start_enrich)
        self.enrich_button.pack(side='left')
        self.stop_button = ttk.Button(bar, text='停止', width=8, command=self.cancel, state='disabled')
        self.stop_button.pack(side='left', padx=gap)
        ttk.Button(bar, text='导出结果', width=10, command=self.export).pack(side='left')
        ttk.Button(bar, text='复制 DOI', width=10, command=self.copy_dois).pack(side='left', padx=gap)
        ttk.Button(bar, text='查看详情', width=10, command=self.detail).pack(side='left')
        self.export_scope = tk.StringVar(value='选中记录')
        ttk.Combobox(bar, textvariable=self.export_scope, values=['选中记录','全部结果'], state='readonly', width=10).pack(side='right')
        ttk.Label(bar, text='导出范围').pack(side='right', padx=gap)
        frame = ttk.Frame(main)
        frame.grid(row=3, column=0, sticky='nsew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=COLUMNS, show='headings', selectmode='extended')
        widths = {'status':90,'title':300,'doi':200,'authors':150,'year':55,'journal':165,
                  'volume':45,'issue':45,'pages':100,'article_number':95,'date':100,
                  'journal_abbreviation':150,'issn':110,'language':65,'publisher':140,'abstract':350,'sources':160}
        self.column_vars = {}
        self.column_menu = tk.Menu(self, tearoff=False)
        configured = (self.config_data.get('literature_search') or {}).get('visible_columns', DEFAULT_COLUMNS)
        if not isinstance(configured, (list,tuple)):
            configured = DEFAULT_COLUMNS
        for column in COLUMNS:
            label = COLUMN_LABELS[column].split('（')[0]
            self.tree.heading(column, text=label, command=lambda c=column:self.sort(c))
            self.tree.column(column, width=round(widths[column]*self.ui_scale), minwidth=45, stretch=column=='title',
                             anchor='center' if column in ('status','year','volume','issue','pages','article_number','date') else 'w')
            self.column_vars[column] = tk.BooleanVar(value=column in configured or column in ('title','doi'))
            self.column_menu.add_checkbutton(label=label, variable=self.column_vars[column],
                state='disabled' if column in ('title','doi') else 'normal', command=self.apply_columns)
        self.apply_columns(save=False)
        self.tree.grid(row=0,column=0,sticky='nsew')
        vbar = ttk.Scrollbar(frame,orient='vertical',command=self.tree.yview)
        vbar.grid(row=0,column=1,sticky='ns')
        hbar = ttk.Scrollbar(frame,orient='horizontal',command=self.tree.xview)
        hbar.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=vbar.set,xscrollcommand=hbar.set)
        self.tree.bind('<Double-1>',self.detail)
        self.tree.bind('<Button-3>',self.context)
        self.tree.bind('<Control-a>',self.select_all)
        self.tree.bind('<<TreeviewSelect>>',lambda event:self.update_count())
        self.row_menu = tk.Menu(self,tearoff=False)
        self.row_menu.add_command(label='复制标题',command=lambda:self.copy_field('title'))
        self.row_menu.add_command(label='复制 DOI',command=self.copy_dois)
        self.row_menu.add_command(label='元数据详情',command=self.detail)
        self.row_menu.add_command(label='打开 DOI 页面',command=self.open_doi)
        info=ttk.Frame(main)
        info.grid(row=4,column=0,sticky='ew',pady=gap)
        ttk.Label(info,textvariable=self.count_text).pack(side='left')
        ttk.Label(info,text='双击查看详情 · 右键表头设置列').pack(side='right')
        self.progress=ttk.Progressbar(main,mode='indeterminate')
        self.progress.grid(row=5,column=0,sticky='ew')
        self.status_label=ttk.Label(main,textvariable=self.status,anchor='w')
        self.status_label.grid(row=6,column=0,sticky='ew',pady=(gap,0))
        main.bind('<Configure>',lambda event:self.status_label.configure(wraplength=max(200,event.width-gap*4)))

    def sync_sources(self, preserve=False):
        for source in ORDER:
            available=provider_available(self.config_data,source)
            if not preserve:
                self.api_vars[source].set(available)
            elif not available:
                self.api_vars[source].set(False)
            suffix='（免 Key）' if source=='crossref' else ('（未配置 Key）' if not available else '')
            self.api_checks[source].configure(text=PROVIDER_LABELS[source]+suffix,state='normal' if available and not self.busy else 'disabled')

    def selected(self):
        return [self.records[int(i)] for i in self.tree.selection() if int(i)<len(self.records)]

    def value(self,record,column):
        if column=='status': return '缺失部分' if missing(record) else '已获取'
        v=record.get(column,'')
        return '; '.join(v) if isinstance(v,list) else str(v or '')

    def refresh(self):
        selected=set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        rows=list(enumerate(self.records))
        if self.sort_column:
            def key(item):
                return tuple((1,int(t)) if t.isdigit() else (0,t.casefold()) for t in re.split(r'(\d+)',self.value(item[1],self.sort_column)))
            filled=[r for r in rows if self.value(r[1],self.sort_column)]
            rows=sorted(filled,key=key,reverse=self.descending)+[r for r in rows if not self.value(r[1],self.sort_column)]
        for i,record in rows:
            self.tree.insert('','end',iid=str(i),values=[self.value(record,c) for c in COLUMNS])
        selection=[i for i in selected if self.tree.exists(i)]
        if selection:self.tree.selection_set(selection)
        for column in COLUMNS:
            self.tree.heading(column,text=COLUMN_LABELS[column].split('（')[0]+((' ▼' if self.descending else ' ▲') if column==self.sort_column else ''))
        self.update_count()

    def update_count(self):
        self.count_text.set(f"本次在线结果 {len(self.records)} 条 · 已选 {len(self.tree.selection())} 条 · 候选池 {self.result.get('candidate_count',0)} 条")

    def sort(self,column):
        self.descending=not self.descending if self.sort_column==column else False
        self.sort_column=column
        self.refresh()

    def apply_columns(self,save=True):
        columns=[c for c in COLUMNS if self.column_vars[c].get() or c in ('title','doi')]
        self.tree.configure(displaycolumns=columns)
        if save:
            try:save_config_changes(self.config_path,preferences={'visible_columns':columns})
            except Exception as error:self.status.set('列设置未保存：'+type(error).__name__)

    def select_all(self,event=None):
        self.tree.selection_set(self.tree.get_children())
        return 'break'

    def _base_context(self,event):
        if self.tree.identify_region(event.x,event.y)=='heading':menu=self.column_menu
        else:
            row=self.tree.identify_row(event.y)
            if not row:return
            if row not in self.tree.selection():self.tree.selection_set(row)
            menu=self.row_menu
        try:menu.tk_popup(event.x_root,event.y_root)
        finally:menu.grab_release()

    def set_busy(self,value):
        self.busy=value
        for widget in (self.search_entry,self.search_button,self.enrich_button,self.config_button):
            widget.configure(state='disabled' if value else 'normal')
        self.mode_box.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if value else 'disabled')
        self.sync_sources(preserve=True)
        if value:self.progress.start(12)
        else:self.progress.stop()

    def start_worker(self,operation):
        self.stop=threading.Event()
        self.set_busy(True)
        def work():
            try:self.events.put(('done',operation()))
            except Exception as error:self.events.put(('error',type(error).__name__))
        threading.Thread(target=work,daemon=True).start()

    def task_config(self):
        self.config_data=self.load_config()
        self.sync_sources(preserve=True)
        return copy.deepcopy(self.config_data),[p for p in ORDER if self.api_vars[p].get()]

    def start_search(self,event=None):
        if self.busy:return 'break'
        query=self.query.get().strip()
        mode={'自动识别':'auto','标题':'title','关键词':'keyword','DOI':'doi'}[self.mode.get()]
        try:
            limit=self.controller.get_search_limit()
            if not query or not 1<=limit<=100 or (mode=='doi' and not doi_normalize(query)):raise ValueError()
            config,sources=self.task_config()
        except Exception as error:
            messagebox.showerror('无法开始检索','请检查输入、结果上限和同目录配置文件。'+type(error).__name__,parent=self)
            return 'break'
        search_sources=[p for p in sources if p in SEARCH_PROVIDERS]
        if not search_sources:
            messagebox.showinfo('选择检索来源','请启用 Crossref 或 OpenAlex；Elsevier / WOS 仅用于 DOI 补全。',parent=self)
            return 'break'
        self.records=[]
        self.result={}
        self.sort_column=''
        self.refresh()
        self.status.set('正在在线检索…')
        def operation():
            result=search_metadata(query,mode=mode,limit=limit,config=config,sources=search_sources,stop_event=self.stop,
                                   on_progress=lambda msg:self.events.put(('status',msg)))
            self.events.put(('result',result))
            if result['mode']=='doi' and result['records'] and not self.stop.is_set():
                original=result['records'][0]
                enriched=complete_metadata(original['doi'],config=config,sources=sources,existing=original,stop_event=self.stop,
                    on_progress=lambda msg:self.events.put(('status',msg)),on_checkpoint=lambda record:self.events.put(('record',(0,record))))
                self.events.put(('record',(0,enriched)))
            errors=[f"{r['provider']}：{r.get('error') or r['status']}" for r in result['source_reports'] if r['status'] not in ('ok','not_found')]
            message=f"{'已停止' if self.stop.is_set() else '在线检索完成'} · 显示 {result['count']} 篇 · 排除非期刊 {result['excluded_count']} 篇"
            return message+(' · '+'；'.join(errors) if errors else '')
        self.start_worker(operation)
        return 'break'

    def start_enrich(self):
        if self.busy:return
        chosen=[(int(i),copy.deepcopy(self.records[int(i)])) for i in self.tree.selection() if self.records[int(i)].get('doi')]
        if not chosen:
            messagebox.showinfo('选择文献','请先选中本次检索结果中带 DOI 的文献。',parent=self)
            return
        try:config,sources=self.task_config()
        except Exception as error:
            messagebox.showerror('配置错误',type(error).__name__,parent=self);return
        if not sources:return
        def operation():
            disabled=set()
            count=0
            for index,record in chosen:
                if self.stop.is_set():break
                enriched=complete_metadata(record['doi'],config=config,sources=sources,existing=record,stop_event=self.stop,disabled_sources=disabled,
                    on_progress=lambda msg:self.events.put(('status',msg)),
                    on_checkpoint=lambda row,i=index:self.events.put(('record',(i,row))))
                self.events.put(('record',(index,enriched)))
                count+=1
            return f"{'已停止' if self.stop.is_set() else '补全结束'} · 已处理 {count}/{len(chosen)} 篇"
        self.start_worker(operation)

    def poll(self):
        if self.closed:return
        try:
            while True:
                kind,data=self.events.get_nowait()
                if kind=='status' and not self.stop.is_set():self.status.set(data)
                elif kind=='result':
                    self.result=data
                    self.records=copy.deepcopy(data['records'])
                    self.refresh()
                    if self.records:self.tree.selection_set('0')
                elif kind=='record':
                    index,record=data
                    self.records[index]=record
                    self.refresh()
                elif kind in ('done','error'):
                    self.set_busy(False)
                    self.status.set('任务失败：'+data if kind=='error' else data)
        except queue.Empty:pass
        self.after_id=self.after(80,self.poll)

    def cancel(self):
        self.stop.set()
        self.stop_button.configure(state='disabled')
        self.status.set('正在停止：等待当前请求返回或超时。')

    def copy_field(self,field):
        text='\n'.join(str(r.get(field) or '') for r in self.selected() if r.get(field))
        if text:
            self.clipboard_clear();self.clipboard_append(text)
            self.status.set('已复制到剪贴板。')

    def copy_dois(self):self.copy_field('doi')


    def open_doi(self):
        records=self.selected()
        if records and records[0].get('doi'):webbrowser.open('https://doi.org/'+urllib.parse.quote(records[0]['doi'],safe='/'))

    def export(self):
        records=self.records if self.export_scope.get()=='全部结果' else self.selected()
        if not records:
            messagebox.showinfo('没有结果','请先在线检索并选择需要导出的记录。',parent=self);return
        path=filedialog.asksaveasfilename(parent=self,initialfile='在线检索结果.ris',defaultextension='.ris',
            filetypes=[('RIS 引文','*.ris'),('JSON 元数据','*.json'),('CSV 表格','*.csv')])
        if path:
            try:export_records(path,records);self.status.set(f'已导出 {len(records)} 篇：{path}')
            except Exception as error:messagebox.showerror('导出失败',type(error).__name__,parent=self)

    def detail(self,event=None):
        if event is not None:
            row=self.tree.identify_row(event.y)
            if not row:return
            self.tree.selection_set(row)
        records=self.selected()
        if not records:return
        record=copy.deepcopy(records[0])
        window=tk.Toplevel(self)
        window.title('元数据详情')
        window.configure(background=self.panel_background)
        self.size_window(window,880,730)
        window.transient(self)
        area=ttk.Frame(window)
        area.pack(fill='both',expand=True)
        canvas=tk.Canvas(area,background=self.panel_background,highlightthickness=0)
        canvas.pack(side='left',fill='both',expand=True)
        scrollbar=ttk.Scrollbar(area,orient='vertical',command=canvas.yview)
        scrollbar.pack(side='right',fill='y')
        canvas.configure(yscrollcommand=scrollbar.set)
        form=ttk.Frame(canvas,padding=12)
        form_id=canvas.create_window((0,0),window=form,anchor='nw')
        form.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda e:canvas.itemconfigure(form_id,width=e.width))
        form.columnconfigure(1,weight=1)
        for row,field in enumerate(EXPORT_FIELDS):
            ttk.Label(form,text=LABELS[field].split('（')[0]).grid(row=row,column=0,sticky='nw',padx=(0,12),pady=5)
            box=tk.Text(form,height=5 if field=='abstract' else (2 if field in ('authors','title') else 1),wrap='word')
            box.grid(row=row,column=1,sticky='ew',pady=5)
            value=record.get(field)
            box.insert('1.0','; '.join(value) if isinstance(value,list) else (value or '（来源未提供）'))
            box.configure(state='disabled')
            ttk.Label(form,text=record.get('field_sources',{}).get(field,'')).grid(row=row,column=2,sticky='nw',padx=(8,0),pady=5)
        log='\n'.join(f"{a['provider']}：{a['result']} ({a.get('elapsed_seconds',0)}s)" for a in record.get('attempts',[]))
        ttk.Label(form,text='查询记录').grid(row=len(EXPORT_FIELDS),column=0,sticky='nw',pady=12)
        box=tk.Text(form,height=6,wrap='word')
        box.grid(row=len(EXPORT_FIELDS),column=1,columnspan=2,sticky='ew',pady=12)
        box.insert('1.0',(log or '检索来源：'+' / '.join(record.get('sources',[])))+'\n拓展关系：'+' / '.join(record.get('relation',[]))+'\n种子：'+'; '.join(record.get('seed_dois',[])))
        box.configure(state='disabled')
        ttk.Button(window,text='关闭',command=window.destroy).pack(anchor='e',padx=12,pady=10)

    def close(self):
        self.closed=True
        self.stop.set()
        self.after_cancel(self.after_id)
        self.destroy()

    def settings_dialog(self):
        self.controller.notebook.select(self.controller.settings_tab)

    def start_expand(self):
        from suite_expansion import expand_seeds
        if self.busy:return
        seeds=copy.deepcopy(self.selected())
        if not seeds:
            messagebox.showinfo('选择种子文献','请先多选需要拓展的种子文献。',parent=self);return
        try:
            limit=self.controller.get_search_limit()
            if not 1<=limit<=100:raise ValueError()
            config,sources=self.task_config()
            config['enabled_apis']=sources
        except Exception:
            messagebox.showerror('参数无效','请检查 1–100 的结果上限及全局配置。',parent=self);return
        self.status.set(f'正在拓展 {len(seeds)} 篇种子，结果总上限 {limit} 篇…')
        def operation():
            result=expand_seeds(seeds,limit=limit,config=config,stop_event=self.stop,
                                on_progress=lambda message:self.events.put(('status',message)))
            if not result['seeds']:
                reasons='；'.join(r.get('error','') for r in result['source_reports'] if r.get('error'))
                return '未能解析种子文献，保留当前列表。'+reasons
            self.events.put(('result',result))
            failures=[r for r in result['source_reports'] if r['status'] not in ('ok','not_found')]
            return (f"{'已停止' if result['cancelled'] else '种子拓展完成'} · {len(result['seeds'])} 篇种子"
                    f" · 本次结果 {result['count']}/{limit} 篇 · 排除非期刊 {result['excluded_count']} 篇"
                    +(f" · {len(failures)} 项来源异常或未启用" if failures else ''))
        self.start_worker(operation)

    def context(self,event):
        self.row_menu.entryconfigure(self.expand_menu_index,state='disabled' if self.busy else 'normal')
        return self._base_context(event)
