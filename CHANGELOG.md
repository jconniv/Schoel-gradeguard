# Changelog

## [Unreleased]

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
