"""Workbench-only names/layout adapters; generic components live in switch_lite_* modules."""
from tkinter import ttk
from switch_lite import PALETTE, apply_theme
from switch_lite_widgets import (SectionCard, ResponsiveTable, initial_window_bounds,
    align_tree_scrollbar, auto_hide_text_scrollbar)

def style_workbench(root):
    apply_theme(root)
    style=ttk.Style(root)
    for alias,base in [('Accent.TButton','Primary.TButton'),('Stop.TButton','TButton'),('Header.TButton','Quiet.TButton')]:
        style.layout(alias,style.layout(base))
        style.configure(alias,**style.configure(base))
        style.map(alias,**style.map(base))
    style.configure('Surface.TFrame',background=PALETTE['bg'])
    style.configure('Card.TLabel',foreground=PALETTE['muted'],background=PALETTE['bg'])
    style.layout('Workbench.TNotebook.Tab',[])
    style.configure('Workbench.TNotebook',borderwidth=0,tabmargins=0,padding=0)
    style.element_create('Workbench.Notebook.client','from','default','Notebook.client')
    style.layout('Workbench.TNotebook',[('Workbench.Notebook.client',{'sticky':'nsew'})])
