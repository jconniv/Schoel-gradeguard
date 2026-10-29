# Schoel GradeGuard

Current local folder: `C:\SchoelTools\SchoelGradeGuard`. Open `Start GradeGuard.lnk` to run the 9.6.0 local build. Older local copies are preserved under dated folders in `C:\SchoelTools\archive\GradeGuard`; manifests record original locations and file hashes.

Schoel GradeGuard is an internal survey-office application for producing and reviewing curb, sidewalk, grade-grid, and structure staking outputs from approved local DXF, LandXML, and civil PDF inputs.

## Status

- Current version: 9.6.0
- Status: local guided workflow pilot; project validation required before field use
- Intended users: Schoel Engineering survey-office personnel
- Maintainer: Joseph Conniv

GradeGuard output requires office review and appropriate licensed-surveyor validation. The software does not replace professional judgment or the certified plans.

Version 9.3.1 resolves the reproduced 9.3.0 audit failures: all 78 calculation checks and 49 unit/regression tests pass. Unsupported or conflicting results are withheld from field CSVs. See `docs/CALCULATION-FIXES-9.3.1.txt` for the changes and intentional limits; `docs/CALCULATION-AUDIT-9.3.0.txt` is historical evidence. Run `python tests/calculation_audit.py --report <new-report.json>` to reproduce the audit. These tests do not establish field/survey accuracy.

Version 9.4.0 adds **Read PDF grades / review curb controls** under Project files. Read all sheets, filter HP/LP and curb labels, inspect their printed evidence, and save individually confirmed controls. Top/back elevations hold directly; lower grades use an explicit local rise once. PDF and DXF fingerprints prevent stale controls. Optional local OCR remains review-only. See docs/PDF-REVIEW-9.4.0.txt for public-plan tests, workflow and limits.

Version 9.5.0 adds **Auto calculate curb, sidewalk & grade**. Run the selected CAD tools together with continuous point numbering and a combined accepted-point CSV, or open the PDF-only workspace. Calibrate a sheet, select/trace linework and locate its grade controls once, then calculate intermediate elevations, path slopes and a bounded finished/subgrade grid. PDF-only results remain explicitly marked for review. See `docs/AUTO-CALCULATE-9.5.0.txt` for setup, formulas and limits.

## Run from source

Version 9.6.0 adds a three-step home screen, guided PDF next-step prompts, and linked point/plan/profile inspection. Advanced settings reveal the detailed tools. The workspace uses the dozer opening's navy, green and gold. See `docs/EASY-WORKFLOW-9.6.0.txt` for the current instructions and review limits.

1. Install Python 3 and the packages in `requirements.txt`.
2. Run `python schoel_gradeguard.py` or `run_app.bat`.

## Workflow preview

A yellow tracked dozer crosses the branded opening screen and grades the terrain. The workspace opens automatically after five seconds. Click **Open GradeGuard** or press **Enter**, **Space**, or **Esc** to skip. **Replay opening** in the header replays it without changing the current job. This animation runs locally with Tk and does not access project files or the network.

Use **Open plans → Calculate → Review results** on the home screen. **Advanced settings** reveals **Files & setup** and **Tool settings** for detailed curb, sidewalk, storm/sanitary and grade-grid options. **Results & review** contains the activity log, layer-detection details, output folder and point inspection actions.

New/Open/Save job remain available at the top. Existing project files remain readable; old geometry/manual control selections require re-saving against the current drawing. File status checks confirm existence and extension only; they do not validate survey references or calculations. See `docs/HOW-TO.md` and `docs/WORKFLOW-PREVIEW-9.2.0.md` for the workflow and known limitations.

## Build the Windows application

Run `build_exe.bat` or `python build_exe.py`. The executable name comes from `APP_VERSION` in `schoel_gradeguard.py`. Generated executables and build caches are intentionally excluded from source control.

For a signed release, run `Build_Signed_MSI.ps1`. It builds the EXE and MSI, signs both from the approved Windows certificate store, verifies both signatures, and writes a SHA-256 release manifest. Private certificate files and passwords are never stored in this repository.

## Launch from Civil 3D or AutoCAD

The MSI installs `GradeGuard.lsp` with the application. Load it with `APPLOAD`, add it to the Startup Suite, and run `SCHOELGRADEGUARD`. See `CAD_Ribbon_Install_Readme.txt` for the ribbon macro and diagnostic commands. The existing GradeGuard import LISP files remain available for importing reviewed output.

Before BC generation, GradeGuard presents a preflight summary of the selected CAD, certified PDF, surface source, units workflow, curb conversion, geometry selections, and output folder. Uncertain or unsafe points are omitted from the stake-ready CSV and written to `GradeGuard_DO_NOT_STAKE.csv` for office review.

## Test

```powershell
python -m unittest discover -s tests -v
python -m py_compile schoel_gradeguard.py
```

## Survey-data safeguards

- Original survey, CAD, LandXML, and PDF inputs remain external to this repository.
- Generated outputs and local job-selection files are excluded from source control.
- Coordinates, units, datums, elevations, curb meanings, and BOC conventions must never be guessed.
- Bare `BOC` is review-only because it can mean either back of curb or bottom of curb depending on the engineer.

See `CHANGELOG.md` and `docs/CALCULATION-FIXES-9.3.1.txt` for current behavior. `V9_0_SURVEY_PACKAGE_MODE_NOTES.txt` is historical.
