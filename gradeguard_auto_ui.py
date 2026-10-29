"""One-run CAD package and a reusable calibrated PDF sheet workspace."""
import copy
import json
import math
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import fitz
from PIL import Image, ImageTk
import gradeguard_auto as calc
import gradeguard_pdf as pdf

GRADE_MEANINGS={'Top / back of curb':'CURB_TOP','Curb flowline / gutter':'CURB_FL','Sidewalk elevation':'SIDEWALK','Finished ground elevation':'FG'}


class WorkWindow(tk.Toplevel):
    """Workers communicate with Tk only through a main-thread queue."""
    def __init__(self, app, title):
        super().__init__(app)
        self.app=app; self.busy=False; self.result=None
        self.title(title); self.transient(app); self.grab_set()
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.status=tk.StringVar(value='Ready')

    def close(self):
        if self.busy: return
        self.destroy()

    def task(self, operation, done):
        if self.busy or self.app._generation_running: return
        self.busy=True; self.app._generation_running=True
        self.app._workflow.set_busy(True)
        self.events=queue.Queue(); self.status.set('Working...')
        def worker():
            try: self.events.put(('done',operation(lambda text:self.events.put(('progress',text)))))
            except Exception as error: self.events.put(('error',str(error)))
        threading.Thread(target=worker,daemon=True).start()
        def poll():
            try:
                while True:
                    kind,value=self.events.get_nowait()
                    if kind=='progress': self.status.set(value); continue
                    self.busy=False; self.app._generation_running=False; self.app._workflow.set_busy(False)
                    if kind=='error':
                        self.status.set('Needs attention: '+value)
                        messagebox.showerror('Calculation needs attention',value,parent=self)
                    else: done(value)
                    return
            except queue.Empty: self.after(100,poll)
        self.after(100,poll)

    def finished(self, result):
        self.result=result
        self.app.last_calculation_review=result['manifest'].get('review_file','')
        failed=[v for v in result['manifest']['tools'] if v['status']=='Failed']
        self.status.set(('Completed with '+str(len(failed))+' failed item(s). ' if failed else 'Calculation complete. ')+result['folder'])
        self.app.log_msg(self.status.get())
        self.app._workflow.show_results()
        self.show_result(result)

    def show_result(self, result):
        lines=[]
        for item in result['manifest']['tools']:
            lines.append(f"{item['tool']}: {item['status']} | {item['calculated']} calculated/accepted | {item['withheld']} withheld\n{item['note']}")
        self.result_text.configure(state='normal')
        self.result_text.delete('1.0','end')
        self.result_text.insert('end',result['manifest']['note']+'\n\n'+'\n\n'.join(lines)+'\n\n'+result['folder'])
        self.result_text.configure(state='disabled')

    def open_result(self):
        if self.result: os.startfile(self.result['folder'])

    def inspect_result(self):
        if self.busy: return
        from gradeguard_review_ui import open_review
        path=self.result['manifest'].get('review_file','') if self.result else None
        open_review(self.app,path,parent=self)


class AutoCalculateWindow(WorkWindow):
    def __init__(self, app):
        super().__init__(app,'Auto calculate | Curb, sidewalk and grade')
        self.geometry('850x670'); self.minsize(760,600)
        body=ttk.Frame(self,padding=20); body.pack(fill='both',expand=True)
        ttk.Label(body,text='Calculate the job together',font=('Segoe UI',19,'bold')).pack(anchor='w')
        ttk.Label(body,text='Use the project files and tool settings already selected, or set up a PDF-only sheet.',wraplength=780).pack(anchor='w',pady=(5,16))
        cad=ttk.LabelFrame(body,text='DXF + PDF + optional LandXML',padding=14); cad.pack(fill='x')
        self.snapshot=app._project_data()
        ttk.Label(cad,text='DXF: '+(Path(self.snapshot['cad_path']).name if self.snapshot['cad_path'] else 'Choose in Project files')+'\nPDF: '+(Path(self.snapshot['pdf_path']).name if self.snapshot['pdf_path'] else 'Choose in Project files'),wraplength=740).pack(anchor='w')
        row=ttk.Frame(cad); row.pack(fill='x',pady=8)
        self.enabled={}
        for key,label in [('curb','Curb'),('sidewalk','Sidewalk'),('grade','Grade grid')]:
            var=tk.BooleanVar(value=True); self.enabled[key]=var
            ttk.Checkbutton(row,text=label,variable=var).pack(side='left',padx=(0,22))
        s=self.snapshot
        ttk.Label(cad,text=f"Spacing: curb {s['spacing']} ft / sidewalk {s['sidewalk_spacing']} ft / grid {s['grade_grid_spacing']} ft\nStart: {s['start_point']} (continues across tools and offsets) · Grid: {s['grade_grid_type']}\nSurface interpretation: {s['surface_interpretation']} · Curb rise: {s['curb_height']} ft",wraplength=740).pack(anchor='w')
        ttk.Button(cad,text='Calculate selected tools',style='Accent.TButton',command=self.run_cad).pack(anchor='e',pady=(10,0))
        plan=ttk.LabelFrame(body,text='PDF only',padding=14); plan.pack(fill='x',pady=12)
        ttk.Label(plan,text='Calibrate one sheet, select or trace paths, and locate the controlling elevations. Calculate intermediate curb/sidewalk grades and a clipped grade grid. Save the setup for repeat runs.',wraplength=730).pack(anchor='w')
        ttk.Button(plan,text='Open PDF calculation workspace',command=self.open_pdf).pack(anchor='e',pady=(8,0))
        footer=ttk.Frame(body); footer.pack(side='bottom',fill='x',pady=(8,0))
        ttk.Button(footer,text='Open results folder',command=self.open_result).pack(side='right')
        ttk.Button(footer,text='Inspect points & profiles',command=self.inspect_result).pack(side='right',padx=5)
        ttk.Label(footer,textvariable=self.status,wraplength=590).pack(side='left',fill='x',expand=True)
        self.result_text=tk.Text(body,height=5,wrap='word',state='disabled',font=('Segoe UI',10))
        self.result_text.pack(fill='both',expand=True)

    def run_cad(self):
        enabled=[k for k,v in self.enabled.items() if v.get()]
        snapshot=copy.deepcopy(self.snapshot)
        self.task(lambda progress:calc.run_cad_package(snapshot,enabled,progress),self.finished)

    def open_pdf(self):
        if self.busy: return
        self.destroy()
        PdfCalculationWindow(self.app)


