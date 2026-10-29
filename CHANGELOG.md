# Changelog

## [Unreleased]

## [9.6.0] - 2026-10-29 (local guided workflow pilot)

### Added
- Linked calculation inspection: select a generated point on the plan, point list or grade profile to inspect its source, status and recorded calculation evidence.
- Saved review packages for PDF and CAD runs; profiles preserve disconnected paths and missing-grade gaps.

### Changed
- Three large home actions: Open plans, Calculate, Review results. Detailed files/tool settings are under Advanced settings.
- Guided PDF next-step buttons, plain-language grade meanings, and survey-coordinate/options fields shown only when needed.
- Match the workspace and review windows to the dozer opening's navy, sage-green and gold palette, with readable inputs and preserved white plan sheets.

## [9.5.0] - 2026-10-28 (local automatic calculation pilot)

### Added
- Auto calculate runs curb, sidewalk and finished/subgrade outputs together from DXF/PDF and optional LandXML, with continuous point numbering and a combined result summary.
- PDF-only sheet calculator supports calibrated local or survey coordinates, vector line selection/manual tracing, located plan elevations, automatic station interpolation and a bounded review grid.
- Preserve PDF-derived calculation provenance, unbracketed gaps and proposed surface assumptions in separate review outputs; original plan files remain unchanged.

### Scope
- PDF-only jobs need a calibrated sheet, selected/traced paths and reviewed control locations/meanings. The software calculates intermediate elevations and path slopes; it does not infer the entire design from an unreviewed PDF.
- PDF grade grids use only finished-grade controls inside a drawn boundary and the control triangles. All PDF-only calculations remain review outputs, including when calibrated to survey coordinates.
- CAD batch numbering reserves curb offset IDs before sidewalk/grid generation. Existing export approval rules remain in effect. Partial failures are listed individually.

## [9.4.0] - 2026-10-28 (local PDF review pilot)

### Added
- Read PDF grades review screen: scan every sheet, inspect callouts on the page, optionally OCR a selected sheet, and save individually reviewed curb controls with source provenance.

### Fixed
- Remove the PDF reader's single-sheet assumption and project-specific spot elevation range. Recognize suffix, stacked, dotted, combined, high/low and swale labels without assuming a curb meaning.
- Bind newly reviewed PDF controls to both PDF and DXF fingerprints; require explicit local curb conversion, units/reference confirmation and CAD tick coordinates before use.

### Scope
- Top/back-of-curb holds use the printed grade directly. Confirmed lower grades receive their individually entered rise once; flush curb accepts an explicit zero rise. Reviewed positions must be within the existing 0.35-foot curb association tolerance.
- Public plan tests cover actual PDF text, rendered callouts and local OCR limitations. Synthetic controls test calculations, source changes and export. OCR is optional through locally installed Tesseract and remains review-only; some drawn/rotated labels require manual entry.
- No matching public DXF or real-project control was supplied. This pilot does not certify complete PDF recognition or survey accuracy. Existing CAD-label conventions and other tool calculations are unchanged.

## [9.3.1] - 2026-10-28 (local calculation corrections)

### Fixed
- Preserve held grades and curb-height conversions; isolate tools and drawing-bound selections; use one field-export approval gate.
- Correct LandXML axes/surface selection/units, source preference, curve geometry, reflected PDF alignment and structure identity/centroids.
- Require actual connected contour brackets; withhold ambiguous, conflicting, unregistered and outside-surface results. Protect sidewalk design holds and retain rejected calculations in review data.
- Save all sidewalk/structure settings, reject invalid numeric settings, and preserve prior output runs.
- Harden both CAD import helpers against blank/nonfinite/RED elevations and quoted CSV; 3D aliases no longer infer connecting lines from row order.

### Validation and scope
- All 78 audit cases and all 49 unit/regression tests pass on synthetic inputs. The 9.3.0 audit recorded 40 passes and 38 failures; original findings remain in `docs/CALCULATION-AUDIT-9.3.0.txt`.
- See `docs/CALCULATION-FIXES-9.3.1.txt` for corrected behavior, fixture contract changes, supported inputs and remaining review requirements.
- Local unsigned build; real-project comparison and professional review remain required. No survey source data, MSI deployment or Git remote changed.

## [9.3.0] - 2026-10-28 (preview)

### Added
- Animated opening screen: a tracked dozer grades a survey-grid scene with rotating tracks, a moving blade and drifting dust. Opens the workspace automatically and supports immediate keyboard/button skip.
- Replay opening from the main window without resetting the current job.

### Survey/data impact
- Startup presentation only; no survey calculations, defaults, project data or export rules change.

### Maintenance
- Consolidated the active preview in `C:\SchoelTools\SchoelGradeGuard`; preserved older GradeGuard folders and Git history in the dated local archive with a verified move manifest. Added launch shortcuts. The folder move itself did not change application code or executable bytes.

## [9.2.0] - 2026-10-28 (workflow preview)

### Added
- Guided Project files, Staking tools, and Results navigation, with dedicated curb, sidewalk, structure, and grade-grid pages.
- Add input-file status, a persistent job toolbar, output-folder access, and a compact activity indicator.

