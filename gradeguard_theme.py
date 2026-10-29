"""Shared native UI colors taken from the GradeGuard opening scene."""
from tkinter import ttk

BG = '#111e2a'
PANEL = '#0c1721'
INPUT = '#1a2b37'
BORDER = '#2b3d49'
TEXT = '#f3f5ed'
MUTED = '#a7b8c4'
GREEN = '#9bb76e'
GOLD = '#d9af52'
GOOD = '#a8c87d'
ERROR = '#ff9585'


def apply_theme(root):
    root.configure(background=BG)
    for key,value in [('background',BG),('foreground',TEXT),('insertBackground',TEXT),
                      ('selectBackground','#36533e'),('selectForeground',TEXT),
                      ('highlightBackground',BORDER),('highlightColor',GOLD)]:
        root.option_add('*'+key,value)
    root.option_add('*Text.background',PANEL)
    root.option_add('*Listbox.background',INPUT)
    root.option_add('*TCombobox*Listbox.background',INPUT)
    root.option_add('*TCombobox*Listbox.foreground',TEXT)
    style=ttk.Style(root); style.theme_use('clam')
    style.configure('.',font=('Segoe UI',10),background=BG,foreground=TEXT,
                    bordercolor=BORDER,lightcolor=BORDER,darkcolor=BORDER,focuscolor=GOLD)
    style.configure('TFrame',background=BG)
    style.configure('TLabel',background=BG,foreground=TEXT)
    style.configure('TButton',padding=(12,8),background='#263a3c',foreground=TEXT,borderwidth=1)
    style.map('TButton',background=[('disabled',INPUT),('active','#365044')],foreground=[('disabled','#8493a0')])
    style.configure('Accent.TButton',background=GREEN,foreground='#162519',font=('Segoe UI',10,'bold'),padding=(18,11))
    style.map('Accent.TButton',background=[('disabled','#3b4b3c'),('active','#acbf8c')],foreground=[('disabled',MUTED)])
    for kind in ('TEntry','TCombobox','TSpinbox'):
        style.configure(kind,fieldbackground=INPUT,foreground=TEXT,insertcolor=TEXT,padding=5,arrowcolor=GOLD)
        style.map(kind,fieldbackground=[('readonly',INPUT),('disabled',PANEL)],foreground=[('disabled',MUTED),('readonly',TEXT)],selectbackground=[('!disabled','#36533e')],selectforeground=[('!disabled',TEXT)])
    for kind in ('TCheckbutton','TRadiobutton'):
        style.configure(kind,background=BG,foreground=TEXT,padding=(0,3),indicatorbackground=INPUT)
        style.map(kind,background=[('active',BG)],foreground=[('disabled',MUTED)],indicatorbackground=[('selected',GREEN)])
    style.configure('TLabelframe',background=BG,bordercolor=BORDER,relief='solid')
    style.configure('TLabelframe.Label',background=BG,foreground=GOLD,font=('Segoe UI',11,'bold'))
    style.configure('Header.TFrame',background=PANEL)
    style.configure('Header.TLabel',background=PANEL,foreground=MUTED)
    style.configure('Logo.TLabel',background='#edf1e9',padding=8)
    style.configure('Title.TLabel',background=PANEL,foreground=TEXT,font=('Segoe UI',22,'bold'))
    style.configure('Heading.TLabel',foreground=TEXT,font=('Segoe UI',16,'bold'))
    style.configure('Muted.TLabel',foreground=MUTED)
    for prefix,padding in [('',(20,9)),('Steps.',(24,12))]:
        style.configure(prefix+'TNotebook',background=BG,borderwidth=0)
        style.configure(prefix+'TNotebook.Tab',padding=padding,background=INPUT,foreground=MUTED)
        style.map(prefix+'TNotebook.Tab',background=[('selected','#29403e'),('active','#263a3c')],foreground=[('selected',GOLD),('active',TEXT)])
    style.configure('Treeview',background=PANEL,fieldbackground=PANEL,foreground=TEXT,rowheight=25,bordercolor=BORDER)
    style.map('Treeview',background=[('selected','#36533e')],foreground=[('selected',TEXT)])
    style.configure('Treeview.Heading',background=INPUT,foreground=GOLD,padding=(7,6))
    style.map('Treeview.Heading',background=[('active','#29403e')])
    for kind in ('Vertical.TScrollbar','Horizontal.TScrollbar'):
        style.configure(kind,background='#29403e',troughcolor=PANEL,arrowcolor=MUTED,bordercolor=BORDER)
    style.configure('Horizontal.TProgressbar',background=GREEN,troughcolor=PANEL)
    style.configure('TPanedwindow',background=BORDER)
    return style
