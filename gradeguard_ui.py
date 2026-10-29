"""GradeGuard's office workflow view; all calculations stay in the application."""
import os
import tkinter as tk
from tkinter import ttk, messagebox


class WorkflowView:
    def __init__(self, app, version, resource_path):
        self.app = app
        self.canvases = {}
        self.file_status = []
        self.job_buttons = []
        self._refresh_id = None
        self._style()
        app.title(f"Schoel GradeGuard v{version} | Workflow Preview")
        app.geometry("1180x820")
        app.minsize(1000, 700)

        header = ttk.Frame(app, padding=(22, 12), style="Header.TFrame")
        header.pack(fill="x")
        try:
            logo = tk.PhotoImage(file=resource_path("gradeguard_logo.png"))
            factor = max(1, (logo.width() + 119) // 120)
            self.logo = logo.subsample(factor, factor)
            ttk.Label(header, image=self.logo, style="Logo.TLabel").pack(side="left", padx=(0, 20))
        except (tk.TclError, OSError):
            self.logo = None
        title = ttk.Frame(header, style="Header.TFrame")
        title.pack(side="left", fill="x", expand=True)
        ttk.Label(title, text="Schoel GradeGuard", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title, text="OPEN PLANS  /  CALCULATE  /  REVIEW", style="Header.TLabel").pack(anchor="w", pady=(4, 0))
        brand_actions = ttk.Frame(header, style="Header.TFrame")
        brand_actions.pack(side="right")
        ttk.Label(brand_actions, text=f"v{version}  ·  Workflow preview", style="Header.TLabel").pack(anchor="e", pady=(0, 6))
        ttk.Button(brand_actions, text="Replay opening", command=app.show_opening).pack(anchor="e")

        jobbar = ttk.Frame(app, padding=(22, 8))
        jobbar.pack(fill="x")
        ttk.Label(jobbar, textvariable=app.project_var, font=("Segoe UI", 10, "bold")).pack(side="left", fill="x", expand=True)
        for label, callback in [("New job", self.new_job), ("Open job", self.open_job), ("Save job", app.save_job)]:
            button = ttk.Button(jobbar, text=label, command=callback)
            button.pack(side="left", padx=(6, 0))
            self.job_buttons.append(button)

        footer = ttk.Frame(app, padding=(22, 8))
        footer.pack(side="bottom", fill="x")
        self.activity = tk.StringVar(value="Start with your project files.")
        ttk.Label(footer, textvariable=self.activity).pack(side="left", fill="x", expand=True)
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=140)
        self.progress.pack(side="right")
        self.steps = ttk.Notebook(app, style="Steps.TNotebook")
        self.steps.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        self.files_page = ttk.Frame(self.steps)
        self.home_page = ttk.Frame(self.steps)
        self.tools_page = ttk.Frame(self.steps)
        self.results_page = ttk.Frame(self.steps)
        for page, label in [(self.home_page, "Start here"), (self.results_page, "Results & review")]:
            self.steps.add(page, text=label)
        from gradeguard_start import build_start
        build_start(self, self.home_page)
        self._files()
        self._tools()
        self._results()
        for variable in (app.cad_var, app.pdf_var, app.surface_var, app.out_var):
            variable.trace_add("write", self.schedule_refresh)
        self.steps.bind("<<NotebookTabChanged>>", self.schedule_refresh, add=True)
        app.bind("<MouseWheel>", self._mousewheel, add=True)
        app.bind("<Destroy>", self._on_destroy, add=True)
        self.refresh_files()
        app.log_msg("Ready. Load project files, choose a staking tool, then review the generated outputs.")

    def _style(self):
        from gradeguard_theme import apply_theme, BG
        self.bg = BG
        apply_theme(self.app)

    def scroll_body(self, parent):
        shell = ttk.Frame(parent)
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, background=self.bg, highlightthickness=0)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, padding=(18, 14))
        item = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        self.canvases[str(body)] = canvas
        return body

    def _mousewheel(self, event):
        # Scroll only the page under the pointer; leave text, dropdowns and pickers alone.
        widget = self.app.winfo_containing(event.x_root, event.y_root)
        if widget is None or widget.winfo_toplevel() != self.app:
            return
        if widget.winfo_class() in ("Text", "TCombobox", "Listbox", "Treeview"):
            return
        while widget is not None:
            canvas = self.canvases.get(str(widget))
            if canvas is not None:
                if canvas.yview() != (0.0, 1.0) and event.delta:
                    units = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
                    canvas.yview_scroll(units, "units")
                return "break"
            widget = widget.master

    def card(self, parent, title):
        frame = ttk.LabelFrame(parent, text=title, padding=14)
        frame.pack(fill="x", pady=(0, 14))
        return frame

    def note(self, parent, text):
        label = ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=800, justify="left")
        label.pack(anchor="w", fill="x", pady=(4, 8))
        label.bind("<Configure>", lambda event: label.configure(wraplength=max(120, event.width)))
        return label

    def field(self, parent, row, label, variable, values=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 20))
        if values is None:
            widget = ttk.Entry(parent, textvariable=variable, width=18)
        else:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=36)
        widget.grid(row=row, column=1, sticky="w", pady=5)
        return widget

    def _files(self):
        bar = ttk.Frame(self.files_page, padding=(18, 10))
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, text="The same project files are shared by all four staking tools.", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Continue to staking tools  >", style="Accent.TButton", command=lambda: self.steps.select(self.tools_page)).pack(side="right")
        body = self.scroll_body(self.files_page)
        ttk.Label(body, text="Set up your project", style="Heading.TLabel").pack(anchor="w")
        self.note(body, "Choose the source files once. Use Open job to restore a saved project.")
        inputs = self.card(body, "Project inputs")
        rows = [
            ("CAD geometry", "Civil DXF", self.app.cad_var, self.app.pick_cad, (".dxf",)),
            ("Certified plans", "PDF required for curb staking", self.app.pdf_var, self.app.pick_pdf, (".pdf",)),
            ("Proposed surface", "Optional LandXML / XML", self.app.surface_var, self.app.pick_surface, (".xml", ".landxml")),
            ("Output folder", "Generated files and review reports", self.app.out_var, self.app.pick_out, None),
        ]
        for index, (title, hint, variable, command, extensions) in enumerate(rows):
            row = ttk.Frame(inputs)
            row.pack(fill="x", pady=(0 if index == 0 else 12, 0))
            row.columnconfigure(1, weight=1)
            caption = ttk.Frame(row, width=220)
            caption.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 18))
            ttk.Label(caption, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(caption, text=hint, style="Muted.TLabel", width=30).pack(anchor="w")
            entry = ttk.Entry(row, textvariable=variable)
            entry.grid(row=0, column=1, sticky="ew")
            ttk.Button(row, text="Browse...", command=command).grid(row=0, column=2, padx=(8, 0))
            status = ttk.Label(row, style="Muted.TLabel")
            status.grid(row=1, column=1, columnspan=2, sticky="w", pady=(3, 0))
            self.file_status.append((variable, status, extensions, title))
        self.pdf_review_button = ttk.Button(inputs, text="Read PDF grades / review curb controls", command=self.app.review_pdf_grades)
        self.pdf_review_button.pack(anchor="e", pady=(12, 0))
        self.job_buttons.append(self.pdf_review_button)
        self.auto_button = ttk.Button(inputs, text="Auto calculate curb, sidewalk & grade", style="Accent.TButton", command=self.auto_calculate)
        self.auto_button.pack(anchor="e", pady=(10, 0))
        self.job_buttons.append(self.auto_button)
        reference = self.card(body, "Surface selection and units")
        ttk.Label(reference, text="LandXML surface name (choose when the file contains several)").pack(anchor="w")
        self.app.surface_name_combo = ttk.Combobox(reference, textvariable=self.app.surface_name_var, width=52)
        self.app.surface_name_combo.pack(anchor="w", fill="x", pady=(4, 10))
        ttk.Checkbutton(reference, text="I confirmed that this unitless DXF uses US survey feet", variable=self.app.unitless_confirm_var).pack(anchor="w")
        self.note(reference, "Calculations use US survey feet. Declared meter/international-foot files are rejected; no coordinates or datums are converted. Confirm the vertical reference and project control separately.")
        info = self.card(body, "Before you generate")
        self.note(info, "Confirm source units, control, benchmark and curb details against the certified plans. File checks here confirm availability only.")
        self.note(info, "For 2D CAD with 3D contours, leave the proposed surface blank. Each tool separates unresolved points into a DO NOT STAKE report. Temporary TIN and unbracketed estimates remain review-only.")

    def _tools(self):
        summary = ttk.Frame(self.tools_page, padding=(18, 8))
        summary.pack(fill="x")
        self.inputs_summary = tk.StringVar()
        ttk.Label(summary, textvariable=self.inputs_summary, style="Muted.TLabel").pack(side="left", expand=True, fill="x")
        ttk.Button(summary, text="Edit project files", command=lambda: self.steps.select(self.files_page)).pack(side="right")
        self.tools = ttk.Notebook(self.tools_page)
        self.tools.pack(fill="both", expand=True, padx=8)
        self.curb_page = self.tool_page("Curb staking", "Review & generate BC points", self.generate_bc, curb=True)
        self.sidewalk_page = self.tool_page("Sidewalk", "Generate sidewalk points", self.app.generate_edge_sidewalk_points)
        self.structures_page = self.tool_page("Storm / sanitary", "Generate FL OUT points", self.app.generate_flout_points)
        self.grid_page = self.tool_page("Grade grid", "Generate grade grid", self.app.generate_grade_grid)
        self._curb(self.curb_page)
        self._sidewalk(self.sidewalk_page)
        self._structures(self.structures_page)
        self._grid(self.grid_page)

    def tool_page(self, title, action, callback, curb=False):
        page = ttk.Frame(self.tools)
        self.tools.add(page, text=title)
        bar = ttk.Frame(page, padding=(18, 10))
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="View results & log", command=self.show_results).pack(side="left")
        button = ttk.Button(bar, text=action, command=callback, style="Accent.TButton")
        button.pack(side="right")
        if curb:
            self.app.generate_button = button
        self.job_buttons.append(button)
        return self.scroll_body(page)

    def _curb(self, body):
        app = self.app
        geometry = self.card(body, "1. Select curb geometry")
        row = ttk.Frame(geometry)
        row.pack(fill="x")
        ttk.Button(row, text="Pick true back of curb", command=app.pick_back_of_curb_entities).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Pick flowline / gutter", command=app.pick_flowline_entities).pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Detect BC layers", command=app.detect_layers).pack(side="left")
        self.note(geometry, "Pick the actual BC and flowline objects when they share a generic layer. Layer-detection details appear in Results & review.")
        settings = self.card(body, "2. Point settings & curb detail")
        self.field(settings, 0, "Starting point number", app.start_var)
        self.field(settings, 1, "Straight curb spacing (ft)", app.spacing_var)
        self.field(settings, 2, "Curb detail from certified PDF", app.curb_type_var, ["Standard curb (+0.50)", "Catch curb per PDF (+0.42)", "Spill curb per PDF (+0.58)", "Custom PDF detail height"])
        self.field(settings, 3, "Curb height (ft)", app.height_var)
        def sync_height(*_):
            value = app.curb_type_var.get()
            for prefix, height in [("Standard", "0.50"), ("Catch", "0.42"), ("Spill", "0.58")]:
                if value.startswith(prefix):
                    app.height_var.set(height)
                    break
        app.curb_type_var.trace_add("write", sync_height)
        detail = ttk.Label(settings, text="Curves: PC, MID and PT. Confirm the selected curb detail before generation.", style="Muted.TLabel")
        detail.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        meaning = self.card(body, "3. Surface / contour meaning")
        for label, value in [("Auto: use explicit BC / FL controls; review ambiguous surface meaning", "auto"), ("Top / back of curb or grading surface: use directly", "bc"), ("Flowline / gutter / pavement surface: add curb height", "flowline")]:
            ttk.Radiobutton(meaning, text=label, variable=app.surface_interpret_var, value=value).pack(anchor="w")
        offset = self.card(body, "Contractor offset (optional)")
        ttk.Checkbutton(offset, text="Create a separate offset CSV and line DXF", variable=app.create_offset_var).pack(anchor="w")
        controls = ttk.Frame(offset)
        controls.pack(fill="x")
        self.field(controls, 0, "Offset distance (ft)", app.offset_distance_var)
        directions = ttk.Frame(offset)
        directions.pack(fill="x", pady=(6, 0))
        for index, (label, value) in enumerate([("Away from FL / gutter", "away_from_flowline"), ("Toward FL / gutter", "toward_flowline"), ("Left of BC", "left"), ("Right of BC", "right")]):
            ttk.Radiobutton(directions, text=label, variable=app.offset_mode_var, value=value).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 24))
        ttk.Checkbutton(offset, text="Flip offset side", variable=app.offset_reverse_var).pack(anchor="w")
        self.note(offset, "The BC generate button also creates enabled offset files. The normal BC CSV is always separate.")
        source = self.card(body, "Elevation source")
        for label, value in [("LandXML / Civil 3D surface first", "surface"), ("3D DXF / Topo-P-Surface first", "dxf_surface"), ("Contours touching curb only", "contours")]:
            ttk.Radiobutton(source, text=label, variable=app.source_mode, value=value).pack(anchor="w")

    def _sidewalk(self, body):
        app = self.app
        pick = self.card(body, "1. Select sidewalk geometry")
        ttk.Button(pick, text="Pick edge of sidewalk", command=app.pick_edge_sidewalk_entities).pack(anchor="w")
        self.note(pick, "For 2D sidewalk geometry, leave LandXML blank and use 3D DXF contours for elevations.")
        settings = self.card(body, "2. Sidewalk point settings")
        self.field(settings, 0, "Starting point number", app.esw_start_var)
        self.field(settings, 1, "Straight edge spacing (ft)", app.esw_spacing_var)
        self.field(settings, 2, "Description", app.esw_desc_var)
        adjustment = self.card(body, "3. Cross slope adjustment")
        self.field(adjustment, 0, "Adjustment", app.esw_adjust_var, ["direct", "auto from contours", "add cross slope", "subtract cross slope"])
        self.field(adjustment, 1, "Cross slope (%)", app.esw_cross_slope_var)
        self.field(adjustment, 2, "Sidewalk width (ft)", app.esw_width_var)
        self.note(body, "Use direct for elevations at the selected edge. Cross slope runs perpendicular to the walk: 10 ft at 1% gives 0.10 ft of elevation difference.")

    def _structures(self, body):
        app = self.app
        settings = self.card(body, "Storm / sanitary FL OUT points")
        self.field(settings, 0, "Starting point number", app.flout_start_var)
        self.field(settings, 1, "Find structures", app.flout_find_var, ["Both", "Storm", "Sanitary"])
        self.field(settings, 2, "Search radius (ft)", app.flout_search_radius_var)
        self.field(settings, 3, "Description format", app.flout_desc_format_var, ["Structure + Pipe Size + FL-OUT", "Structure + FL-OUT", "Pipe Size + FL-OUT", "FL-OUT only"])
        manual = self.card(body, "Manual structure selection")
        ttk.Button(manual, text="Pick structure centers / FL OUT", command=app.pick_flout_centers).pack(anchor="w")
        self.note(manual, "Auto mode requires one explicit OUT elevation associated with the same structure ID. Conflicting or IN-only values are withheld. Pick centers manually when the plans do not identify them clearly.")
        self.note(body, "Creates a separate field CSV with no header. Review the source and QA reports before use.")

    def _grid(self, body):
        app = self.app
        settings = self.card(body, "Subgrade / finished grade grid")
        self.field(settings, 0, "Grid type", app.grade_grid_type_var, ["SUBGRADE", "FINISHED GRADE"])
        self.field(settings, 1, "Starting point number", app.grade_grid_start_var)
        self.field(settings, 2, "Grid spacing (ft)", app.grade_grid_spacing_var)
        self.field(settings, 3, "Subtract from finished grade (ft)", app.grade_grid_subtract_var)
        self.note(body, "Uses LandXML, Topo-P-Surface or contours as finished grade, then subtracts the entered amount. Use 0.00 for FINISHED GRADE. Creates a separate field CSV with no header.")

    def _results(self):
        app = self.app
        body = ttk.Frame(self.results_page, padding=18)
        body.pack(fill="both", expand=True)
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Label(row, text="Review your generated package", style="Heading.TLabel").pack(side="left")
        ttk.Button(row, text="Open output folder", command=self.open_output).pack(side="right")
        ttk.Button(row, text="Inspect points & profiles", command=self.inspect_points).pack(side="right", padx=8)
        self.note(body, "Review the QA and source reports before field use. For BC points, GradeGuard_DO_NOT_STAKE.csv lists withheld points; keep it out of field imports.")
        ttk.Label(body, text="Layer detection", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 4))
        app.layer_box = self.text_box(body, 4, False)
        app.layer_box.insert("1.0", "No layer detection yet. In Staking tools > Curb staking, choose Detect BC layers or pick the actual curb objects.")
        ttk.Label(body, text="Activity & output paths", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(14, 4))
        app.log = self.text_box(body, 8, True)

    def text_box(self, parent, height, expand):
        frame = ttk.Frame(parent)
        frame.pack(fill="both" if expand else "x", expand=expand)
        text = tk.Text(frame, height=height, wrap="word", background="#0c1721", foreground="#f3f5ed", insertbackground="#f3f5ed", relief="solid", borderwidth=1, padx=10, pady=8, font=("Consolas", 10))
        scrollbar = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        return text

    def schedule_refresh(self, *_):
        if self._refresh_id is None:
            self._refresh_id = self.app.after_idle(self.refresh_files)

    def _on_destroy(self, event):
        if event.widget == self.app and self._refresh_id is not None:
            self.app.after_cancel(self._refresh_id)
            self._refresh_id = None

    def refresh_files(self):
        if self._refresh_id is not None:
            self.app.after_cancel(self._refresh_id)
            self._refresh_id = None
        for variable, label, extensions, title in self.file_status:
            path = variable.get().strip()
            color = "#a7b8c4"
            if not path:
                status = "Optional — using DXF surface / contours" if title == "Proposed surface" else "Not selected"
            elif extensions is None:
                status = "Folder exists" if os.path.isdir(path) else "Folder will be created during generation"
            elif os.path.splitext(path)[1].lower() not in extensions:
                status, color = "Choose " + " / ".join(extensions) + " format", "#ff9585"
            elif not os.path.isfile(path):
                status, color = "File not found — check this path", "#ff9585"
            else:
                status, color = "File found — contents and survey references still require review", "#a8c87d"
            label.configure(text=status, foreground=color)
        cad = os.path.basename(self.app.cad_var.get().strip()) or "No CAD selected"
        pdf = os.path.basename(self.app.pdf_var.get().strip()) or "No PDF selected"
        self.inputs_summary.set(f"CAD: {cad[:45]}   |   PDF: {pdf[:45]}")
        if hasattr(self, 'home_summary'):
            pdf_path=self.app.pdf_var.get().strip()
            self.home_summary.set((f"Plans: {os.path.basename(pdf_path)}\n" + (f"CAD: {cad}" if self.app.cad_var.get().strip() else "Workflow: PDF plans only")) if pdf_path else 'No plans selected. Start with Open plans.')

    def new_job(self):
        self.app.new_job()
        self.steps.select(self.home_page)
        self.activity.set("New job. Select your project files.")

    def auto_calculate(self):
        if self.app._generation_running:
            return
        from gradeguard_auto_ui import AutoCalculateWindow
        AutoCalculateWindow(self.app)

    def inspect_points(self):
        from gradeguard_review_ui import open_review
        open_review(self.app)

    def show_advanced(self):
        for page,label in [(self.files_page,'Files & setup'),(self.tools_page,'Tool settings')]:
            if str(page) not in self.steps.tabs(): self.steps.add(page,text=label)
        self.steps.select(self.files_page)

    def quick_calculate(self):
        if self.app._generation_running: return
        if not os.path.isfile(self.app.pdf_var.get().strip()):
            from gradeguard_start import QuickSetupWindow
            QuickSetupWindow(self); return
        if self.app.cad_var.get().strip(): self.auto_calculate()
        else:
            from gradeguard_auto_ui import PdfCalculationWindow
            PdfCalculationWindow(self.app)

    def open_job(self):
        self.app.open_job()
        self.steps.select(self.home_page)

    def generate_bc(self):
        self.app.generate()
        if self.app._generation_running:
            self.set_busy(True)

    def set_busy(self, busy):
        for button in self.job_buttons:
            button.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.start(15)
            self.activity.set("Calculating. Progress and output paths appear in Results & review.")
        else:
            self.progress.stop()
            self.activity.set("Generation finished. Check Results & review for the outcome.")
            self.app.generate_button.configure(text="Review & generate BC points")

    def show_results(self):
        self.steps.select(self.results_page)

    def open_output(self):
        path = self.app.out_var.get().strip()
        if not path or not os.path.isdir(path):
            messagebox.showinfo("Output folder", "The output folder does not exist yet. Generate a package or select an existing output folder.", parent=self.app)
            return
        try:
            os.startfile(os.path.abspath(path))
        except OSError as error:
            messagebox.showerror("Open output folder", str(error), parent=self.app)
