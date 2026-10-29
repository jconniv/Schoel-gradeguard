"""Plain-language start screen; existing engineering settings stay available."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from gradeguard_theme import GOLD,MUTED


class QuickSetupWindow(tk.Toplevel):
    def __init__(self,view):
        super().__init__(view.app)
        self.view=view; self.app=view.app
        self.title('GradeGuard | Open your plans'); self.geometry('850x470'); self.minsize(760,440)
        self.transient(self.app); self.grab_set()
        body=ttk.Frame(self,padding=22); body.pack(fill='both',expand=True)
        ttk.Label(body,text='Open your plans',font=('Segoe UI',22,'bold')).pack(anchor='w')
        ttk.Label(body,text='Start with the plan PDF. Add CAD or a surface if you have them.',foreground=MUTED).pack(anchor='w',pady=(5,18))
        self.file_row(body,'Plan PDF',self.app.pdf_var,[('PDF plans','*.pdf')])
        self.extra=tk.BooleanVar(value=bool(self.app.cad_var.get() or self.app.surface_var.get()))
        ttk.Checkbutton(body,text='I also have a DXF drawing or LandXML surface',variable=self.extra,command=self.toggle).pack(anchor='w',pady=12)
        self.more=ttk.Frame(body)
        self.file_row(self.more,'DXF drawing',self.app.cad_var,[('Civil DXF','*.dxf')])
        self.file_row(self.more,'LandXML surface',self.app.surface_var,[('LandXML','*.xml *.landxml')])
        ttk.Checkbutton(self.more,text='For a DXF with unspecified units: I checked that it uses US survey feet',variable=self.app.unitless_confirm_var).pack(anchor='w',pady=7)
        self.toggle()
        bottom=ttk.Frame(body); bottom.pack(side='bottom',fill='x',pady=(16,0))
        ttk.Button(bottom,text='Continue →',style='Accent.TButton',command=self.proceed).pack(side='right')
        ttk.Label(bottom,text='Tool settings and output location are under Advanced settings.',wraplength=490,foreground=MUTED).pack(side='left')

    def file_row(self,parent,label,variable,types):
        row=ttk.Frame(parent); row.pack(fill='x',pady=5)
        ttk.Label(row,text=label,width=19).pack(side='left')
        ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True)
        def browse():
            path=filedialog.askopenfilename(parent=self,filetypes=types)
            if path: variable.set(path)
        ttk.Button(row,text='Choose file…',command=browse).pack(side='left',padx=(7,0))

    def toggle(self):
        if self.extra.get(): self.more.pack(fill='x')
        else: self.more.pack_forget()

    def proceed(self):
        path=Path(self.app.pdf_var.get())
        if path.suffix.lower()!='.pdf' or not path.is_file():
            messagebox.showinfo('Choose the plans','Choose an existing PDF plan file to continue.',parent=self); return
        if not self.extra.get(): self.app.cad_var.set(''); self.app.surface_var.set('')
        self.destroy(); self.view.quick_calculate()


def build_start(view,parent):
    body=view.scroll_body(parent)
    ttk.Label(body,text="Let’s set the grade.",font=('Segoe UI',29,'bold')).pack(anchor='w',pady=(8,8))
    ttk.Label(body,text='Open the plans. Calculate the points. Review the results.',font=('Segoe UI',13),foreground=MUTED).pack(anchor='w')
    cards=ttk.Frame(body); cards.pack(fill='x',pady=18)
    actions=[('1','Open plans','Pick your PDF. Add CAD files if you have them.',lambda:QuickSetupWindow(view)),
             ('2','Calculate','Calculate curb, sidewalk and grade points.',view.quick_calculate),
             ('3','Review results','Click a point to see its grade and where it came from.',view.inspect_points)]
    for i,(number,label,description,command) in enumerate(actions):
        cards.columnconfigure(i,weight=1,uniform='step')
        card=ttk.LabelFrame(cards,text='STEP '+number,padding=18); card.grid(row=0,column=i,sticky='nsew',padx=(0,12 if i<2 else 0))
        ttk.Label(card,text=label,font=('Segoe UI',18,'bold')).pack(anchor='w')
        ttk.Label(card,text=description,wraplength=255,foreground=MUTED).pack(anchor='w',pady=(10,20))
        button=ttk.Button(card,text=label+' →',style='Accent.TButton',command=command); button.pack(side='bottom',fill='x')
        view.job_buttons.append(button)
    view.home_summary=tk.StringVar(value='No plans selected. Start with Open plans.')
    ttk.Label(body,textvariable=view.home_summary,foreground=GOLD,wraplength=980,font=('Segoe UI',12)).pack(anchor='w',pady=(0,12))
    ttk.Label(body,text='PDF only? The next screen guides you through scale, paths and grade controls once. You can save that setup for the next run.',foreground=MUTED,wraplength=980).pack(anchor='w')
    ttk.Button(body,text='Advanced settings',command=view.show_advanced).pack(anchor='w',pady=(12,0))
