import copy
import json
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import literature_download_gui as download_gui
import paper_metadata_gui as metadata_gui
import suite_search as search
from suite_search_ui import SearchPanel
from suite_paths import APP_DIR, RESOURCE_DIR, ENTRY_PATH, entry_command
from suite_settings import search_limit,pdf_directory


class SuiteApp(download_gui.DownloadApp):
    def __init__(self):
        super().__init__()
        self.title('文献工作台')
        self._expired_prompted=True  # Login remains an explicit global-config action.
        self.status_text.set('就绪 · 可下载开放全文或通过已授权的来源获取全文')
        self.notebook.bind('<<NotebookTabChanged>>',self._tab_changed,add='+')

    def _command(self,*arguments):
        return entry_command()+['_download-kernel',*arguments]

    def _configure_style(self):
        super()._configure_style()
        style=ttk.Style(self)
        page='#FFFFFF'
        self.configure(background=page)
        # Azure supplies the shapes; every non-input surface uses one palette.
        style.configure('.',background=page,foreground='#334155')
        for name in ('TFrame','Surface.TFrame','TLabel','Card.TLabel','TLabelframe','TLabelframe.Label','TNotebook',
                     'TCheckbutton','TRadiobutton','Switch.TCheckbutton','TSeparator','TPanedwindow'):
            style.configure(name,background=page)
        for name in ('TCheckbutton','TRadiobutton','Switch.TCheckbutton'):
            style.map(name,background=[('disabled',page),('active',page),('selected',page),('!disabled',page)])
        style.configure('TNotebook.Tab',background=page)
        style.map('TNotebook.Tab',background=[('selected',page),('active','#E8EFF8'),('!selected','#E8EFF8')])
        self.option_add('*Canvas.background',page)
        self.option_add('*Text.background','#FFFFFF')
        self.option_add('*Text.foreground','#334155')

    def get_search_limit(self):
        config=search.read_config(self._config_file_path()) if self._config_file_path().exists() else {}
        return search_limit(config)

    def _build_ui(self):
        super()._build_ui()
        self.notebook.tab(self.download_tab,text='  文献下载  ')
        self.notebook.tab(self.settings_tab,text='  全局配置  ')
        self.search_tab=ttk.Frame(self.notebook)
        self.metadata_tab=ttk.Frame(self.notebook)
        self.notebook.insert(0,self.search_tab,text='  文献检索  ')
        self.notebook.insert(1,self.metadata_tab,text='  元数据补全  ')
        self.search_panel=SearchPanel(self.search_tab,self,APP_DIR/'config.local.json')
        self.search_panel.pack(fill='both',expand=True)
        self.metadata_panel=metadata_gui.run_gui(APP_DIR,parent=self.metadata_tab)
        self.metadata_panel.pack(fill='both',expand=True)
        self.metadata_panel.settings_dialog=lambda:self.notebook.select(self.settings_tab)
        self.metadata_panel.settings_button.configure(command=self.metadata_panel.settings_dialog)
        self.progress.configure(background='#FFFFFF')
        # Link the separate results pages by explicit user actions only.
        self.metadata_panel.context_menu.add_separator()
        self.metadata_panel.context_menu.add_command(label='发送到文献下载',command=lambda:self.queue_download(self.metadata_panel.selection()))
        self.notebook.select(self.search_tab)

    def _build_settings_tab(self):
        super()._build_settings_tab()
        container=self.elsevier_key_entry.master
        config=search.read_config(APP_DIR/'config.local.json') if (APP_DIR/'config.local.json').exists() else {}
        keys=config.get('api_keys') or {}
        self.extra_keys={}
        self.extra_key_entries=[]
        settings_page=container.master
        for child in settings_page.grid_slaves():
            child.grid_configure(row=int(child.grid_info()['row'])+1)
        shared=ttk.LabelFrame(settings_page,text='通用设置',padding=14)
        shared.grid(row=0,column=0,sticky='ew',pady=(0,12))
        shared.columnconfigure(1,weight=1)
        ttk.Label(shared,text='PDF 目录',style='Card.TLabel').grid(row=0,column=0,sticky='w',padx=(0,10))
        self.global_pdf_entry=ttk.Entry(shared,textvariable=self.output_dir)
        self.global_pdf_entry.grid(row=0,column=1,sticky='ew')
        ttk.Button(shared,text='选择目录',command=self.choose_output_dir).grid(row=0,column=2,padx=(8,0))
        ttk.Button(shared,text='打开目录',command=self.open_output_dir).grid(row=0,column=3,padx=(8,0))
        self.search_limit_var=tk.StringVar(value=str(search_limit(config)))
        ttk.Label(shared,text='文献检索上限',style='Card.TLabel').grid(row=1,column=0,sticky='w',pady=(12,0),padx=(0,10))
        self.global_limit_box=ttk.Spinbox(shared,from_=1,to=100,width=8,textvariable=self.search_limit_var)
        self.global_limit_box.grid(row=1,column=1,sticky='w',pady=(12,0))
        ttk.Label(shared,text='1–100，检索与多种子拓展共用',style='Card.TLabel').grid(row=1,column=2,columnspan=2,sticky='w',pady=(12,0),padx=(8,0))
        ttk.Button(shared,text='保存全局配置',style='Accent.TButton',command=self.save_system_config).grid(row=2,column=3,sticky='e',pady=(14,0))
        for row,(key,label) in enumerate([('crossref','Crossref Plus Key'),('wos','WOS API Key'),('elsevier_inst_token','Elsevier InstToken')],2):
            variable=tk.StringVar(value=keys.get(key,''))
            self.extra_keys[key]=variable
            ttk.Label(container,text=label,style='Card.TLabel').grid(row=row,column=0,sticky='w',pady=(12,0))
            entry=ttk.Entry(container,textvariable=variable,show='*')
            entry.grid(row=row,column=1,columnspan=4,sticky='ew',padx=(10,6),pady=(12,0))
            self.extra_key_entries.append(entry)

    def _toggle_key_visibility(self):
        super()._toggle_key_visibility()
        for entry in getattr(self,'extra_key_entries',[]):
            entry.configure(show='' if self.show_api_keys.get() else '*')

    def _load_config_into_vars(self):
        super()._load_config_into_vars()
        try:
            current=search.read_config(self._config_file_path()) if self._config_file_path().exists() else {}
            self.output_dir.set(str(pdf_directory(current,APP_DIR)))
            if hasattr(self,'search_limit_var'):self.search_limit_var.set(str(search_limit(current)))
        except Exception:pass
        if hasattr(self,'extra_keys'):
            try:
                config=search.read_config(self._config_file_path())
                for key,variable in self.extra_keys.items():variable.set((config.get('api_keys') or {}).get(key,''))
            except Exception:pass

    def save_system_config(self,*,silent=False):
        try:
            limit=search_limit({'literature_search':{'limit':self.search_limit_var.get()}})
            if not self.output_dir.get().strip():raise ValueError('请设置 PDF 目录')
            directory=pdf_directory({'paths':{'pdf_dir':self.output_dir.get().strip()}},APP_DIR)
        except (ValueError,TypeError) as error:
            self.settings_status_text.set(str(error))
            if not silent:messagebox.showerror('全局配置无效',str(error),parent=self)
            return False
        # Suppress the inherited success dialog until all credential fields save.
        if not super().save_system_config(silent=True):
            if not silent:messagebox.showerror('配置保存失败',self.settings_status_text.get(),parent=self)
            return False
        try:
            from suite_search_ui import save_config_changes
            save_config_changes(self._config_file_path(),keys={k:v.get().strip() for k,v in self.extra_keys.items()},
                                preferences={'limit':limit},pdf_dir=str(directory))
            self.output_dir.set(str(directory))
            if hasattr(self,'search_panel'):
                self.search_panel.config_data=self.search_panel.load_config()
                self.search_panel.sync_sources(preserve=True)
                self.metadata_panel.config_data=metadata_gui.load_settings(APP_DIR)
                self.metadata_panel.folder.set(str(directory))
                self.metadata_panel.sync_api_controls()
            self.settings_status_text.set('全局配置已保存，四个页面与 CLI 共用此配置。')
            return True
        except Exception as error:
            self.settings_status_text.set('配置保存失败：'+type(error).__name__)
            if not silent:messagebox.showerror('配置保存失败',type(error).__name__,parent=self)
            return False

    def _tab_changed(self,event=None):
        if not hasattr(self,'search_panel'):return
        if self.notebook.select()==str(self.search_tab) and not self.search_panel.busy:
            try:
                self.search_panel.config_data=self.search_panel.load_config()
                self.search_panel.sync_sources(preserve=True)
            except Exception:self.search_panel.status.set('配置读取失败，请检查全局配置。')
        if self.notebook.select()==str(self.metadata_tab) and not self.metadata_panel.busy:
            try:
                self.metadata_panel.config_data=metadata_gui.load_settings(APP_DIR)
                self.metadata_panel.folder.set(self.metadata_panel.config_data['pdf_dir'])
            except Exception:self.metadata_panel.status.set('配置读取失败，请检查全局配置。')

    def queue_download(self,records):
        dois=list(dict.fromkeys(search.doi_normalize(r.get('doi')) for r in records if search.doi_normalize(r.get('doi'))))
        if not dois:
            messagebox.showinfo('没有 DOI','请选择带 DOI 的文献。',parent=self);return
        if self._download_active:
            messagebox.showinfo('正在下载','请等待当前下载任务结束后再发送。',parent=self);return
        self.doi_text.delete('1.0','end')
        self.doi_text.insert('1.0','\n'.join(dois))
        self.notebook.select(self.download_tab)
        self.status_text.set(f'已接收 {len(dois)} 篇，点击“开始下载”执行。')

    def queue_metadata(self,records):
        if self.metadata_panel.busy:
            messagebox.showinfo('正在补全','请等待当前补全任务结束。',parent=self);return
        saved=[]
        for incoming in records:
            doi=search.doi_normalize(incoming.get('doi'))
            if not doi:continue
            record,_=self.metadata_panel.library.import_record(doi,source='online_search')
            for field in ('doi',)+search.FIELDS:
                if incoming.get(field) and not record.get(field):
                    record[field]=copy.deepcopy(incoming[field])
                    record.setdefault('field_sources',{})[field]=incoming.get('field_sources',{}).get(field,'')
            saved.append(self.metadata_panel.library.save(record))
        if not saved:
            messagebox.showinfo('没有 DOI','请选择带 DOI 的文献。',parent=self);return
        try:
            saved,linked=self.metadata_panel.library.link_local_pdfs(saved,self.metadata_panel.folder.get())
            self.status_text.set(f'已导入 {len(saved)} 条文献，新关联 {linked} 个本地 PDF。')
        except OSError as error:
            messagebox.showwarning('PDF 关联检查失败',f'文献记录已导入，但本地 PDF 检查失败：{error}',parent=self)
        self.metadata_panel.query.set('')
        self.metadata_panel.filter.set('全部状态')
        self.metadata_panel.refresh()
        ids=[str(r['id']) for r in saved]
        self.metadata_panel.tree.selection_set(ids)
        self.metadata_panel.tree.see(ids[0])
        self.notebook.select(self.metadata_tab)

    def _on_close(self):
        if hasattr(self,'search_panel'):
            self.search_panel.stop.set()
            self.search_panel.closed=True
        if hasattr(self,'metadata_panel'):self.metadata_panel.stop.set()
        try:
            for job in self.tk.call('after','info'):
                self.tk.call('after','cancel',job)
        except tk.TclError:pass
        super()._on_close()


def launch():
    download_gui.enable_windows_dpi_awareness()
    app=SuiteApp()
    app.mainloop()
    return 0
