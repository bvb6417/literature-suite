"""First-run access setup and verified custom WebVPN registration."""
import json
from pathlib import Path
import queue
import tempfile
import threading
from urllib.parse import urlsplit
import webbrowser
import tkinter as tk
from tkinter import ttk
from providers import institutional_access as access
from suite_paths import APP_DIR
from switch_lite import Modal

marker = APP_DIR / 'state' / 'setup_v1.done'
custom = APP_DIR / 'state' / 'custom_schools.json'

def read(path, fallback):
    return json.loads(path.read_text('utf-8')) if path.exists() else fallback

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
    Path(out.name).replace(path)

def verify_access(config, name, address, username, password):
    catalog = access.schools_path_from_config(config)
    entries, _ = access.load_school_catalog(catalog)
    entry = next((e for e in entries if e.name.casefold() == name.casefold()), None)
    addition = None
    if entry is None:
        if not name or not address:
            raise ValueError('新学校需要填写学校名和 WebVPN 地址。')
        if '://' not in address:
            address = 'https://' + address
        url = urlsplit(address)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('请输入学校官方 HTTPS WebVPN 入口地址。')
        addition = {'name':name, 'type':'webvpn', 'base_url':address, 'origin':'custom'}
        entry = access._entry_from_mapping(addition, origin='custom')
    elif address and address.rstrip('/') != entry.base_url.rstrip('/'):
        raise ValueError('该学校已存在，输入的地址与配置库不同。')
    if bool(username) != bool(password):
        raise ValueError('自动填表需要同时输入账号和密码。')
    if username and not entry.auto_login:
        raise ValueError('此学校尚无自动填表规则。请清空账号密码，在学校登录窗口输入并完成验证。')
    custom.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=custom.parent) as folder:
        test_catalog = catalog
        if addition:
            test_catalog = Path(folder) / 'schools.json'
            write(test_catalog, {'schools':[addition]})
        trial = access.apply_school_selection(json.loads(json.dumps(config)),entry,test_catalog)
        if username:
            trial["institution"]["_setup_credentials"] = (username,password)
        if entry.access_type in ('webvpn','ezproxy'):
            status = access.institution_session_status(trial)
            if not status.get('valid'):
                result = access.login_institution(trial, 180)
                if not result.get('ok'):
                    raise ValueError(result.get('reason') or '学校代理会话未通过校验。')
                status = access.institution_session_status(trial)
                if not status.get('valid'):
                    raise ValueError(status.get('message') or '登录后代理访问校验失败。')
    return entry, catalog, addition

def commit_access(entry, addition, username, password):
    if username:
        import win32cred
        win32cred.CredWrite({'Type':win32cred.CRED_TYPE_GENERIC,'TargetName':'LiteratureWorkbench/'+entry.id,
                             'UserName':username,'CredentialBlob':password.encode('utf-16-le'),
                             'Persist':win32cred.CRED_PERSIST_LOCAL_MACHINE})
    if addition:
        data = read(custom, {'schools':[]})
        data['schools'].append(addition)
        write(custom, data)

def prompt_if_needed(app):
    prefs=read(marker,{})
    if prefs.get('dismissed'):
        return
    cfg=read(app._config_file_path(),{})
    institution=cfg.get('institution') or {}
    reasons=[]
    if not prefs.get('seen'):reasons.append('首次使用')
    if not (institution.get('school_id') or institution.get('school_name')):reasons.append('尚未选择学校接入方式')
    if not app.elsevier_api_key.get().strip():reasons.append('尚未配置 Elsevier API Key')
    if not reasons:return
    write(marker,{'seen':True})
    win=Modal(app,'下载前准备',width=555,height=320)
    ttk.Label(win.body,text='、'.join(reasons)+'。',wraplength=round(475*win.scale)).pack(anchor='w')
    ttk.Separator(win.body).pack(fill='x',pady=12)
    ttk.Label(win.body,text='请到「全局配置」选择学校并填写 API Key。校外访问订阅全文时，请先连接校园 VPN 或在学校 WebVPN 完成登录。',wraplength=round(475*win.scale)).pack(anchor='w')
    muted=tk.BooleanVar(win,value=False)
    ttk.Checkbutton(win.body,text='不再提示',variable=muted,command=lambda:write(marker,{'seen':True,'dismissed':muted.get()})).pack(anchor='w',pady=10)
    def configure():
        win.close()
        app.notebook.select(app.settings_tab)
    win.primary.configure(text='前往全局配置',command=configure)
    win.secondary.configure(text='稍后',command=win.close)
    win.secondary.pack(side='right',padx=8)

