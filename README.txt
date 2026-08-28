Schoel GradeGuard v9.0.3

Release validation:
  python -m unittest discover -s tests -v

PDF HP/LP safety:
- Reads HP, LP, HP/TC, LP/TC, HIGH POINT, LOW POINT, BOC/BC/TC, and EL./ELEV forms.
- Automatic PDF hard holds require a vector leader/tick endpoint and controlled PDF-to-CAD alignment.
- Approximate/text-center detections are review-only and require a manual tick anchor.
- Bare BOC is review-only because different engineers use it for BACK OF CURB or BOTTOM OF CURB. Confirm the plan legend before manually anchoring it.

Run:
  install_requirements.bat   (first time only)
  run_app.bat

Build EXE:
  build_exe.bat

v8.4 focus:
- Adds a visible required QA checklist before generating BC staking points.
- Stops BC generation if latest CAD, latest signed/certified PDF, units, control, benchmark, PDF-over-CAD, and curb detail checks are not confirmed.
- Adds curb detail selection: Catch curb (+0.42), Spill curb (+0.58), or Custom verified height.
- Defaults to catch curb +0.42 instead of nominal 0.50.
- Uses spill curb +0.58 when selected.
- Allows custom curb height only when the user confirms it from the civil PDF detail.
- Keeps the v8.3 survey-style BC chain interpolation between contour controls.
- Keeps field CSV files with no header.
- Keeps descriptions as BC, BC PC, BC PT, BC HP, and BC LP.
- Keeps exact contour crossing rules: contour on selected BC holds exact elevation; contour on selected FL/gutter/EP adds the verified curb height.
- Keeps unsupported elevations as 0.000 with review flags instead of guessing.
- Adds v8.4 checklist and curb-height notes to the HTML report/debug data.

Boss checklist rules built into v8.4:
- Confirm latest CAD and PDF files.
- Confirm CAD placement and coordinate units, especially US Survey Foot vs International Foot.
- Confirm control and benchmark.
- PDF plans trump CAD/DXF.
- Look at curb details to determine catch or spill curb.
- Use edge of paving/flowline plus 0.42 ft for catch curb.
- Use edge of paving/flowline plus 0.58 ft for spill curb.
- Provide crew point files and review information.
- Save DXF backgrounds in US Survey Feet when exporting backgrounds.


Schoel GradeGuard v8.4.9 Patch Notes

Purpose:
Fix the Brayfield Crest south curb error where a normal BC point near the south end was being overwritten to 615.000 even though the DXF contour control south of the curb end is 610.000.

Root cause:
The v8.4.7/v8.4.8 station contour profile was still using a global selected-BC station chain in some cases. When multiple curb pieces or parallel curb/gutter lines were selected, a contour anchor from a different curb run could land at a similar station and overwrite the local curb elevation.

Fixes:
1. Local curb-run stationing
   GradeGuard now stations contour controls on the curb segment/component the point actually lies on, not every selected curb segment as one artificial chain.

2. Near-point contour hold
   If a BC point is within a tight tolerance of a DXF contour, the contour is held directly at that BC station.

3. Endpoint-extension contour control
   If the curb stops short but a contour is just beyond the start/end of the curb along the curb tangent, GradeGuard projects that contour to the curb endpoint and holds that contour elevation directly.

4. Direct BC contour still does not get curb height
   Curb height is only added for selected FL/gutter/EP controls.

5. HP/LP protection remains
   BC HP and BC LP points are still hard locked and cannot be smoothed or averaged through unless a trusted HP/LP callout controls them.

Brayfield test intent:
The south curb endpoint should now see the nearby 610 contour beyond the curb end instead of being overwritten by a different 615 contour control.


v8.5.3: Surface Mode is now component-aware. Direct BC contours hold directly. EP/FL/gutter/pavement contour/TIN controls are raised by the curb height to compute BC. Default surface interpretation is Auto.