### Changed
- Group contractor offsets and elevation options with curb staking; keep each tool's generation action visible while its settings scroll.
- Use the canonical application version in executable names and survey-package manifests.

### Survey/data impact
- Interface and version identification only. Existing generation functions, project settings, defaults, coordinates, elevations, units, tolerances, and export safety rules are retained.

## [9.1.1] - 2026-09-16

### Fixed

- Index curb endpoints to avoid all-pairs connectivity and ordering scans while preserving original distance and tie rules.
- Read PDF grading-sheet vectors once per label extraction and reuse the loaded DXF for layer detection.

### Survey/data impact

- Performance only; retains 9.1.0 preflight, HP/LP trust checks, ambiguous BOC handling, and withholding unsafe staking points. No elevation formula, unit, datum, tolerance, precision, or export rule changed.

## [9.1.0] - 2026-08-28

### Added

- Added an explicit preflight confirmation summarizing CAD, certified PDF, surface source, U.S. survey-foot workflow, curb conversion, selected geometry, and output location.
- Added `GradeGuard_DO_NOT_STAKE.csv` as a clearly labeled office-review report for withheld points.
- Added regression tests for stake-ready filtering and withheld-point reporting.

### Changed

- Unsafe, missing, zero, and review-only elevations are now omitted from the raw stake-ready CSV instead of being exported as numeric `0.000` elevations.
- Debug and confidence reports now leave withheld elevations blank so they cannot resemble measured or designed zero elevations.

### Survey/data impact

- No coordinate, surface, curb, HP/LP, interpolation, or elevation formula changed. The export safety gate changed: points without an approved elevation source are withheld from field import and require office review.

## [9.0.5] - 2026-08-28

### Added

- Added `GradeGuard.lsp` so GradeGuard can be launched directly from Civil 3D or AutoCAD with `SCHOELGRADEGUARD`.
- Added commands for selecting and testing a nonstandard GradeGuard installation path.
- Added CAD startup and ribbon instructions and included all three GradeGuard LISP files in the MSI.

### Changed

- The MSI now installs the versioned build as the stable `SchoelGradeGuard.exe` filename used by the CAD launcher.

### Survey/data impact

- CAD launch and packaging only. No calculations, coordinates, elevations, units, datums, precision, imports, exports, or QA rules changed.

## [9.0.4] - 2026-08-28

### Added

- Added a versioned WiX MSI installer for per-machine deployment under `C:\SchoelTools\SchoelGradeGuard`.
- Added certificate-store signing and verification for both the GradeGuard EXE and MSI.
- Added a SHA-256 release manifest for update verification and traceability.

### Security

- Signing uses the installed Schoel Engineering private certificate without committing or copying PFX files, passwords, or private-key material into the repository.

### Survey/data impact

- Packaging and signing only. No calculations, coordinates, elevations, units, datums, precision, imports, exports, or QA rules changed.

## [9.0.3] - 2026-08-28

### Fixed

- Classified bare `BOC` HP/LP callouts as ambiguous because engineering plans may use BOC for either back of curb or bottom of curb.
- Prevented ambiguous BOC elevations from becoming automatic HP/LP hard holds or receiving an assumed curb-height conversion.
- Required the plan legend/engineer convention to be confirmed and the callout manually anchored before an ambiguous BOC value controls staking output.
- Added explicit, review-only detection for fully written `BOTTOM OF CURB` HP/LP callouts.

### Survey/data impact

- This is a staking-elevation safety correction. Ambiguous BOC values are now review-only instead of being silently interpreted as top/back-of-curb elevations.

## [9.0.2] - 2026-08-28

### Fixed

- Prevented compound PDF callouts such as `HP/TC` and `LOW POINT BOC` from being parsed repeatedly as generic TC/BC callouts.
- Added explicit recognition of HP/LP elevation forms such as `HP EL. 612.34` and `LP ELEV 611.80`.
- Restricted automatic PDF HP/LP hard holds to callouts with a vector leader/tick endpoint and controlled PDF-to-CAD alignment (RMSE no greater than 1.0 ft, with no alignment warning).
- Kept approximate extents matches and text-box-center detections as review-only evidence requiring a manual tick anchor.
- Corrected the generated HTML report and README to display version 9.0.2.

### Survey/data impact

- HP/LP elevations can affect exported staking elevations. This patch prevents uncertain PDF positions from silently becoming exact hard holds; rejected automatic detections require office review/manual anchoring.

## [9.0.1] - 2026-08-28

### Changed

- Kept the GradeGuard window responsive during BC point generation by moving DXF/PDF processing and report creation to a guarded background worker.
- Added an in-progress cursor, button state, and duplicate-run protection for slower computers.
- Replaced quadratic staking-point de-duplication with tolerance-equivalent spatial bucketing for dense curb geometry.
- Updated the Windows build artifact name and displayed application version to 9.0.1.

### Survey/data impact

- No calculation, coordinate, elevation, unit, datum, precision, import, export, or QA rule was changed.
