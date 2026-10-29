"""Linked plan, profile and provenance inspector; never edits design values."""
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox

import fitz
from PIL import Image,ImageTk
from gradeguard_pdf import fingerprint,render_evidence
from gradeguard_review import load_review,profile_edges,finite
from gradeguard_theme import BG,PANEL,INPUT,TEXT,MUTED,GREEN,GOLD,GOOD,ERROR,BORDER


def open_review(app,path=None,parent=None):
    if not path:
        path=getattr(app,'last_calculation_review','')
    if not path or not Path(path).is_file():
        path=filedialog.askopenfilename(parent=parent or app,title='Open Calculation-Review.json',
            initialdir=app.out_var.get() or '.',filetypes=[('Calculation review','*.json')])
    if not path: return None
    try: return CalculationReviewWindow(app,path,parent)
    except Exception as error:
        messagebox.showerror('Open calculation review',str(error),parent=parent or app)
        return None


class CalculationReviewWindow(tk.Toplevel):
    def __init__(self,app,path,parent=None):
        model=load_review(path)
        super().__init__(parent or app)
        self.app=app; self.model=model; self.file_path=str(path); self.owner=parent or app
        self.title('GradeGuard | Point calculations & grade profiles')
        self.geometry('1390x920'); self.minsize(1100,760); self.configure(background=BG)
        self.transient(self.owner); self.grab_set(); self.protocol('WM_DELETE_WINDOW',self.close)
        self.points={p['id']:p for p in model['points']}; self.paths={p['id']:p for p in model['paths']}
        self.selected=None; self.zoom=1.; self.centre=None; self.pdf_doc=None; self.pdf_photo=None; self.pdf_cache=None
        self.plan_hits=[]; self.profile_hits=[]; self.edges=[]; self.render_id=None; self.visible=[]; self.syncing=False
        self.path_var=tk.StringVar(value='All points'); self.find_var=tk.StringVar(); self.source_status=[]
        self._verify_sources(); self._build(); self.populate()
        self.bind('<Destroy>',self.on_destroy,add=True)

    def _verify_sources(self):
        for path,digest in self.model.get('sources',{}).items():
            try:
                if fingerprint(path)!=digest: self.source_status.append('Changed: '+Path(path).name)
            except OSError: self.source_status.append('Missing: '+Path(path).name)
        pdf=self.model.get('pdf_path','')
        self.pdf_verified=False
        if pdf and Path(pdf).is_file() and self.model.get('pdf_sha256'):
            self.pdf_verified=fingerprint(pdf)==self.model['pdf_sha256']
        if self.model['workflow']=='PDF only' and self.pdf_verified:
            self.pdf_doc=fitz.open(pdf)

    def _build(self):
        header=ttk.Frame(self,padding=(16,12),style='Header.TFrame'); header.pack(fill='x')
        ttk.Label(header,text='Point calculations & grade profiles',style='Title.TLabel').pack(anchor='w')
        ttk.Label(header,text=self.model['note'],style='Header.TLabel',wraplength=1320).pack(anchor='w',pady=(5,0))
        controls=ttk.Frame(self,padding=(12,8)); controls.pack(fill='x')
        ttk.Label(controls,text='Path').pack(side='left')
        self.path_combo=ttk.Combobox(controls,textvariable=self.path_var,values=['All points']+list(self.paths),state='readonly',width=30)
        self.path_combo.pack(side='left',padx=8); self.path_combo.bind('<<ComboboxSelected>>',lambda e:self.change_path())
        ttk.Label(controls,text='Find point').pack(side='left',padx=(10,3))
        ttk.Entry(controls,textvariable=self.find_var,width=14).pack(side='left')
        ttk.Button(controls,text='Find',command=self.find_point).pack(side='left',padx=5)
        ttk.Button(controls,text='Fit plan',command=self.fit).pack(side='right',padx=3)
        ttk.Button(controls,text='Zoom to point',command=self.focus_point).pack(side='right',padx=3)
        self.status=tk.StringVar(value='Click a point on the plan, profile or list. '+(' | '.join(self.source_status) if self.source_status else 'Source fingerprints match this saved run.'))
        ttk.Label(self,textvariable=self.status,foreground=GOLD if self.source_status else MUTED,wraplength=1320,padding=10).pack(side='bottom',fill='x')
        panes=ttk.Panedwindow(self,orient='horizontal'); panes.pack(fill='both',expand=True,padx=12,pady=(0,6))
        left=ttk.Frame(panes); right=ttk.Frame(panes,padding=(10,0,0,0)); panes.add(left,weight=3); panes.add(right,weight=2)
        left.grid_rowconfigure(0,weight=3); left.grid_rowconfigure(1,weight=2); left.grid_columnconfigure(0,weight=1)
        plan_box=ttk.LabelFrame(left,text='Plan — select a calculated point',padding=6); plan_box.grid(row=0,column=0,sticky='nsew')
        self.plan=tk.Canvas(plan_box,background=PANEL,highlightthickness=0,height=360); self.plan.pack(fill='both',expand=True)
        profile_box=ttk.LabelFrame(left,text='Grade profile — station in feet / elevation in feet',padding=6); profile_box.grid(row=1,column=0,sticky='nsew',pady=(8,0))
        self.profile=tk.Canvas(profile_box,background=PANEL,highlightthickness=0,height=230); self.profile.pack(fill='both',expand=True)
        for canvas in (self.plan,self.profile): canvas.bind('<Configure>',lambda e:self.schedule_render())
        self.plan.bind('<Button-1>',lambda e:self.pick(e,self.plan_hits)); self.profile.bind('<Button-1>',lambda e:self.pick(e,self.profile_hits))
        self.plan.bind('<MouseWheel>',self.wheel)
        columns=('point','station','z','status')
        table=ttk.Frame(right); table.pack(fill='x')
        self.table=ttk.Treeview(table,columns=columns,show='headings',height=7,selectmode='browse')
        for key,label,width in [('point','Point',65),('station','Station',85),('z','Grade',90),('status','State',155)]:
            self.table.heading(key,text=label); self.table.column(key,width=width,stretch=key=='status')
        scroll=ttk.Scrollbar(table,command=self.table.yview); self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); self.table.pack(fill='x',expand=True)
        self.table.tag_configure('missing',foreground=ERROR); self.table.tag_configure('review',foreground=GOLD)
        self.table.bind('<<TreeviewSelect>>',self.table_selected)
        evidence=ttk.Frame(right); evidence.pack(fill='x',pady=8)
        self.evidence_var=tk.StringVar(value='')
        self.evidence_combo=ttk.Combobox(evidence,textvariable=self.evidence_var,state='readonly',width=18)
        self.evidence_combo.pack(side='left',fill='x',expand=True)
        self.evidence_button=ttk.Button(evidence,text='View PDF evidence',command=self.show_evidence,state='disabled'); self.evidence_button.pack(side='right',padx=(5,0))
        self.details=tk.Text(right,wrap='word',background=PANEL,foreground=TEXT,insertbackground=TEXT,padx=12,pady=10,relief='flat',font=('Segoe UI',10),state='disabled',width=47)
        ds=ttk.Scrollbar(right,command=self.details.yview); self.details.configure(yscrollcommand=ds.set)
        ds.pack(side='right',fill='y'); self.details.pack(fill='both',expand=True)
        self.write_details('Select a point to inspect its calculation.\n\nGreen: accepted by the existing field-export gate.\nGold: calculated for review or withheld.\nRed: no recorded elevation.\n\nProfiles do not connect across missing grades or disconnected runs.')

    def write_details(self,text):
        self.details.configure(state='normal'); self.details.delete('1.0','end'); self.details.insert('end',text); self.details.configure(state='disabled')

    def populate(self):
        current=self.path_var.get()
        self.visible=[p for p in self.points.values() if current=='All points' or p['path']==current]
        self.syncing=True; self.table.delete(*self.table.get_children())
        for p in self.visible:
            tag='missing' if p['z'] is None else '' if p['approved'] else 'review'
            self.table.insert('','end',iid=p['id'],values=(p['number'],'' if p['station'] is None else f"{p['station']:.3f}", 'MISSING' if p['z'] is None else f"{p['z']:.4f}",p['status']),tags=(tag,))
        self.syncing=False; self.schedule_render()

    def change_path(self):
        self.selected=None; self.evidence_button.configure(state='disabled'); self.write_details('Select a point on this path.'); self.populate()

    def select_point(self,ident):
        p=self.points[ident]; self.selected=ident
        target=p['path'] if p['path'] in self.paths else 'All points'
        if target!=self.path_var.get(): self.path_var.set(target); self.populate()
        self.syncing=True; self.table.selection_set(ident); self.table.see(ident); self.syncing=False
        self.display_details(p); self.schedule_render()

    def table_selected(self,event=None):
        if self.syncing: return
        selected=self.table.selection()
        if selected and selected[0]!=self.selected: self.select_point(selected[0])

    def find_point(self):
        target=self.find_var.get().strip()
        matches=[p['id'] for p in self.points.values() if str(p['number'])==target or p['id']==target]
        if len(matches)==1: self.select_point(matches[0]); self.focus_point()
        else: self.status.set('Point not found.' if not matches else 'More than one tool uses that point number; select its row.')

    def display_details(self,p):
        detail=p.get('detail',{}); grade='MISSING — not zero' if p['z'] is None else f"{p['z']:.6f} ft"
        x_label,y_label=('Local X','Local Y') if self.model['coordinate_frame']=='local' else ('Easting','Northing')
        lines=[f"POINT {p['number']} · {p['tool']}",f"Calculated grade: {grade}",f"State: {p['status']}",
               f"{x_label}: {p['x']:.6f}   {y_label}: {p['y']:.6f}",f"Units: {self.model['units']}"]
        if p['station'] is not None: lines.append(f"Path: {p['path']}   Station: {p['station']:.6f} ft")
        lines+=['',detail.get('method','Recorded calculation'),detail.get('formula','No intermediate formula was recorded.')]
        if p['path'] in self.paths:
            path=self.paths[p['path']]; peers=[r for r in self.points.values() if r['path']==p['path']]
            length=sum(math.dist(a,b) for a,b in zip(path['points'],path['points'][1:])) if path.get('closed') else None
            outgoing=next((e for e in profile_edges(peers,length) if e['a']==p['id']),None)
            if outgoing:
                following=self.points[outgoing['b']]
                flow='Flat between these points.' if outgoing['flow']=='flat' else f"Falls toward point {self.points[outgoing['flow']]['number']}."
                lines+=['',f"To point {following['number']}: {outgoing['length']:.6f} ft; signed slope {outgoing['slope']:.6f}%.",flow,
                        'Slope = 100 × (next grade - this grade) / station distance.']
            else: lines+=['','No outgoing slope: path end, missing grade or duplicate/ambiguous station.']
            lines+=['',path.get('station_note','')]
        self.controls=detail.get('controls',[])
        for c in self.controls:
            lines+=['',f"Control {c.get('id','')} · {c.get('role','Spot')}",c.get('printed',''),c.get('conversion','')]
            if 'station' in c: lines.append(f"Control station: {c['station']:.6f} ft")
        if detail.get('triangle'):
            lines+=['','Controlling triangle vertices (X, Y, Z):']+['  '+', '.join(f'{v:.6f}' for v in vertex) for vertex in detail['triangle']]
        lines+=['','RECORDED SOURCE',p.get('source','') or 'No source record.']
        if p.get('issue'): lines+=['','QA NOTES',p['issue']]
        if self.source_status: lines+=['','SOURCE CHANGES — historical results only']+self.source_status
        self.write_details('\n'.join(lines))
        labels=[f"{i+1}: {c.get('id','Control')}" for i,c in enumerate(self.controls)]
        self.evidence_combo.configure(values=labels); self.evidence_var.set(labels[0] if labels else '')
        self.evidence_button.configure(state='normal' if labels and self.pdf_verified else 'disabled')

    def schedule_render(self):
        if self.render_id is None: self.render_id=self.after_idle(self.render)

    def render(self):
        self.render_id=None
        if not self.winfo_exists(): return
        self.draw_plan(); self.draw_profile()

    def color(self,p): return ERROR if p['z'] is None else GOOD if p['approved'] else GOLD

    def draw_plan(self):
        c=self.plan; c.delete('all'); self.plan_hits=[]; w,h=max(c.winfo_width(),1),max(c.winfo_height(),1)
        if w<10 or h<10 or not self.points: return
        is_pdf=self.pdf_doc is not None
        def xy(p): return tuple(p['pdf']) if is_pdf else (p['x'],-p['y'])
        coords=[xy(p) for p in self.points.values()]
        if is_pdf:
            page=self.pdf_doc[self.model['pdf_page']-1]; lo=(0,0); hi=(page.rect.width,page.rect.height)
        else: lo=(min(p[0] for p in coords),min(p[1] for p in coords)); hi=(max(p[0] for p in coords),max(p[1] for p in coords))
        base=min((w-40)/max(hi[0]-lo[0],1),(h-40)/max(hi[1]-lo[1],1)); scale=base*self.zoom
        centre=self.centre or ((lo[0]+hi[0])/2,(lo[1]+hi[1])/2)
        def screen(p): return ((p[0]-centre[0])*scale+w/2,(p[1]-centre[1])*scale+h/2)
        if is_pdf:
            # Bound raster memory independently of the zoom selection.
            raster_scale=min(scale,math.sqrt(15_000_000/(hi[0]*hi[1])))
            if self.pdf_cache!=(w,h,round(scale,7)):
                pix=page.get_pixmap(matrix=fitz.Matrix(raster_scale,raster_scale),alpha=False)
                im=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
                if raster_scale<scale:
                    # At the memory bound retain a lower zoom rather than misalign overlays.
                    self.zoom=raster_scale/base; scale=raster_scale
                self.pdf_photo=ImageTk.PhotoImage(im); self.pdf_cache=(w,h,round(scale,7))
            c.create_image(*screen((0,0)),anchor='nw',image=self.pdf_photo)
        for path in self.paths.values():
            vertices=path.get('pdf',[]) if is_pdf else [(p[0],-p[1]) for p in path['points']]
            if len(vertices)>1:
                c.create_line(*[v for p in vertices for v in screen(p)],fill='#647d73',width=2 if path['id']==self.path_var.get() else 1)
        for p in self.points.values():
            sx,sy=screen(xy(p))
            if not (-10<sx<w+10 and -10<sy<h+10): continue
            self.plan_hits.append((sx,sy,p['id']))
            selected=p['id']==self.selected; r=6 if selected else 3
            c.create_oval(sx-r,sy-r,sx+r,sy+r,fill=self.color(p),outline='#ffffff' if selected else PANEL,width=2 if selected else 1)
            if selected:
                c.create_text(min(max(sx+12,50),w-70),max(14,sy-14),text=f"Point {p['number']}",anchor='w',fill='#14212b' if is_pdf else TEXT,font=('Segoe UI',11,'bold'))
        if not is_pdf: c.create_text(10,10,anchor='nw',text='Local XY' if self.model['coordinate_frame']=='local' else 'Survey XY',fill=MUTED)

    def draw_profile(self):
        c=self.profile; c.delete('all'); self.profile_hits=[]; self.edges=[]
        w,h=max(c.winfo_width(),1),max(c.winfo_height(),1); name=self.path_var.get()
        if name not in self.paths:
            c.create_text(w/2,h/2,text='Select a curb or sidewalk path to view its profile.\nGrid points have no connected station profile.',fill=MUTED,justify='center'); return
        points=sorted([p for p in self.visible if finite(p['station'])],key=lambda p:p['station'])
        if not points: c.create_text(w/2,h/2,text='No points can be assigned unambiguously to this path.',fill=MUTED); return
        path=self.paths[name]; length=sum(math.dist(a,b) for a,b in zip(path['points'],path['points'][1:]))
        lo=min(p['station'] for p in points); hi=length if path.get('closed') else max(p['station'] for p in points)
        values=[p['z'] for p in points if finite(p['z'])]; zlo=min(values) if values else 0; zhi=max(values) if values else 1
        pad=max((zhi-zlo)*.12,.05); zlo-=pad; zhi+=pad
        x0,y0,x1,y1=68,24,max(110,w-22),max(80,h-58)
        def screen(station,z): return (x0+(station-lo)/max(hi-lo,1e-8)*(x1-x0),y1-(z-zlo)/(zhi-zlo)*(y1-y0))
        for i in range(5):
            z=zlo+(zhi-zlo)*i/4; y=screen(lo,z)[1]
            c.create_line(x0,y,x1,y,fill=BORDER)
            c.create_text(x0-8,y,text=f'{z:.3f}' if values else '—',anchor='e',fill=MUTED,font=('Segoe UI',9))
            s=lo+(hi-lo)*i/4; x=screen(s,zlo)[0]
            c.create_text(x,y1+23,text=f'{s:.2f}',fill=MUTED,font=('Segoe UI',9))
        self.edges=profile_edges(points,length if path.get('closed') else None)
        for edge in self.edges:
            a,b=self.points[edge['a']],self.points[edge['b']]
            end_station=length if b['station']<a['station'] else b['station']
            pa,pb=screen(a['station'],a['z']),screen(end_station,b['z'])
            # Arrow follows falling elevation; a flat segment has no direction arrow.
            arrow='none' if edge['flow']=='flat' else 'first' if edge['flow']==a['id'] else 'last'
            c.create_line(*pa,*pb,fill=GREEN,width=2,arrow=arrow,arrowshape=(8,9,4))
            if a['id']==self.selected:
                c.create_text((pa[0]+pb[0])/2,(pa[1]+pb[1])/2-14,text=f"{edge['slope']:+.3f}%",fill=GOLD,font=('Segoe UI',10,'bold'))
            if end_station==length and b['station']<a['station']:
                c.create_oval(pb[0]-4,pb[1]-4,pb[0]+4,pb[1]+4,outline=GOLD)
                self.profile_hits.append((*pb,b['id']))
        for p in points:
            x,y=screen(p['station'],p['z']) if p['z'] is not None else (screen(p['station'],zlo)[0],y1+8)
            self.profile_hits.append((x,y,p['id'])); r=6 if p['id']==self.selected else 4
            if p['z'] is None:
                c.create_line(x-r,y-r,x+r,y+r,fill=ERROR,width=2); c.create_line(x-r,y+r,x+r,y-r,fill=ERROR,width=2)
            else: c.create_oval(x-r,y-r,x+r,y+r,fill=self.color(p),outline=TEXT if p['id']==self.selected else PANEL,width=2)
        c.create_text(x0,h-13,anchor='w',text='Arrows follow lower grade · red × = missing · vertical scale is exaggerated',fill=MUTED,font=('Segoe UI',9))

    def pick(self,event,hits):
        if not hits: return
        nearest=min(hits,key=lambda p:(p[0]-event.x)**2+(p[1]-event.y)**2)
        if math.hypot(nearest[0]-event.x,nearest[1]-event.y)<=16: self.select_point(nearest[2])

    def fit(self): self.zoom=1.; self.centre=None; self.pdf_cache=None; self.schedule_render()

    def focus_point(self):
        if self.selected:
            p=self.points[self.selected]; self.centre=tuple(p['pdf']) if self.pdf_doc else (p['x'],-p['y']); self.zoom=3.; self.pdf_cache=None; self.schedule_render()

    def wheel(self,event):
        self.zoom=max(.5,min(8,self.zoom*(1.25 if event.delta>0 else .8))); self.pdf_cache=None; self.schedule_render(); return 'break'

    def show_evidence(self):
        try:
            pdf=self.model['pdf_path']
            if not self.pdf_verified or fingerprint(pdf)!=self.model['pdf_sha256']: raise ValueError('The PDF changed or is missing. Restore the original source before viewing its evidence.')
            index=int(self.evidence_var.get().split(':',1)[0])-1; control=self.controls[index]
            page_number=control.get('page',self.model.get('pdf_page',1)); box=control.get('pdf_bbox')
            if not box and control.get('pdf'):
                with fitz.open(pdf) as doc:
                    tick=fitz.Point(*control['pdf'])*doc[page_number-1].derotation_matrix
                    box=(tick.x-15,tick.y-15,tick.x+15,tick.y+15)
            png,highlight=render_evidence(pdf,page_number,box,width=1000)
            import io
            im=Image.open(io.BytesIO(png)); im.thumbnail((1000,650))
            window=tk.Toplevel(self); window.title(f"PDF evidence | {control.get('id','Control')} | sheet {page_number}"); window.transient(self)
            ttk.Label(window,text=control.get('printed','Located plan control'),wraplength=1000,padding=10).pack(fill='x')
            window.photo=ImageTk.PhotoImage(im); ttk.Label(window,image=window.photo).pack()
            ttk.Label(window,text='Original plan evidence; no PDF content or grade has been changed.',padding=10).pack()
        except Exception as error: messagebox.showerror('PDF evidence',str(error),parent=self)

    def on_destroy(self,event):
        if event.widget==self and self.render_id is not None:
            self.after_cancel(self.render_id); self.render_id=None

    def close(self):
        if self.pdf_doc: self.pdf_doc.close(); self.pdf_doc=None
        self.destroy()
        if self.owner.winfo_exists() and self.owner!=self.app: self.owner.grab_set()