def show_school(app,existing=None):
    if getattr(app,'_setup_busy',False):return
    config_path=app._config_file_path()
    cfg=read(config_path,{})
    # Input methods and normal click-to-activate rely on a managed Windows window.
    win=tk.Toplevel(app)
    win.withdraw()
    win.title('刷新机构登录' if existing else '新增学校')
    win.transient(app)
    win.resizable(False,False)
    win.scale=app.winfo_fpixels('1i')/96
    bar=ttk.Frame(win,padding=(14,4,2,4))
    bar.pack(fill='x')
    caption=ttk.Label(bar,text=win.title(),style='Strong.TLabel')
    caption.pack(side='left')
    ttk.Separator(win).pack(fill='x')
    body=ttk.Frame(win,padding=16)
    body.pack(fill='both',expand=True)
    body.columnconfigure(1,weight=1)
    actions=ttk.Frame(win,padding=(16,0,16,16))
    actions.pack(fill='x')
    win._closed=False
    def close():
        if win._closed:return
        win._closed=True
        if getattr(app,'_active_modal',None) is win:app._active_modal=None
        win.chrome.detach()
        win.destroy()
    win.close=close
    win.protocol('WM_DELETE_WINDOW',close)
    win.bind('<Escape>',lambda event:close())
    app._active_modal=win
    win.primary=ttk.Button(actions,style='Primary.TButton')
    win.primary.pack(side='right')
    win.secondary=ttk.Button(actions)
    fields=[]
    def field(row,label,value='',secret=False,hint=''):
        var=tk.StringVar(win,value=value)
        ttk.Label(body,text=label).grid(row=row,column=0,sticky='w',padx=(0,12))
        editor=ttk.Entry(body,textvariable=var,width=38,show='*' if secret else '',state='readonly' if existing and row<2 else 'normal')
        editor.grid(row=row,column=1,sticky='ew',pady=4)
        if hint:
            from tkwebui.placeholder import set_placeholder
            from tkinter import font as tkfont
            font=tkfont.nametofont("TkTextFont",root=win)
            editor.configure(width=max(38,(font.measure(hint)+20)//max(1,font.measure("0"))+1))
            set_placeholder(editor,hint)
        if not editor.instate(['readonly']):fields.append(editor)
        return var
    school=field(0,'学校名称',existing.name if existing else '')
    address=field(1,'WebVPN 地址',existing.base_url if existing else '',
                   hint='' if existing else 'https://webvpn(wvpn).xxxx.edu.cn  不知道可必应检索“xx大学 webvpn”')
    username=tk.StringVar(win);password=tk.StringVar(win)
    if existing and existing.auto_login:
        username=field(2,'自动登录账号（选填）')
        password=field(3,'自动登录密码',secret=True)
    login_hint='请在学校网页完成认证。通过代理访问校验后保存。'
    if existing and existing.auto_login:
        account,secret,source=access.resolve_institution_credentials(cfg,existing)
        source_label={'config':'JSON 配置','environment':'环境变量','windows':'Windows 凭据管理器'}.get(source,'本机配置')
        if account and secret:
            login_hint=f'已识别 {source_label} 中的账号密码。两项留空即可复用；填写新账号密码可更新。'
        elif account or secret:
            login_hint=f'{source_label} 中的账号密码不完整，请补全配置，或在此同时填写账号和密码。'
        else:
            login_hint='未配置自动登录账号密码。两项留空可在学校网页手动登录。'
        # The form reports presence only; secrets are not copied into widgets.
        del account,secret
    status=tk.StringVar(win,value=login_hint)
    ttk.Label(body,textvariable=status,wraplength=round(520*win.scale)).grid(row=4,column=0,columnspan=2,sticky='w',pady=8)
    def start():
        values=school.get().strip(),address.get().strip(),username.get().strip(),password.get()
        if not values[0]:
            status.set('请选择学校或输入学校名称。');return
        if not existing and (not values[1] or any(e.name.casefold()==values[0].casefold() for e in access.load_school_catalog(access.schools_path_from_config(cfg))[0])):
            status.set('请输入未收录的学校名称及其 WebVPN 地址。');return
        app._setup_busy=True
        win.primary.configure(state='disabled')
        win.secondary.configure(state='disabled')
        status.set('正在测试学校接入。需要认证时，请在打开的浏览器中操作。')
        results=queue.Queue()
        def worker():
            try:results.put((verify_access(cfg,*values),None))
            except Exception as error:results.put((None,str(error)))
        threading.Thread(target=worker,daemon=True).start()
        def poll():
            if results.empty():
                app.after(150,poll);return
            app._setup_busy=False
            result,error=results.get()
            if win._closed:return
            win.primary.configure(state='normal');win.secondary.configure(state='normal')
            if error:status.set(error);return
            entry,path,addition=result
            try:
                commit_access(entry,addition,values[2],values[3])
            except Exception as exc:
                status.set(f"无法保存学校或自动登录凭据：{exc}");return
            fresh=access.apply_school_selection(read(config_path,{}),entry,path)
            if values[2]:
                # Newly entered credentials were just stored in Windows.
                # Remove the old JSON pair so it cannot override that update.
                fresh['institution'].pop('login_username',None)
                fresh['institution'].pop('login_password',None)
            fresh['institution']['auto_login']=True
            write(config_path,fresh)
            app._load_config_into_vars();app.load_schools()
            app.status_text.set('已设置 '+entry.name+(' · Cookie 已保存，会话验证通过' if entry.access_type in ('webvpn','ezproxy') else ' · 请确认校园网或 VPN 已连接'))
            win.close()
        app.after(150,poll)
    win.primary.configure(text='测试并保存',command=start)
    win.secondary.configure(text='取消',command=win.close)
    win.secondary.pack(side='right',padx=8)


    win.update_idletasks()
    width,height=win.winfo_reqwidth(),win.winfo_reqheight()
    x=app.winfo_rootx()+max(0,(app.winfo_width()-width)//2)
    y=app.winfo_rooty()+max(0,(app.winfo_height()-height)//2)
    win.geometry(f'+{x}+{y}')
    win.deiconify()
    win.wait_visibility()
    from window_chrome import WindowChrome
    win.chrome=WindowChrome(win,bar,drag_widgets=(bar,caption),on_close=close,dialog=True)
    win.update_idletasks()
    win.grab_set()
    win.lift()
    (fields[0] if fields else win.primary).focus_force()