def sheet_paths(page):
    """Connected vector subpaths in rotated display coordinates; curves flattened.

    Bezier flatness tolerance is 0.10 PDF point, not a survey precision claim.
    The selected vector geometry is still a PDF-derived review product.
    """
    paths=[]; rotation=page.rotation_matrix
    def xy(p):
        q=p*rotation; return (q.x,q.y)
    def flatten(a,b,c,d,depth=0):
        def off(p):
            dx=d.x-a.x; dy=d.y-a.y
            return abs(dx*(a.y-p.y)-(a.x-p.x)*dy)/max(math.hypot(dx,dy),1e-10)
        if depth>=12 or (max(off(b),off(c))<.1 and (abs(b-a)+abs(c-b)+abs(d-c)-abs(d-a))<.2): return [d]
        ab=(a+b)/2; bc=(b+c)/2; cd=(c+d)/2; abc=(ab+bc)/2; bcd=(bc+cd)/2; mid=(abc+bcd)/2
        return flatten(a,ab,abc,mid,depth+1)+flatten(mid,bcd,cd,d,depth+1)
    for drawing in page.get_drawings():
        current=[]
        for item in drawing['items']:
            kind=item[0]; seq=[]
            if kind=='l': seq=[item[1],item[2]]
            elif kind=='c': seq=[item[1]]+flatten(*item[1:5])
            elif kind=='re':
                r=item[1]; seq=[r.tl,r.tr,r.br,r.bl,r.tl]
            elif kind=='qu':
                q=item[1]; seq=[q.ul,q.ur,q.lr,q.ll,q.ul]
            if not seq: continue
            seq=[xy(p) for p in seq]
            if current and calc.distance(current[-1],seq[0])>.01:
                if len(current)>1: paths.append(current)
                current=[]
            current.extend(seq[1:] if current else seq)
        if len(current)>1:
            if drawing.get('closePath') and calc.distance(current[0],current[-1])>.01: current.append(current[0])
            paths.append(current)
    return [[p for i,p in enumerate(path) if i==0 or calc.distance(p,path[i-1])>1e-9] for path in paths]


