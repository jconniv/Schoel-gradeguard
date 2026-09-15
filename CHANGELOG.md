# Changelog

## [Unreleased]

### Maintenance

- Added an automatic-on-`main`, manually runnable, non-deleting GitHub-to-SharePoint source mirror with protected credential handling and generated-file exclusions.

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
