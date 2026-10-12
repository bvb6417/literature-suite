"""Switch Lite shell around the real workbench pages in this isolated copy."""
import tkinter as tk
from tkinter import ttk
import traceback
import json
from suite_gui import SuiteApp
from suite_paths import APP_DIR
from preview_widgets import style_workbench, align_tree_scrollbar, auto_hide_text_scrollbar
from switch_lite_widgets import install_input_focus, focus_when_mapped
from switch_lite import PALETTE, Sidebar, Modal, icon, enable_dpi_awareness
from window_chrome import WindowChrome

class PreviewApp(SuiteApp):
    def __init__(self):
        super().__init__()
        self.title('文献工作台')

    def _configure_style(self):
        self._azure_theme_loaded=False
        style_workbench(self)

    def _build_download_tab(self):
        super()._build_download_tab()
        self.status_label.master.columnconfigure(0,pad=18)
        for name,width in [('doi',190),('status',80),('source',110),('size',80),('elapsed',100)]:
            self.result_tree.column(name,width=width,minwidth=70)
        self.result_tree.column('message',width=190,minwidth=160)
        horizontal=ttk.Scrollbar(self.result_tree.master,orient='horizontal',command=self.result_tree.xview)
        horizontal.grid(row=1,column=0,sticky='ew')
        self.result_tree.configure(xscrollcommand=horizontal.set)
        self.main_pane.bind('<ButtonRelease-1>',self._remember_pane_ratio,add='+')

    def _remember_pane_ratio(self,event=None):
        if self.main_pane.winfo_width()>1:
            self._preferred_pane_ratio=self.main_pane.sashpos(0)/self.main_pane.winfo_width()

    def _set_initial_pane_position(self):
        self._pane_ratio_job=None
        try:
            width=self.main_pane.winfo_width()
            if width>1:self.main_pane.sashpos(0,round(width*getattr(self,'_preferred_pane_ratio',1/3)))
        except tk.TclError:pass

    def _build_ui(self):
        super()._build_ui()
        self.rowconfigure(0,weight=0)
        self.rowconfigure(1,weight=0)
        self.rowconfigure(2,weight=1)
        self.columnconfigure(0,weight=0)
        self.columnconfigure(1,weight=0)
        self.columnconfigure(2,weight=1)
        self.notebook.configure(style='Workbench.TNotebook')
        self.notebook.grid_configure(row=2,column=2,padx=8,pady=(4,12))
        self.side=ttk.Frame(self,style='Sidebar.TFrame',width=220)
        self.side.grid(row=0,column=0,rowspan=3,sticky='nsew')
        self.side.pack_propagate(False)
        self.brand=ttk.Frame(self.side,style='Sidebar.TFrame',padding=(12,14))
        self.brand.pack(fill='x')
        self.collapse_button=ttk.Button(self.brand,image=icon(self,'collapse'),style='Nav.TButton',command=self.toggle_sidebar)
        self.collapse_button.pack(side='right')
        self.brand_label=ttk.Label(self.brand,text='文献工作台',style='Sidebar.Strong.TLabel')
        self.brand_label.pack(side='left',padx=(4,0))
        self.page_map={'search':self.search_tab,'metadata':self.metadata_tab,'download':self.download_tab,'settings':self.settings_tab}
        items=[('search','文献检索','search'),('metadata','元数据补全','library'),('download','文献下载','download'),('settings','全局配置','settings')]
        self.navigation=Sidebar(self.side,items,self.select_page,width=220)
        self.navigation.pack(fill='both',expand=True,pady=(12,0))
        self.copy_label=ttk.Label(self.side,text='文献工作台',style='Sidebar.Muted.TLabel')
        self.copy_label.pack(anchor='w',padx=22,pady=18)
        ttk.Separator(self,orient='vertical').grid(row=0,column=1,rowspan=3,sticky='ns')
        self.toolbar=ttk.Frame(self,padding=(22,10,12,10))
        self.toolbar.grid(row=0,column=2,sticky='ew')
        self.chrome=WindowChrome(self,self.toolbar,drag_widgets=(self.toolbar,self.brand,self.brand_label),on_close=self._on_close,sidebar=self.side)
        self.page_name=tk.StringVar(value='文献检索')
        self.page_label=ttk.Label(self.toolbar,textvariable=self.page_name,style='Strong.TLabel')
        self.page_label.pack(side='left')
        self.page_label.bind('<ButtonPress-1>',self.chrome.drag)
        self.page_label.bind('<Double-Button-1>',self.chrome.double_click)
        self.chrome.drag_widgets.append(self.page_label)
        ttk.Button(self.toolbar,text='打开下载目录',style='Quiet.TButton',command=self.open_output_dir).pack(side='right')
        ttk.Separator(self).grid(row=1,column=2,sticky='ew')
        self.notebook.bind('<<NotebookTabChanged>>',self.sync_navigation,add='+')
        self.navigation.select('search',notify=False)
        self.search_panel.search_button.configure(style='Primary.TButton')
        self.metadata_panel.enrich_button.configure(style='Primary.TButton')
        self.progress.configure(height=10)
        self._style_texts(self)
        self._bind_settings_wheel()
        self._bind_column_widths()
        for tree in (self.search_panel.tree,self.metadata_panel.tree,self.result_tree):
            align_tree_scrollbar(tree)
        self._set_sidebar_collapsed(True,flush=False)
        self.search_panel.detail=self.search_detail
        self.search_panel.tree.bind('<Double-1>',self.search_detail)
        # Existing button/menu callbacks were registered before the override.
        for widget in self.walk(self.search_panel):
            if isinstance(widget,ttk.Button) and widget.cget('text')=='查看详情':widget.configure(command=self.search_detail)
        self.search_panel.row_menu.entryconfigure(2,command=self.search_detail)

    def _bind_column_widths(self):
        self._column_settings_file=APP_DIR/'preview_ui.json'
        try:self._column_settings=json.loads(self._column_settings_file.read_text(encoding='utf-8'))
        except (OSError,ValueError):self._column_settings={}
        scale=self.winfo_fpixels('1i')/96
        for key,tree in [('search',self.search_panel.tree),('metadata',self.metadata_panel.tree),('download',self.result_tree)]:
            saved=self._column_settings.get(key,{})
            for column in tree['columns']:
                tree.column(column,stretch=False)
                if column in saved:
                    try:tree.column(column,width=max(45,min(3000,round(float(saved[column])*scale))))
                    except (TypeError,ValueError):pass
            def press(event,widget=tree):
                widget._manual_column_resize=widget.identify_region(event.x,event.y)=='separator'
            def release(event,widget=tree,name=key):
                if getattr(widget,'_manual_column_resize',False):
                    widget._manual_column_resize=False
                    self.after_idle(lambda:self._save_column_widths(name,widget))
            tree.bind('<ButtonPress-1>',press,add='+')
            tree.bind('<ButtonRelease-1>',release,add='+')

    def _save_column_widths(self,key,tree):
        scale=self.winfo_fpixels('1i')/96
        layout=getattr(tree,'_responsive_table',None)
        widths=layout.remember_columns() if layout else {column:tree.column(column,'width') for column in tree['columns']}
        self._column_settings[key]={column:round(width/scale,2) for column,width in widths.items()}
        self._column_settings_file.write_text(json.dumps(self._column_settings,ensure_ascii=False,indent=2),encoding='utf-8')

    def _bind_settings_wheel(self):
        # Tk does not bubble a child's wheel events through its parent frames.
        # A page-local bindtag captures only this page, never other toplevels.
        tag=f'WorkbenchSettingsWheel_{id(self)}'
        self._settings_wheel_remainder=0
        def wheel(event):
            if self.notebook.select()!=str(self.settings_tab):return None
            if self.grab_current() is not None:return None
            bounds=self.settings_canvas.bbox(self._settings_canvas_window)
            if bounds and bounds[3]-bounds[1]<=self.settings_canvas.winfo_height():
                self.settings_canvas.yview_moveto(0)
                return 'break'
            if getattr(event,'num',None) in (4,5):
                units=-3 if event.num==4 else 3
            else:
                self._settings_wheel_remainder+=event.delta
                notches=int(self._settings_wheel_remainder/120)
                self._settings_wheel_remainder-=notches*120
                units=-notches*3
            if units:self.settings_canvas.yview_scroll(units,'units')
            return 'break'
        for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):
            self.bind_class(tag,sequence,wheel)
        for widget in (self.settings_tab,*self.walk(self.settings_tab)):
            widget.bindtags((tag,*widget.bindtags()))

    def walk(self,parent):
        for child in parent.winfo_children():
            yield child
            yield from self.walk(child)

    def _style_texts(self,parent):
        install_input_focus(parent)
        for widget in self.walk(parent):
            if isinstance(widget,tk.Text):
                auto_hide_text_scrollbar(widget)
                widget.configure(background=PALETTE['bg'],foreground=PALETTE['fg'],relief='flat',borderwidth=0,
                                 highlightthickness=1,highlightbackground=PALETTE['border'],highlightcolor=PALETTE['blue'],
                                 selectbackground=PALETTE['selected'],selectforeground=PALETTE['fg'])

    def select_page(self,key):
        self.notebook.select(self.page_map[key])

    def sync_navigation(self,event=None):
        if not hasattr(self,'navigation'):return
        key=next((k for k,v in self.page_map.items() if str(v)==self.notebook.select()),'search')
        self.navigation.select(key,notify=False)
        self.page_name.set(self.navigation.labels[key])
        target={'search':self.search_panel.search_entry,'metadata':self.metadata_panel.search_entry.text,
                'download':self.doi_text,'settings':self.global_pdf_entry}[key]
        focus_when_mapped(target,when=lambda:self.notebook.select()==str(self.page_map[key]))

    def toggle_sidebar(self):
        self._set_sidebar_collapsed(not self.navigation.collapsed)

    def _set_sidebar_collapsed(self,collapsed,*,flush=True):
        self.chrome.begin_update()
        try:
            self.navigation.set_collapsed(collapsed)
            self.side.configure(width=72 if collapsed else 220)
            if collapsed:
                self.brand_label.pack_forget();self.copy_label.pack_forget()
            else:
                self.brand_label.pack(side='left',padx=(4,0));self.copy_label.pack(anchor='w',padx=22,pady=18)
            self.collapse_button.configure(image=icon(self,'expand' if collapsed else 'collapse'))
            if flush:self.update_idletasks()
        finally:
            self.chrome.end_update()
            self.chrome.schedule_cache()

    def search_detail(self,event=None):
        panel=self.search_panel
        if event is not None:
            row=panel.tree.identify_row(event.y)
            if not row:return
            panel.tree.selection_set(row)
        records=panel.selected()
        if not records:return
        from suite_search_ui import EXPORT_FIELDS, LABELS
        record=records[0]
        dialog=Modal(self,'元数据详情',width=740,height=660)
        dialog.body.columnconfigure(1,weight=1)
        for row,field in enumerate(EXPORT_FIELDS):
            ttk.Label(dialog.body,text=LABELS[field].split('（')[0],style='Muted.TLabel').grid(row=row,column=0,sticky='nw',padx=(0,18),pady=8)
            value=record.get(field) or '（来源未提供）'
            if isinstance(value,list):value='; '.join(value)
            ttk.Label(dialog.body,text=str(value),wraplength=round(520*dialog.scale),justify='left').grid(row=row,column=1,sticky='ew',pady=8)

    def report_callback_exception(self,exc,value,tb):
        with (APP_DIR/'error.log').open('a',encoding='utf-8') as f:traceback.print_exception(exc,value,tb,file=f)
        if hasattr(self,'status_text'):self.status_text.set('界面异常已记录到 error.log')

    def _on_close(self):
        if getattr(self,'_active_modal',None):self._active_modal.close()
        if hasattr(self,'chrome'):self.chrome.detach()
        super()._on_close()

def launch():
    enable_dpi_awareness()
    app=PreviewApp()
    app.mainloop()
    return 0
