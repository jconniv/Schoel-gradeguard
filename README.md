# Schoel GradeGuard

Schoel GradeGuard is an internal survey-office application for producing and reviewing curb, sidewalk, grade-grid, and structure staking outputs from approved local DXF, LandXML, and civil PDF inputs.

## Status

- Current version: 9.0.4
- Status: pre-release validation
- Intended users: Schoel Engineering survey-office personnel
- Maintainer: Joseph Conniv

GradeGuard output requires office review and appropriate licensed-surveyor validation. The software does not replace professional judgment or the certified plans.

## Run from source

1. Install Python 3 and the packages in `requirements.txt`.
2. Run `python schoel_gradeguard.py` or `run_app.bat`.

## Build the Windows application

Run `build_exe.bat`. Generated executables and build caches are intentionally excluded from source control.

For a signed release, run `Build_Signed_MSI.ps1`. It builds the EXE and MSI, signs both from the approved Windows certificate store, verifies both signatures, and writes a SHA-256 release manifest. Private certificate files and passwords are never stored in this repository.

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

See `CHANGELOG.md` and `V9_0_SURVEY_PACKAGE_MODE_NOTES.txt` for release history and current behavior.
