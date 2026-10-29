# Animated opening, version 9.3.0

The opening scene uses a native Tk canvas: a yellow tracked dozer crosses the screen, its wheels and track pads rotate, dust trails behind it, and its blade pushes soil ahead while leaving a level grade line. The approved GradeGuard logo remains unchanged. The survey grid and contours are decorative, not project geometry or survey results.

The scene runs for five seconds, then reveals the existing workspace. Enter, Space, Escape, the Open GradeGuard button, or closing the intro skip it immediately. Replay opening is available from the main header and preserves the current job. It reuses an already-open intro rather than opening duplicates. Timers are cancelled when the intro closes. If the intro cannot start, the workspace is revealed and the error is recorded in the activity log.

Source: `gradeguard_splash.py`. Integration is limited to application startup and the replay button. No calculation, import/export, project-schema, unit, datum, tolerance or default change is included. No new package dependencies, internet access, video player or external animation files are needed.

Run `python -m unittest discover -s tests -v` and `python -m py_compile schoel_gradeguard.py gradeguard_ui.py gradeguard_splash.py`. Lifecycle tests cover travel across the scene, bounded canvas-item counts, skip/timeout completion, cancellation of timers, preserving job settings during replay, startup visibility and the failure fallback.

The 9.2.0 working source is preserved in `C:\SchoelTools\archive\GradeGuard\2026-10-28\05-Workflow-Preview-9.2.0`. The preceding executable is retained there after verifying the new build. Current launch shortcuts target the 9.3.0 executable.

Verified on 2026-10-28: all 19 tests passed; Python compilation and the Windows executable build passed. The actual opening artwork, skip-to-workspace behavior, packaged dozer scene and automatic transition into the workspace were checked in Windows. A comparison with the saved 9.2.0 source confirmed that 202 existing functions/methods are unchanged; only startup initialization changed and the replay method was added. The delivered executable and active executable have matching SHA-256 hashes.

The build still reports the existing optional hidden-import warnings for `scipy.special._cdflib`, `pysqlite2`, `MySQLdb` and `psycopg2`. This remains an unsigned preview with the existing office-validation requirements. The decorative opening does not validate any survey data.