class PdfCalculationWindow(WorkWindow):
    def __init__(self, app):
        super().__init__(app,'PDF calculation workspace')
        self.geometry('1390x900'); self.minsize(1080,740)
        self.doc=None; self.paths=[]; self.controls=[]; self.boundary=[]; self.cal_points=[]; self.pending=[]
        self.observations=[]; self.vectors=[]; self.mode='inspect'; self.page_number=1; self.scale=1.; self.loaded_pdf=''; self.loaded_hash=''
        self.vars={}
        header=ttk.Frame(self,padding=(14,10)); header.pack(fill='x')
        ttk.Label(header,text='Follow these three steps',font=('Segoe UI',18,'bold')).pack(side='left')
        ttk.Button(header,text='Load setup',command=self.load_setup).pack(side='right',padx=4)
        ttk.Button(header,text='Save setup',command=self.save_setup).pack(side='right',padx=4)
        self.tabs=ttk.Notebook(self); self.tabs.pack(fill='both',expand=True,padx=12)
        self.setup=ttk.Frame(self.tabs,padding=16); self.sheet=ttk.Frame(self.tabs); self.results=ttk.Frame(self.tabs,padding=16)
        for frame,title in [(self.setup,'1  Sheet & scale'),(self.sheet,'2  Paths & grades'),(self.results,'3  Calculate & review')]: self.tabs.add(frame,text=title)
        self.setup_ui(); self.sheet_ui(); self.results_ui()
        footer=ttk.Frame(self,padding=10); footer.pack(fill='x')
        self.next_button=ttk.Button(footer,text='Next →',style='Accent.TButton',command=self.next_step)
        self.next_button.pack(side='right',padx=(10,0))
        ttk.Label(footer,textvariable=self.status,wraplength=900).pack(side='left',fill='x',expand=True)
        self.tabs.bind('<<NotebookTabChanged>>',self.update_next)
        self.vars['pdf'].set(app.pdf_var.get()); self.vars['output'].set(app.out_var.get())
        self.protocol('WM_DELETE_WINDOW',self.close)

    def value(self,key): return self.vars[key].get().strip()

    def entry(self,parent,key,label,default='',width=42):
        row=ttk.Frame(parent); row.pack(fill='x',pady=4)
        ttk.Label(row,text=label,width=22 if width<20 else 32).pack(side='left')
        self.vars[key]=tk.StringVar(value=default)
        widget=ttk.Entry(row,textvariable=self.vars[key],width=width); widget.pack(side='left',fill='x',expand=True)
        return row

    def setup_ui(self):
        ttk.Label(self.setup,text='Tell GradeGuard the size of this sheet',font=('Segoe UI',18,'bold')).pack(anchor='w',pady=(0,5))
        ttk.Label(self.setup,text='Use a printed distance you can identify on the plan. Pick both ends, then enter that distance in feet.',wraplength=1000).pack(anchor='w',pady=(0,12))
        row=self.entry(self.setup,'pdf','Plan PDF')
        ttk.Button(row,text='Browse',command=self.browse).pack(side='left',padx=6)
        row=self.entry(self.setup,'page','Sheet number (1-based)','1')
        ttk.Button(row,text='Load sheet',command=self.load_sheet).pack(side='left',padx=6)
        self.entry(self.setup,'output','Results folder',self.app.out_var.get())
        self.entry(self.setup,'reference','Scale and elevation reference notes')
        self.units=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.setup,text='I checked that dimensions and elevations use US survey feet',variable=self.units).pack(anchor='w',pady=7)
        row=ttk.Frame(self.setup); row.pack(fill='x',pady=8)
        ttk.Label(row,text='Positioning method',width=32).pack(side='left')
        self.vars['frame']=tk.StringVar(value='local')
        ttk.Radiobutton(row,text='Use a known distance',variable=self.vars['frame'],value='local').pack(side='left')
        ttk.Radiobutton(row,text='Use survey coordinates (advanced)',variable=self.vars['frame'],value='survey').pack(side='left',padx=12)
        fields=ttk.Frame(self.setup); fields.pack(fill='x')
        self.local_fields=ttk.Frame(fields); self.survey_fields=ttk.Frame(fields)
        self.entry(self.local_fields,'length','Known distance (ft)','')
        ttk.Label(self.local_fields,text='Positions will be relative to this baseline, not survey Easting / Northing.').pack(anchor='w',pady=4)
        self.entry(self.survey_fields,'survey1','Survey point 1: Easting, Northing','')
        self.entry(self.survey_fields,'survey2','Survey point 2: Easting, Northing','')
        self.entry(self.survey_fields,'survey3','Independent check: Easting, Northing','')
        self.entry(self.survey_fields,'tolerance','Survey check tolerance (ft)','')
        self.vars['frame'].trace_add('write',self.show_coordinate_fields); self.show_coordinate_fields()
        row=ttk.Frame(self.setup); row.pack(fill='x',pady=12)
        ttk.Button(row,text='Pick the known points on the plan',style='Accent.TButton',command=self.pick_calibration).pack(side='left')
        ttk.Button(row,text='Check scale',command=self.check_calibration).pack(side='left',padx=10)
        self.cal_status=tk.StringVar(value='No calibration points selected.')
        ttk.Label(self.setup,textvariable=self.cal_status,wraplength=1200).pack(anchor='w')
        ttk.Label(self.setup,text='Pick two known endpoints for a local scale, or two survey control points plus an independent third point away from the baseline. Each sheet has its own setup. Printed grades still need a verified meaning and a location at their leader/tick.',wraplength=1200).pack(anchor='w',pady=16)

    def sheet_ui(self):
        toolbar=ttk.Frame(self.sheet,padding=8); toolbar.pack(fill='x')
        self.kind=tk.StringVar(value='CURB')
        ttk.Combobox(toolbar,textvariable=self.kind,values=['CURB','SIDEWALK'],state='readonly',width=12).pack(side='left')
        for label,command in [('Select vector',lambda:self.set_mode('vector')),('Trace path',lambda:self.start_trace('trace')),('Grade boundary',lambda:self.start_trace('boundary')),('Undo vertex',self.undo),('Finish trace',self.finish_trace),('Cancel pick',self.cancel)]:
            ttk.Button(toolbar,text=label,command=command).pack(side='left',padx=2)
        ttk.Button(toolbar,text='−',command=lambda:self.zoom(.8)).pack(side='right')
        ttk.Button(toolbar,text='+',command=lambda:self.zoom(1.25)).pack(side='right')
        side_shell=ttk.Frame(self.sheet,width=380); side_shell.pack(side='right',fill='y'); side_shell.pack_propagate(False)
        from gradeguard_theme import BG
        self.side_canvas=tk.Canvas(side_shell,background=BG,highlightthickness=0)
        side_scroll=ttk.Scrollbar(side_shell,command=self.side_canvas.yview)
        self.side_canvas.configure(yscrollcommand=side_scroll.set)
        side_scroll.pack(side='right',fill='y'); self.side_canvas.pack(fill='both',expand=True)
        side=ttk.Frame(self.side_canvas,padding=8)
        side_window=self.side_canvas.create_window((0,0),window=side,anchor='nw')
        side.bind('<Configure>',lambda e:self.side_canvas.configure(scrollregion=self.side_canvas.bbox('all')))
        self.side_canvas.bind('<Configure>',lambda e:self.side_canvas.itemconfigure(side_window,width=e.width))
        ttk.Label(side,text='Read the printed grades',font=('Segoe UI',11,'bold')).pack(anchor='w')
        row=ttk.Frame(side); row.pack(fill='x',pady=4)
        ttk.Button(row,text='Read PDF text',command=self.scan).pack(side='left')
        ttk.Button(row,text='OCR this sheet',command=self.ocr).pack(side='left',padx=4)
        ttk.Button(row,text='Manual',command=self.manual_grade).pack(side='left')
        self.candidates=ttk.Treeview(side,columns=('label','z'),show='headings',height=4,selectmode='browse')
        for col,label,width in [('label','Printed label',160),('z','Grade',90)]: self.candidates.heading(col,text=label); self.candidates.column(col,width=width)
        self.candidates.pack(fill='x'); self.candidates.bind('<<TreeviewSelect>>',self.select_observation)
        self.entry(side,'z','Printed elevation','',10)
        self.meaning=tk.StringVar(value='')
        ttk.Label(side,text='Meaning from this plan legend').pack(anchor='w',pady=(6,0))
        ttk.Combobox(side,textvariable=self.meaning,values=list(GRADE_MEANINGS),state='readonly').pack(fill='x')
        self.entry(side,'rise','Local curb rise (FL only)','',8)
        self.role=tk.StringVar(value='Spot')
        ttk.Combobox(side,textvariable=self.role,values=['Spot','HP','LP'],state='readonly').pack(fill='x',pady=4)
        self.control_path=tk.StringVar(value='')
        self.path_combo=ttk.Combobox(side,textvariable=self.control_path,values=[],state='readonly')
        ttk.Label(side,text='Path for curb / sidewalk control').pack(anchor='w')
        self.path_combo.pack(fill='x')
        ttk.Button(side,text='Locate grade at leader / tick',command=self.locate_grade).pack(fill='x',pady=7)
        ttk.Label(side,text='Top/back: use grade directly. FL: add the entered local rise once. FG: grade grid only.',wraplength=320).pack(anchor='w')
        self.items=ttk.Treeview(side,columns=('detail',),show='tree headings',height=4,selectmode='browse')
        self.items.heading('#0',text='Selected item'); self.items.column('#0',width=100)
        self.items.heading('detail',text='Meaning / grade'); self.items.column('detail',width=170)
        ttk.Button(side,text='Remove selected item',command=self.remove_item).pack(side='bottom',fill='x')
        self.items.pack(fill='both',expand=True,pady=7)
        viewport=ttk.Frame(self.sheet); viewport.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(viewport,background='#0c1721',highlightthickness=0)
        xbar=ttk.Scrollbar(viewport,orient='horizontal',command=self.canvas.xview); ybar=ttk.Scrollbar(viewport,command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=xbar.set,yscrollcommand=ybar.set)
        xbar.pack(side='bottom',fill='x'); ybar.pack(side='right',fill='y'); self.canvas.pack(fill='both',expand=True)
        self.canvas.bind('<Button-1>',self.click)
        self.canvas.bind('<ButtonPress-2>',lambda e:self.canvas.scan_mark(e.x,e.y)); self.canvas.bind('<B2-Motion>',lambda e:self.canvas.scan_dragto(e.x,e.y,gain=1))

    def results_ui(self):
        ttk.Label(self.results,text='Calculate intermediate points',font=('Segoe UI',17,'bold')).pack(anchor='w',pady=(0,10))
        for key,label,default in [('spacing','Distance between path points (ft)','25'),('grid_spacing','Distance between grid points (ft)','25'),('depth','Depth below finished grade (ft)','0')]: self.entry(self.results,key,label,default)
        options=ttk.Frame(self.results)
        self.more_options=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.results,text='More calculation options',variable=self.more_options,command=lambda:options.pack(fill='x',after=options_toggle) if self.more_options.get() else options.pack_forget()).pack(anchor='w')
        options_toggle=self.results.winfo_children()[-1]
        self.entry(options,'association','Control-to-path tolerance (ft)','0.35')
        self.entry(options,'start','First point number','6000')
        self.grid_enabled=tk.BooleanVar(value=False)
        ttk.Checkbutton(self.results,text='Calculate grade grid inside the drawn boundary (at least 3 FG controls)',variable=self.grid_enabled).pack(anchor='w',pady=8)
        ttk.Label(self.results,text='Curb and sidewalk use station interpolation between controls, including HP/LP holds. Missing end controls leave gaps. A PDF spot-based grid is an inferred surface without design breaklines. These outputs are for review and are not approved field CSVs.',wraplength=1100).pack(anchor='w',pady=10)
        ttk.Button(self.results,text='Auto calculate this sheet',style='Accent.TButton',command=self.calculate).pack(anchor='w',pady=10)
        ttk.Button(self.results,text='Open results folder',command=self.open_result).pack(side='bottom',anchor='e',pady=8)
        ttk.Button(self.results,text='Inspect points & profiles',command=self.inspect_result).pack(side='bottom',anchor='e',pady=4)
        self.result_text=tk.Text(self.results,height=9,wrap='word',state='disabled',font=('Segoe UI',10)); self.result_text.pack(fill='both',expand=True)

    def browse(self):
        if self.busy: return
        path=filedialog.askopenfilename(parent=self,filetypes=[('Plan PDF','*.pdf')])
        if path: self.vars['pdf'].set(path)

    def load_sheet(self, preserve=False):
        if self.busy: return False
        try:
            path=self.value('pdf'); page=int(self.value('page')); digest=pdf.fingerprint(path)
            if not preserve and (self.paths or self.controls or self.cal_points or self.boundary):
                if not messagebox.askyesno('Replace sheet setup?','Loading a sheet clears the current paths, controls and calibration. Save the setup first if you need it.',parent=self): return False
            doc=fitz.open(path)
            if not 1<=page<=len(doc): doc.close(); raise ValueError('Sheet number is outside the PDF.')
            vectors=sheet_paths(doc[page-1])
            if self.doc: self.doc.close()
            self.doc=doc; self.loaded_pdf=path; self.loaded_hash=digest; self.page_number=page; self.vectors=vectors
            if not preserve: self.paths=[]; self.controls=[]; self.boundary=[]; self.cal_points=[]; self.pending=[]
            self.observations=[]; self.candidates.delete(*self.candidates.get_children())
            self.mode='inspect'; self.scale=min(1.5,1000/self.doc[page-1].rect.width)
            self.render(); self.refresh_items(); self.tabs.select(self.sheet)
            self.status.set(f'Sheet {page}: {len(vectors)} vector paths. Select linework or trace points; middle-drag pans.')
            return True
        except Exception as error: messagebox.showerror('Load sheet',str(error),parent=self); return False

    def render(self):
        if not self.doc: return
        pix=self.doc[self.page_number-1].get_pixmap(matrix=fitz.Matrix(self.scale,self.scale),alpha=False)
        self.photo=ImageTk.PhotoImage(Image.frombytes('RGB',(pix.width,pix.height),pix.samples))
        self.canvas.delete('all'); self.canvas.create_image(0,0,image=self.photo,anchor='nw',tags='page')
        self.canvas.configure(scrollregion=(0,0,pix.width,pix.height)); self.overlay()

    def zoom(self,factor):
        if not self.doc or self.busy: return
        rect=self.doc[self.page_number-1].rect
        target=max(.1,min(4,self.scale*factor))
        if rect.width*rect.height*target*target>18000000: self.status.set('Maximum sheet zoom reached.'); return
        self.scale=target; self.render()

    def overlay(self):
        self.canvas.delete('overlay')
        def line(points,color,closed=False):
            if len(points)<2: return
            pts=points+[points[0]] if closed else points
            self.canvas.create_line(*[v*self.scale for p in pts for v in p],fill=color,width=3,tags='overlay')
        def dot(p,label,color):
            x,y=[v*self.scale for v in p]
            self.canvas.create_oval(x-5,y-5,x+5,y+5,fill=color,outline='white',tags='overlay')
            self.canvas.create_text(x+7,y-9,text=label,anchor='w',fill=color,font=('Segoe UI',10,'bold'),tags='overlay')
        for path in self.paths:
            color='#ee7400' if path['kind']=='CURB' else '#0057cc'; line(path['points'],color); dot(path['points'][0],path['id'],color)
        line(self.boundary,'#9c239f',True); line(self.pending,'#df1747')
        for i,p in enumerate(self.cal_points): dot(p,'CAL '+str(i+1),'#df1747')
        for c in self.controls: dot(c['pdf'],c['id']+' '+str(c['z']),'#00844a')
        selected=self.candidates.selection()
        if selected and self.doc:
            row=self.observations[int(selected[0])]
            box=fitz.Rect(row.bbox)*self.doc[self.page_number-1].rotation_matrix
            self.canvas.create_rectangle(*[v*self.scale for v in box],outline='#df1747',width=3,tags='overlay')

    def set_mode(self,mode):
        if self.busy: return
        if not self.doc: messagebox.showinfo('Load a sheet','Load the plan sheet first.',parent=self); return
        self.mode=mode; self.tabs.select(self.sheet)
        instructions={'vector':'Click the relevant vector path. Use Trace path when linework is fragmented.', 'trace':'Click successive path vertices. Finish trace commits this path.', 'boundary':'Click boundary vertices, then Finish trace.', 'calibrate':'Click calibration points in order: first, second, then independent check for survey mode.', 'grade':'Click the physical leader/tick position for this elevation.'}
        self.status.set(instructions.get(mode,'Inspect the selected sheet.'))

    def start_trace(self,mode):
        if self.busy: return
        self.pending=[]; self.set_mode(mode); self.overlay()

    def cancel(self):
        if self.busy: return
        self.pending=[]; self.set_mode('inspect'); self.overlay()

    def undo(self):
        if self.busy: return
        if self.pending: self.pending.pop(); self.overlay()

    def pick_calibration(self):
        if self.busy: return
        if not self.doc and not self.load_sheet(): return
        self.cal_points=[]; self.set_mode('calibrate'); self.overlay()

    def click(self,event):
        if self.busy or not self.doc: return
        p=(self.canvas.canvasx(event.x)/self.scale,self.canvas.canvasy(event.y)/self.scale)
        rect=self.doc[self.page_number-1].rect
        if not rect.contains(fitz.Point(*p)): return
        if self.mode=='calibrate':
            self.cal_points.append(p)
            needed=2 if self.value('frame')=='local' else 3
            self.cal_status.set(f'{len(self.cal_points)} of {needed} calibration points selected.')
            if len(self.cal_points)>=needed: self.mode='inspect'; self.status.set('Calibration picks complete. Check calibration on Sheet & scale.'); self.tabs.select(self.setup)
        elif self.mode in ('trace','boundary'):
            if not self.pending or calc.distance(p,self.pending[-1])>1e-8: self.pending.append(p)
        elif self.mode=='vector':
            matches=[]
            for path in self.vectors:
                if len(path)<2: continue
                d,_,_=calc.project_to_path(p,path,calc.path_stations(path)); matches.append((d,path))
            if matches:
                d,path=min(matches,key=lambda v:v[0])
                if d*self.scale<=12: self.add_path(path)
                else: self.status.set('No vector within 12 screen pixels. Zoom in or use Trace path.')
        elif self.mode=='grade':
            self.controls.append(dict(self.pending_control,pdf=p)); self.mode='inspect'
            self.status.set('Grade located. Select the next printed grade or calculate the sheet.'); self.refresh_items()
        self.overlay()

    def add_path(self,points):
        prefix=self.kind.get(); ids={p['id'] for p in self.paths}; index=1
        while f'{prefix}-{index}' in ids: index+=1
        self.paths.append(dict(id=f'{prefix}-{index}',kind=prefix,points=list(points),source='Selected PDF vector' if self.mode=='vector' else 'Manually traced PDF'))
        self.control_path.set(f'{prefix}-{index}'); self.refresh_items(); self.status.set(f'Added {prefix}-{index}. Locate its controlling grades next.')

    def finish_trace(self):
        if self.busy: return
        minimum=3 if self.mode=='boundary' else 2
        if self.mode not in ('trace','boundary') or len(self.pending)<minimum: self.status.set(f'Pick at least {minimum} vertices first.'); return
        if self.mode=='boundary': self.boundary=list(self.pending); self.grid_enabled.set(True); self.status.set('Grade boundary saved. Locate at least three finished-grade controls inside it.')
        else: self.add_path(self.pending)
        self.pending=[]; self.mode='inspect'; self.refresh_items(); self.overlay()

    def scan(self):
        if not self.doc: return
        path=self.loaded_pdf; page=self.page_number
        self.task(lambda progress:([row for row in pdf.scan_pdf(path,progress=lambda n,total:progress(f'Reading sheet {n} of {total}...'))[0] if row.page==page]),self.scanned)

    def ocr(self):
        if not self.doc: return
        path=self.loaded_pdf; page=self.page_number
        self.task(lambda progress:pdf.ocr_page(path,page),self.scanned)

    def scanned(self, rows):
        self.observations=rows; self.candidates.delete(*self.candidates.get_children())
        for i,row in enumerate(rows): self.candidates.insert('', 'end',iid=str(i),values=(row.label,row.elevation))
        self.status.set(f'{len(rows)} candidate grades on this sheet. Verify each digit, legend meaning and leader location before using it.')

    def select_observation(self,event=None):
        if self.busy: return
        chosen=self.candidates.selection()
        if not chosen: return
        row=self.observations[int(chosen[0])]; self.vars['z'].set(str(row.elevation))
        self.role.set('HP' if 'HP' in row.label or 'HIGH' in row.label else 'LP' if 'LP' in row.label or 'LOW' in row.label else 'Spot')
        self.meaning.set('')
        self.status.set(row.raw+' | '+str(row.flags)); self.overlay()
        rect=fitz.Rect(row.bbox)*self.doc[self.page_number-1].rotation_matrix
        self.canvas.xview_moveto(max(0,(rect.x0*self.scale-self.canvas.winfo_width()/3)/(self.photo.width())))
        self.canvas.yview_moveto(max(0,(rect.y0*self.scale-self.canvas.winfo_height()/3)/(self.photo.height())))

    def locate_grade(self):
        if self.busy: return
        try:
            kind=GRADE_MEANINGS.get(self.meaning.get(),self.meaning.get())
            if not kind: raise ValueError('Choose the elevation meaning from the sheet legend.')
            path=self.control_path.get()
            if kind!='FG':
                matching=next((p for p in self.paths if p['id']==path),None)
                if not matching or matching['kind']!=('SIDEWALK' if kind=='SIDEWALK' else 'CURB'): raise ValueError('Select a matching curb or sidewalk path.')
            z=calc.number(self.value('z'),'Printed elevation')
            rise=calc.number(self.value('rise'),'Local curb rise',nonnegative=True) if kind=='CURB_FL' else 0
            selected=self.candidates.selection(); observation=self.observations[int(selected[0])] if selected else None
            ids={c['id'] for c in self.controls}; i=1
            while f'G{i}' in ids: i+=1
            self.pending_control=dict(id=f'G{i}',kind=kind,z=z,rise=rise,path_id=path if kind!='FG' else '',role=self.role.get(),page=self.page_number,
                printed=observation.raw if observation else 'Manual elevation',method=observation.method if observation else 'Manual',observation_id=observation.id if observation else '',
                pdf_bbox=list(observation.bbox) if observation else None)
            self.set_mode('grade')
        except Exception as error: messagebox.showerror('Locate grade',str(error),parent=self)

    def manual_grade(self):
        if self.busy: return
        self.candidates.selection_remove(*self.candidates.selection())
        self.vars['z'].set(''); self.meaning.set(''); self.role.set('Spot')
        self.status.set('Enter the missed printed grade, choose its meaning and locate its leader/tick.'); self.overlay()

    def refresh_items(self):
        self.items.delete(*self.items.get_children())
        for p in self.paths: self.items.insert('','end',iid='p:'+p['id'],text=p['id'],values=(str(len(p['points']))+' vertices',))
        for c in self.controls: self.items.insert('','end',iid='c:'+c['id'],text=c['id'],values=(f"{c['kind']} {c['z']} {c.get('role','Spot')}",))
        if self.boundary: self.items.insert('','end',iid='boundary',text='Boundary',values=(str(len(self.boundary))+' vertices',))
        self.path_combo.configure(values=[p['id'] for p in self.paths])

    def remove_item(self):
        if self.busy: return
        chosen=self.items.selection()
        if not chosen: return
        key=chosen[0]
        if key=='boundary': self.boundary=[]
        elif key.startswith('p:'):
            self.paths=[p for p in self.paths if p['id']!=key[2:]]
            self.controls=[c for c in self.controls if c.get('path_id')!=key[2:]]
        else: self.controls=[c for c in self.controls if c['id']!=key[2:]]
        self.refresh_items(); self.overlay()

    def calibration(self):
        pairs=[dict(pdf=p) for p in self.cal_points]
        if self.value('frame')=='survey':
            if len(pairs)!=3: raise ValueError('Pick two survey controls and one independent check on the sheet.')
            for i,pair in enumerate(pairs):
                values=self.value('survey'+str(i+1)).replace(',',' ').split()
                if len(values)!=2: raise ValueError('Enter Easting, Northing for each survey control.')
                pair['xy']=[calc.number(v,'Survey coordinate') for v in values]
        return dict(mode=self.value('frame'),points=pairs,length=self.value('length'),tolerance=self.value('tolerance'),units='US survey feet' if self.units.get() else '',reference=self.value('reference'))

    def check_calibration(self):
        try:
            model=calc.calibrate(self.calibration())
            self.cal_status.set(f"Scale: {model['scale']:.8f} ft / PDF point. Frame: {model['mode']}. Check residual: {model['check_residual']} ft.")
            return True
        except Exception as error: messagebox.showerror('Calibration',str(error),parent=self)
        return False

    def show_coordinate_fields(self,*args):
        self.local_fields.pack_forget(); self.survey_fields.pack_forget()
        (self.survey_fields if self.value('frame')=='survey' else self.local_fields).pack(fill='x')

    def update_next(self,event=None):
        selected=self.tabs.select()
        self.next_button.configure(text='Next: paths & grades →' if selected==str(self.setup) else 'Next: calculate →' if selected==str(self.sheet) else 'Review points →' if self.result else 'Calculate →')

    def next_step(self):
        if self.busy: return
        selected=self.tabs.select()
        if selected==str(self.setup):
            if not self.doc:
                if self.load_sheet(): self.pick_calibration()
                return
            if self.check_calibration():
                self.tabs.select(self.sheet); self.status.set('Select a curb or sidewalk path, then read and locate its printed grades.')
        elif selected==str(self.sheet):
            if not self.check_calibration(): self.tabs.select(self.setup); return
            if not self.paths and not self.boundary:
                self.status.set('Select or trace a curb / sidewalk path, or draw a grade boundary first.'); return
            if not self.controls:
                self.status.set('Read PDF text or enter a grade manually, choose its meaning, then locate its leader / tick.'); return
            self.tabs.select(self.results)
        elif self.result: self.inspect_result()
        else: self.calculate()

    def job(self):
        if not self.doc: raise ValueError('Load the plan sheet first.')
        if self.value('pdf')!=self.loaded_pdf or int(self.value('page'))!=self.page_number: raise ValueError('Load the selected PDF/sheet before calculating.')
        if pdf.fingerprint(self.loaded_pdf)!=self.loaded_hash: raise ValueError('The PDF changed. Reload it and review the setup.')
        return dict(schema='gradeguard.pdf-calculation.v1',pdf_path=self.loaded_pdf,pdf_sha256=self.loaded_hash,page=self.page_number,
            calibration=self.calibration(),paths=copy.deepcopy(self.paths),controls=copy.deepcopy(self.controls),boundary=copy.deepcopy(self.boundary),
            spacing=self.value('spacing'),grid_spacing=self.value('grid_spacing'),association_tolerance=self.value('association'),start_point=self.value('start'),subgrade_depth=self.value('depth'),calculate_grid=self.grid_enabled.get(),output=self.value('output'))

    def save_setup(self):
        if self.busy: return
        try:
            job=self.job()
            path=filedialog.asksaveasfilename(parent=self,defaultextension='.json',initialfile='PDF-Calculation-Setup.json',filetypes=[('PDF calculation setup','*.json')])
            if path: Path(path).write_text(json.dumps(job,indent=2,allow_nan=False),encoding='utf-8'); self.status.set('Saved '+path)
        except Exception as error: messagebox.showerror('Save setup',str(error),parent=self)

    def load_setup(self):
        if self.busy: return
        path=filedialog.askopenfilename(parent=self,filetypes=[('PDF calculation setup','*.json')])
        if not path: return
        try:
            job=json.loads(Path(path).read_text(encoding='utf-8'))
            if job['schema']!='gradeguard.pdf-calculation.v1': raise ValueError('This is not a PDF calculation setup.')
            if pdf.fingerprint(job['pdf_path'])!=job['pdf_sha256']: raise ValueError('The PDF no longer matches this setup.')
            if (self.paths or self.controls) and not messagebox.askyesno('Replace setup?','Replace the current paths and grades with this saved setup?',parent=self): return
            self.vars['pdf'].set(job['pdf_path']); self.vars['page'].set(str(job['page']))
            if not self.load_sheet(preserve=True): return
            spec=job['calibration']; self.vars['frame'].set(spec['mode']); self.vars['reference'].set(spec['reference']); self.units.set(spec['units']=='US survey feet')
            self.vars['length'].set(str(spec.get('length',''))); self.vars['tolerance'].set(str(spec.get('tolerance','')))
            self.cal_points=[p['pdf'] for p in spec['points']]
            for i,p in enumerate(spec['points'][:3]): self.vars['survey'+str(i+1)].set(', '.join(map(str,p.get('xy',[]))))
            self.paths=job['paths']; self.controls=job['controls']; self.boundary=job['boundary']; self.pending=[]
            for key,field in [('spacing','spacing'),('grid_spacing','grid_spacing'),('association','association_tolerance'),('start','start_point'),('depth','subgrade_depth'),('output','output')]: self.vars[key].set(str(job.get(field,'')))
            self.grid_enabled.set(job['calculate_grid']); self.cal_status.set(f'{len(self.cal_points)} saved calibration points.'); self.refresh_items(); self.overlay()
            self.status.set('Loaded saved sheet setup. Review the overlay before calculating.')
        except Exception as error: messagebox.showerror('Load setup',str(error),parent=self)

    def calculate(self):
        if self.busy: return
        try:
            job=self.job(); calc.calibrate(job['calibration']); output=self.value('output')
            self.task(lambda progress:calc.calculate_pdf_job(job,output,progress),self.finished)
        except Exception as error: messagebox.showerror('Calculate',str(error),parent=self)

    def show_result(self,result):
        super().show_result(result); self.tabs.select(self.results); self.update_next()

    def close(self):
        if self.busy: return
        if self.doc: self.doc.close()
        super().close()
