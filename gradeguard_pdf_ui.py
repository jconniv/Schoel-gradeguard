"""Review PDF evidence and key confirmed CAD curb controls without registration guesses."""
import base64
import json
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import uuid

from gradeguard_pdf import (PdfGrade, scan_pdf, ocr_page, render_evidence, reviewed_control,
                           save_reviewed_data, validate_reviewed_data, export_review, fingerprint)


class PdfReviewWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.pdf_path = app.pdf_var.get().strip()
        self.cad_path = app.cad_var.get().strip()
        self.cad_hash = fingerprint(self.cad_path) if Path(self.cad_path).is_file() else None
        self.rows, self.spots, self.info = [], {}, {}
        self.selected = None
        self.events = queue.Queue()
        self.busy = False
        self.dirty = False
        self.title('GradeGuard | Read PDF grades')
        self.geometry('1280x880')
        self.minsize(1050, 740)
        self.transient(app)
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='Reading all PDF sheets...')
        self.page = tk.IntVar(value=1)
        self.filter_page = tk.BooleanVar(value=False)
        self.filter_kind = tk.StringVar(value='All callouts')
        self.search = tk.StringVar()
        self.whole_sheet = tk.BooleanVar(value=False)
        self.zoom = 1.0
        self.fields = {key: tk.StringVar() for key in ('label','elevation','easting','northing','meaning','role','height','reference')}
        self.confirmed = tk.BooleanVar(value=False)
        self._build()
        self.grab_set()
        self.start_work(lambda: scan_pdf(self.pdf_path), 'scan')

    def _build(self):
        header = ttk.Frame(self, padding=10)
        header.pack(fill='x')
        ttk.Label(header, text='Read the number. Verify the meaning. Hold the actual curb point.',
                  font=('Segoe UI', 13, 'bold')).pack(anchor='w')
        ttk.Label(header, text=Path(self.pdf_path).name).pack(anchor='w', pady=(4,0))
        ttk.Label(header, text='Detections include possible legends and existing grades. Nothing is approved automatically.').pack(anchor='w')
        toolbar = ttk.Frame(self, padding=(10,0,10,8))
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='PDF page').pack(side='left')
        self.page_spin = ttk.Spinbox(toolbar, from_=1, to=1, textvariable=self.page, width=5, command=self.change_page)
        self.page_spin.pack(side='left', padx=5)
        self.page_spin.bind('<Return>', lambda e: self.change_page())
        ttk.Button(toolbar, text='Show page', command=self.change_page).pack(side='left')
        ttk.Checkbutton(toolbar, text='List this page only', variable=self.filter_page, command=self.populate).pack(side='left', padx=8)
        self.ocr_button = ttk.Button(toolbar, text='OCR this page', command=self.ocr)
        self.ocr_button.pack(side='left', padx=4)
        ttk.Button(toolbar, text='Key a missed callout', command=self.manual).pack(side='left', padx=4)
        ttk.Button(toolbar, text='Export review CSV', command=self.export).pack(side='right')
        filters = ttk.Frame(self, padding=(10,0,10,5))
        filters.pack(fill='x')
        ttk.Label(filters,text='Find').pack(side='left')
        search = ttk.Entry(filters,textvariable=self.search,width=32)
        search.pack(side='left',padx=6)
        kinds = ttk.Combobox(filters,textvariable=self.filter_kind,state='readonly',width=19,
                            values=('All callouts','High / low points','Curb labels','Accepted controls'))
        kinds.pack(side='left',padx=6)
        self.search.trace_add('write',lambda *_:self.populate())
        self.filter_kind.trace_add('write',lambda *_:self.populate())

        footer = ttk.Frame(self, padding=10)
        footer.pack(side='bottom', fill='x')
        ttk.Label(footer, textvariable=self.status, wraplength=900).pack(side='left', fill='x', expand=True)
        self.save_button = ttk.Button(footer, text='Save reviewed curb controls', command=self.save)
        self.save_button.pack(side='right')

        editor = ttk.LabelFrame(self, text='Selected callout -> confirmed top/back-of-curb control', padding=8)
        editor.pack(side='bottom', fill='x', padx=10, pady=4)
        definitions = [('Printed label','label'),('Printed elevation','elevation'),('CAD Easting','easting'),('CAD Northing','northing')]
        for column, (title, key) in enumerate(definitions):
            ttk.Label(editor, text=title).grid(row=0, column=column, sticky='w', padx=5)
            ttk.Entry(editor, textvariable=self.fields[key], width=23).grid(row=1, column=column, sticky='ew', padx=5)
            editor.columnconfigure(column, weight=1)
        ttk.Label(editor, text='Verified elevation meaning').grid(row=2, column=0, sticky='w', padx=5, pady=(6,0))
        ttk.Combobox(editor, textvariable=self.fields['meaning'], values=('Top/back of curb','Flowline/gutter/bottom of curb'),
                     state='readonly', width=33).grid(row=3, column=0, sticky='ew', padx=5)
        ttk.Label(editor, text='Verified curb point type').grid(row=2, column=1, sticky='w', padx=5)
        ttk.Combobox(editor, textvariable=self.fields['role'], values=('Spot','High point','Low point'), state='readonly').grid(row=3,column=1,sticky='ew',padx=5)
        ttk.Label(editor, text='Local rise to curb top (ft)').grid(row=2,column=2,sticky='w',padx=5)
        ttk.Entry(editor,textvariable=self.fields['height']).grid(row=3,column=2,sticky='ew',padx=5)
        ttk.Label(editor, text='Top elevations receive no addition.\nFor lower grades, enter the local rise.').grid(row=3,column=3,sticky='w',padx=5)
        ttk.Label(editor,text='Legend/detail sheet and benchmark/vertical reference checked').grid(row=4,column=0,columnspan=4,sticky='w',padx=5,pady=(6,0))
        ttk.Entry(editor,textvariable=self.fields['reference']).grid(row=5,column=0,columnspan=4,sticky='ew',padx=5)
        ttk.Checkbutton(editor, text='I verified the value, design meaning, actual CAD curb tick, US survey feet and matching vertical reference.',
                        variable=self.confirmed).grid(row=6,column=0,columnspan=3,sticky='w',padx=5,pady=5)
        controls = ttk.Frame(editor)
        controls.grid(row=6,column=3,sticky='e')
        ttk.Button(controls,text='Accept / update',command=self.accept).pack(side='left',padx=3)
        ttk.Button(controls,text='Remove hold',command=self.remove).pack(side='left',padx=3)
        for field in self.fields.values():
            field.trace_add('write', lambda *_: self.confirmed.set(False))

        panes = ttk.Panedwindow(self, orient='horizontal')
        panes.pack(fill='both', expand=True, padx=10, pady=4)
        left = ttk.Frame(panes)
        panes.add(left,weight=2)
        self.tree = ttk.Treeview(left, columns=('page','label','value','method','status'), show='headings', selectmode='browse')
        for key, title, width in [('page','Page',45),('label','Label',90),('value','Elevation',85),('method','Read by',75),('status','State',100)]:
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width,stretch=key in ('label','status'))
        scroll = ttk.Scrollbar(left,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y')
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',self.select)
        self.tree.tag_configure('accepted',foreground='#a8c87d')
        right = ttk.Frame(panes)
        panes.add(right,weight=3)
        viewbar = ttk.Frame(right)
        viewbar.pack(fill='x')
        ttk.Checkbutton(viewbar,text='Whole sheet / legend',variable=self.whole_sheet,command=self.render).pack(side='left')
        ttk.Button(viewbar,text='Zoom +',command=lambda:self.scale(1.4)).pack(side='right')
        ttk.Button(viewbar,text='Zoom -',command=lambda:self.scale(1/1.4)).pack(side='right')
        self.evidence = tk.StringVar(value='Select a detected callout to see its printed evidence.')
        ttk.Label(right,textvariable=self.evidence,wraplength=560).pack(fill='x')
        canvas_frame = ttk.Frame(right)
        canvas_frame.pack(fill='both',expand=True)
        self.canvas = tk.Canvas(canvas_frame,background='#0c1721',highlightthickness=0)
        sx = ttk.Scrollbar(canvas_frame,orient='horizontal',command=self.canvas.xview)
        sy = ttk.Scrollbar(canvas_frame,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(xscrollcommand=sx.set,yscrollcommand=sy.set)
        canvas_frame.columnconfigure(0,weight=1)
        canvas_frame.rowconfigure(0,weight=1)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        sx.grid(row=1,column=0,sticky='ew')
        sy.grid(row=0,column=1,sticky='ns')

    def start_work(self, callback, kind):
        if self.busy:
            return
        self.busy = True
        self.ocr_button.configure(state='disabled')
        def worker():
            try:
                self.events.put((kind, callback(), None))
            except Exception as error:
                self.events.put((kind,None,str(error)))
        threading.Thread(target=worker,daemon=True).start()
        self.after(100,self.poll)

    def poll(self):
        try:
            kind,result,error = self.events.get_nowait()
        except queue.Empty:
            self.after(100,self.poll)
            return
        self.busy = False
        self.ocr_button.configure(state='normal')
        if error:
            self.status.set('Reading failed: '+error)
            messagebox.showerror('Read PDF grades',error,parent=self)
            return
        if kind == 'scan':
            self.rows,self.info = result
            self.page_spin.configure(to=len(self.info['pages']))
            self.restore()
        else:
            known = {r.id for r in self.rows}
            self.rows.extend(r for r in result if r.id not in known)
            self.filter_page.set(True)
        self.populate()
        self.status.set(f'{len(self.rows)} observations across {len(self.info.get("pages",[]))} sheets. '
                        f'{len(self.spots)} accepted. Visually check for missed callouts; OCR may misread digits.')
        self.render()

    def restore(self):
        path = Path(self.app.anchor_path)
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema') != 'gradeguard.pdf-controls.v1':
                return
            validate_reviewed_data(data,self.pdf_path,self.cad_path)
            self.spots = {s['id']:s for s in data['manual_spots']}
            known = {r.id for r in self.rows}
            for s in self.spots.values():
                if s['id'] not in known:
                    self.rows.append(PdfGrade(s['id'],s['page'],tuple(s['pdf_bbox']),s['detected_label'],
                                              s['detected_elevation'],s['raw'],s['method']))
        except Exception as error:
            messagebox.showwarning('Saved controls require review',str(error),parent=self)

    def populate(self):
        selection = self.selected.id if self.selected else None
        self.tree.delete(*self.tree.get_children())
        try:
            current_page = self.page.get()
        except tk.TclError:
            return
        for row in self.rows:
            if self.filter_page.get() and row.page != current_page:
                continue
            accepted = row.id in self.spots
            if self.search.get().strip().upper() not in (row.raw+' '+row.label+' '+str(row.elevation)).upper():
                continue
            category = self.filter_kind.get()
            if category == 'Accepted controls' and not accepted:
                continue
            if category == 'High / low points' and not any(t in row.label for t in ('HP','LP','HIGH POINT','LOW POINT')):
                continue
            if category == 'Curb labels' and not any(t in row.label for t in ('TC','BC','BOC','TB','FL','GUT','CURB')):
                continue
            self.tree.insert('', 'end', iid=row.id, values=(row.page,row.label,f'{row.elevation:.4f}'.rstrip('0').rstrip('.'),
                             row.method,'Accepted' if accepted else 'Review'), tags=('accepted',) if accepted else ())
        if selection and self.tree.exists(selection):
            self.tree.selection_set(selection)

    def select(self, event=None):
        ids = self.tree.selection()
        if not ids:
            return
        self.selected = next(row for row in self.rows if row.id == ids[0])
        row = self.selected
        self.page.set(row.page)
        for variable in self.fields.values():
            variable.set('')
        self.fields['label'].set(row.label)
        self.fields['elevation'].set(str(row.elevation))
        saved = self.spots.get(row.id)
        if saved:
            for key in ('meaning','role','reference'):
                self.fields[key].set(saved[key])
            self.fields['easting'].set(str(saved['cad'][0]))
            self.fields['northing'].set(str(saved['cad'][1]))
            self.fields['height'].set(str(saved['curb_rise']))
            self.fields['elevation'].set(str(saved['raw_elevation']))
        self.confirmed.set(False)
        self.evidence.set(row.raw.replace('\n',' | ')+'\n'+row.flags)
        self.whole_sheet.set(not bool(row.bbox))
        self.zoom = 1.0
        self.render()

    def render(self):
        if not self.info:
            return
        try:
            page = self.page.get()
            if not 1 <= page <= len(self.info['pages']):
                raise ValueError('Choose a PDF page in the document.')
            bbox = self.selected.bbox if self.selected and self.selected.page == page and not self.whole_sheet.get() else None
            data,rect = render_evidence(self.pdf_path,page,bbox,width=int(650*self.zoom))
            self.photo = tk.PhotoImage(data=base64.b64encode(data))
            self.canvas.delete('all')
            self.canvas.create_image(0,0,anchor='nw',image=self.photo)
            if rect:
                self.canvas.create_rectangle(*rect,outline='#ef4444',width=2)
            self.canvas.configure(scrollregion=(0,0,self.photo.width(),self.photo.height()))
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
        except Exception as error:
            self.status.set(str(error))

    def change_page(self):
        self.selected = None
        for variable in self.fields.values():
            variable.set('')
        self.confirmed.set(False)
        self.evidence.set('Whole sheet view. Select a callout or key a missed one after checking the legend.')
        self.whole_sheet.set(True)
        self.populate()
        self.render()

    def scale(self, factor):
        self.zoom = min(5,max(0.5,self.zoom*factor))
        self.render()

    def ocr(self):
        try:
            page = self.page.get()
            if not 1 <= page <= len(self.info.get('pages',[])):
                raise ValueError('Choose a valid page first.')
            self.status.set(f'OCR is reading page {page} locally. This may take up to two minutes. Verify every detected digit.')
            self.start_work(lambda:ocr_page(self.pdf_path,page),'ocr')
        except Exception as error:
            messagebox.showerror('OCR',str(error),parent=self)

    def manual(self):
        try:
            page = self.page.get()
            if not 1 <= page <= len(self.info.get('pages',[])):
                raise ValueError('Choose a valid PDF page first.')
            row = PdfGrade('manual-'+uuid.uuid4().hex,page,(),'MANUAL',0.0,'Manually read from PDF','Manual entry')
            self.rows.append(row)
            self.search.set('')
            self.filter_kind.set('All callouts')
            self.populate()
            self.tree.selection_set(row.id)
            self.tree.see(row.id)
        except Exception as error:
            messagebox.showerror('Key callout',str(error),parent=self)

    def accept(self):
        try:
            if not self.selected:
                raise ValueError('Select a callout first.')
            f = {key:var.get() for key,var in self.fields.items()}
            control = reviewed_control(self.selected,f['elevation'],f['easting'],f['northing'],f['meaning'],f['role'],
                                       f['height'],f['reference'],self.confirmed.get())
            control['reviewed_label'] = f['label']
            self.spots[self.selected.id] = control
            self.dirty = True
            self.populate()
            self.status.set(f'{len(self.spots)} accepted controls. Save to use them in curb generation. Top grade: {control["elev"]:.4f} ft.')
        except Exception as error:
            messagebox.showerror('Review curb control',str(error),parent=self)

    def remove(self):
        if self.selected and self.selected.id in self.spots:
            del self.spots[self.selected.id]
            self.dirty = True
            self.populate()
            self.status.set('Hold removed. Save to update the control file used by this job.')

    def save(self):
        try:
            # Existing engine validation checks declared DXF units before any hold is saved.
            from schoel_gradeguard import validate_generation_inputs
            validate_generation_inputs(self.cad_path,units_confirmed=self.app.unitless_confirm_var.get())
            if fingerprint(self.pdf_path) != self.info.get('pdf_sha256'):
                raise ValueError('The PDF changed during review. Close and scan it again.')
            if not self.cad_hash or fingerprint(self.cad_path) != self.cad_hash:
                raise ValueError('Select the matching DXF before review. If it changed, close and review the controls again.')
            default = 'GradeGuard_PDF_Controls.json'
            path = filedialog.asksaveasfilename(parent=self,title='Save reviewed PDF curb controls',defaultextension='.json',
                                              initialdir=self.app.out_var.get() or None,initialfile=default,
                                              filetypes=[('PDF controls','*.json')])
            if not path:
                return
            if Path(path).resolve() in (Path(self.pdf_path).resolve(),Path(self.cad_path).resolve()):
                raise ValueError('Choose a new control file, never a source drawing or PDF.')
            save_reviewed_data(path,list(self.spots.values()),self.pdf_path,self.cad_path)
            self.app.anchor_path = path
            self.dirty = False
            self.app.log_msg(f'Saved {len(self.spots)} reviewed PDF curb controls: {path}')
            self.status.set(f'Saved {len(self.spots)} controls. Save the job to retain this file selection. Curb generation checks their CAD locations.')
        except Exception as error:
            messagebox.showerror('Save PDF controls',str(error),parent=self)

    def export(self):
        path = filedialog.asksaveasfilename(parent=self,title='Export observations for review only',defaultextension='.csv',
                                          initialfile='PDF_Grade_Observations_REVIEW_ONLY.csv',filetypes=[('CSV','*.csv')])
        if path:
            try:
                export_review(path,self.rows)
                self.status.set('Saved observation CSV for review. This is not a staking point file.')
            except Exception as error:
                messagebox.showerror('Export observations',str(error),parent=self)

    def close(self):
        if self.dirty and not messagebox.askyesno('Unsaved control changes','Close without saving these control changes?',parent=self):
            return
        self.destroy()
