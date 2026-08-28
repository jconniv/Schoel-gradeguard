Schoel GradeGuard - Civil 3D / AutoCAD Launcher

Installed application:
C:\Program Files\SchoelTools\SchoelGradeGuard\SchoelGradeGuard.exe

Installed LISP launcher:
C:\Program Files\SchoelTools\SchoelGradeGuard\GradeGuard.lsp

Load GradeGuard.lsp once with APPLOAD and add it to the Startup Suite.

CAD command:
SCHOELGRADEGUARD

Ribbon macro:
^C^C(if (not c:SCHOELGRADEGUARD) (load "C:/Program Files/SchoelTools/SchoelGradeGuard/GradeGuard.lsp"));SCHOELGRADEGUARD;

Diagnostics:
SCHOELGRADEGUARDTEST checks the configured EXE path.
SCHOELGRADEGUARDPATH lets the user select the EXE if GradeGuard was installed elsewhere.

Import commands supplied by the other GradeGuard LISP files:
GGIMPORTV9
GGIMPORTV93D
GGIMPORTBC
GGIMPORTBC3D
