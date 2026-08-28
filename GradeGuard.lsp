; ============================================================
; Schoel GradeGuard - Civil 3D / AutoCAD LISP Launcher
;
; Commands:
;   SCHOELGRADEGUARD       Launch Schoel GradeGuard
;   SCHOELGRADEGUARDPATH   Pick and save the EXE path
;   SCHOELGRADEGUARDTEST   Test the saved EXE path
;
; Ribbon macro after this file is loaded:
;   ^C^C_SCHOELGRADEGUARD;
; ============================================================

(vl-load-com)

(setq *SCHOELGRADEGUARD_REGKEY* "HKEY_CURRENT_USER\\Software\\Schoel\\GradeGuard")
(setq *SCHOELGRADEGUARD_DEFAULT_EXE* "C:\\Program Files\\SchoelTools\\SchoelGradeGuard\\SchoelGradeGuard.exe")

(defun schoelgradeguard-getpath ( / p )
  (setq p (vl-registry-read *SCHOELGRADEGUARD_REGKEY* "ExePath"))
  (if (or (null p) (= p ""))
    (setq p *SCHOELGRADEGUARD_DEFAULT_EXE*)
  )
  p
)

(defun schoelgradeguard-setpath (p)
  (vl-registry-write *SCHOELGRADEGUARD_REGKEY* "ExePath" p)
  p
)

(defun schoelgradeguard-exists (p)
  (and p (/= p "") (findfile p))
)

(defun c:SCHOELGRADEGUARDPATH ( / p )
  (setq p (getfiled "Pick SchoelGradeGuard.exe" (schoelgradeguard-getpath) "exe" 0))
  (if p
    (progn
      (schoelgradeguard-setpath p)
      (princ (strcat "\nSaved Schoel GradeGuard EXE path: " p))
    )
    (princ "\nNo EXE selected.")
  )
  (princ)
)

(defun c:SCHOELGRADEGUARDTEST ( / p )
  (setq p (schoelgradeguard-getpath))
  (princ (strcat "\nSaved EXE path: " p))
  (if (schoelgradeguard-exists p)
    (princ "\nEXE FOUND. Run SCHOELGRADEGUARD to launch.")
    (princ "\nEXE NOT FOUND. Install GradeGuard or run SCHOELGRADEGUARDPATH.")
  )
  (princ)
)

(defun c:SCHOELGRADEGUARD ( / p )
  (setq p (schoelgradeguard-getpath))
  (if (not (schoelgradeguard-exists p))
    (progn
      (alert
        (strcat
          "SchoelGradeGuard.exe was not found.\n\n"
          "Saved path:\n" p "\n\n"
          "Pick the EXE now."
        )
      )
      (c:SCHOELGRADEGUARDPATH)
      (setq p (schoelgradeguard-getpath))
    )
  )
  (if (schoelgradeguard-exists p)
    (progn
      (startapp p)
      (princ (strcat "\nLaunching Schoel GradeGuard: " p))
    )
    (alert "Still cannot find the EXE. Install Schoel GradeGuard from the signed MSI, then try again.")
  )
  (princ)
)

(princ "\nSchoel GradeGuard launcher loaded.")
(princ "\nCommands: SCHOELGRADEGUARD, SCHOELGRADEGUARDPATH, SCHOELGRADEGUARDTEST")
(princ)
