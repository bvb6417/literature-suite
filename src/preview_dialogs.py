"""Messagebox-compatible rounded modals with explicit synchronous return values."""
import tkinter as tk
from tkinter import ttk
from switch_lite import Modal

class MessageBoxes:
    def _show(self,title,message,choices,default=None,**options):
        parent=options.get('parent') or tk._default_root
        if parent is None:
            from tkinter import messagebox as native
            return native.showerror(title,message)
        dialog=Modal(parent,title or '提示',width=500,height=300)
        result=[default]
        ttk.Label(dialog.body,text=str(message),wraplength=round(440*dialog.scale),justify='left').pack(fill='x',anchor='w')
        if options.get('detail'):
            ttk.Label(dialog.body,text=str(options['detail']),wraplength=round(440*dialog.scale),style='Muted.TLabel').pack(fill='x',pady=(12,0))
        def finish(value):
            result[0]=value
            dialog.close()
        label,value=choices[0]
        dialog.primary.configure(text=label,command=lambda:finish(value))
        for label,value in choices[1:]:
            ttk.Button(dialog.actions,text=label,command=lambda v=value:finish(v)).pack(side='right',padx=(0,8))
        parent.wait_window(dialog)
        return result[0]
    def showinfo(self,title=None,message=None,**kw):return self._show(title,message,[('知道了','ok')],'ok',**kw)
    showwarning=showinfo
    showerror=showinfo
    def askyesno(self,title=None,message=None,**kw):return self._show(title,message,[('是',True),('否',False)],False,**kw)
    def askokcancel(self,title=None,message=None,**kw):return self._show(title,message,[('确定',True),('取消',False)],False,**kw)
    def askyesnocancel(self,title=None,message=None,**kw):return self._show(title,message,[('是',True),('否',False),('取消',None)],None,**kw)
    def askretrycancel(self,title=None,message=None,**kw):return self._show(title,message,[('重试',True),('取消',False)],False,**kw)
    def askquestion(self,title=None,message=None,**kw):return self._show(title,message,[('是','yes'),('否','no')],'no',**kw)

messagebox=MessageBoxes()
