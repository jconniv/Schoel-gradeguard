# Workflow preview 9.2.0

## Baseline and version discovery

Checked on 2026-10-28. The newest existing source is GitHub branch `bugfix/curb-performance-9.1.1`, commit `232ae13e7f8658bb1ba439b5f30b524026d06dd4`. PR #2 is open and unmerged. GitHub `main` is version 9.1.0 at `f85b571412c36467e6d2979b589c31701e2041f6`; no GitHub releases or tags were returned.

The supplied `C:\SchoelTools\GradeGuard\SchoelGradeGuard_v9_0_Survey_Package_Mode` contains 9.0.5 source and packaged builds. The older Desktop workspace with a 9.0.1 performance build is a separate development line and does not include the newer export safeguards. The Desktop `.publish-gradeguard` checkout contains the matching 9.1.1 source.

This preview is an isolated clone, now at `C:\SchoelTools\SchoelGradeGuard`, branch `feature/simpler-workflow`, based on 9.1.1. It was moved from `C:\SchoelTools\GradeGuard\GradeGuard-Development` during the October 28 consolidation. Older GradeGuard copies are preserved under `C:\SchoelTools\archive\GradeGuard\2026-10-28`, with their original locations and verification recorded in `MOVE-MANIFEST.json`. The unrelated Asset Tracker checkout in the former Desktop development folder remains in place. No GitHub or SharePoint publishing is part of this preview.

## Scope

The interface is now in `gradeguard_ui.py`. The main application still owns its existing Tk variables, project storage, selection dialogs and generation callbacks. Four staking pages replace the long combined form. Fixed actions, status messages, tab navigation and results access reduce scrolling. The executable and survey-package manifest now identify the canonical application version.

## Validation

Run `python -m unittest discover -s tests -v`, `python -m py_compile schoel_gradeguard.py gradeguard_ui.py build_exe.py`, and `python build_exe.py`.

The workflow tests use synthetic file paths and mocked generation, exercise the actual Tk controls, verify navigation/project settings and curb-height behavior, and verify generation-error recovery. The original HP/LP and field-export tests remain included. A source comparison against the 9.1.1 baseline checks that the calculation functions are unchanged. These are software checks, not survey accuracy validation.

Completed on 2026-10-28: 14 tests passed, Python compilation passed, and the versioned Windows executable built successfully. The source comparison found 198 unchanged functions/methods; only view construction, three completion/error UI methods, and the manifest title changed. All four staking pages and Results were visually checked. The packaged executable launched with the bundled branding, navigated to curb staking, and correctly rejected generation without a CAD input. SHA-256 was verified after copying the executable to the deliverables folder.

PyInstaller emitted hidden-import warnings for `scipy.special._cdflib`, `pysqlite2`, `MySQLdb`, and `psycopg2`. No build error occurred. Representative project processing in the frozen executable remains unverified.

## Existing limitations requiring a separate calculation pass

- LandXML coordinate order is still inferred from coordinate magnitudes.
- Source-preference radio buttons retain the existing behavior: the value is stored and used for the missing-LandXML prompt, but is not passed to the calculation engine to change source precedence.
- The local contour fallback still has fixed elevation and contour-interval limits; temporary contour TINs use unconstrained triangulation.
- The CAD-units setting is not a general CRS, unit or vertical-datum validator.
- Sidewalk, structure and grade-grid generation retain their existing synchronous behavior.

Before production use, review the workflow on a representative approved job and compare survey outputs with approved reference results. The local portable preview is unsigned and does not replace the installed application or the CAD launcher.
