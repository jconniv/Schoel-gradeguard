; GradeGuard v9 Civil 3D Import
; Commands:
;   GGIMPORTV9       Imports GradeGuard Confidence Summary CSV with symbols colored by QA status.
;   GGIMPORTV93D     Same, plus draws a 3D polyline through imported points.
;
; Best input:
;   GradeGuard_Confidence_Summary.csv
;
; Also works with:
;   Generated_BC_Points.csv
;
; Layers:
;   GG_GREEN_READY
;   GG_YELLOW_REVIEW
;   GG_RED_DONT_STAKE
;   GG_BC_TEXT
;   GG_BC_ELEV
;   GG_BC_3D_POLY

(defun gg:split (s delim / pos out token)
  (setq out '())
  (while (setq pos (vl-string-search delim s))
    (setq token (substr s 1 pos))
    (setq out (append out (list token)))
    (setq s (substr s (+ pos 2)))
  )
  (append out (list s))
)

(defun gg:trim (s)
  (vl-string-trim " \t\r\n\"" s)
)

(defun gg:ensure-layer (name color /)
  (if (not (tblsearch "LAYER" name))
    (command "_.-LAYER" "_M" name "_C" (itoa color) name "")
  )
)

(defun gg:make-text (pt h txt layer /)
  (entmakex
    (list
      (cons 0 "TEXT")
      (cons 8 layer)
      (cons 10 pt)
      (cons 40 h)
      (cons 1 txt)
      (cons 7 (getvar "TEXTSTYLE"))
      (cons 50 0.0)
      (cons 72 0)
      (cons 73 0)
    )
  )
)

(defun gg:make-line (p1 p2 layer /)
  (entmakex (list (cons 0 "LINE") (cons 8 layer) (cons 10 p1) (cons 11 p2)))
)

(defun gg:make-circle (pt r layer /)
  (entmakex (list (cons 0 "CIRCLE") (cons 8 layer) (cons 10 pt) (cons 40 r)))
)

(defun gg:make-symbol-at (pt scale layer status / x y z)
  (setq x (car pt))
  (setq y (cadr pt))
  (setq z (caddr pt))
  (gg:make-circle pt (* 0.35 scale) layer)
  (gg:make-line (list (- x (* 0.50 scale)) y z) (list (+ x (* 0.50 scale)) y z) layer)
  (gg:make-line (list x (- y (* 0.50 scale)) z) (list x (+ y (* 0.50 scale)) z) layer)
  (if (= status "RED")
    (gg:make-circle pt (* 0.75 scale) layer)
  )
)

(defun gg:status-layer (status)
  (cond
    ((= status "GREEN") "GG_GREEN_READY")
    ((= status "YELLOW") "GG_YELLOW_REVIEW")
    ((= status "RED") "GG_RED_DONT_STAKE")
    (T "GG_YELLOW_REVIEW")
  )
)

(defun gg:looks-like-header (line)
  (or (vl-string-search "Point" line) (vl-string-search "QA_Status" line))
)

(defun gg:import-core (make3d / file fh line vals p n e z d status layer pt label textpt elevpt points oldcmdecho symbolScale headerMode)
  (setq file (getfiled "Select GradeGuard CSV" "" "csv" 0))
  (if (not file)
    (progn (princ "\nNo file selected.") (princ))
    (progn
      (setq oldcmdecho (getvar "CMDECHO"))
      (setvar "CMDECHO" 0)

      (gg:ensure-layer "GG_GREEN_READY" 3)
      (gg:ensure-layer "GG_YELLOW_REVIEW" 2)
      (gg:ensure-layer "GG_RED_DONT_STAKE" 1)
      (gg:ensure-layer "GG_BC_TEXT" 7)
      (gg:ensure-layer "GG_BC_ELEV" 3)
      (gg:ensure-layer "GG_BC_3D_POLY" 4)

      (setq symbolScale (getreal "\nSymbol scale <1.0>: "))
      (if (not symbolScale) (setq symbolScale 1.0))

      (setq fh (open file "r"))
      (setq points '())
      (setq headerMode nil)

      (while (setq line (read-line fh))
        (if (> (strlen (vl-string-trim " \t\r\n" line)) 0)
          (progn
            (if (gg:looks-like-header line)
              (setq headerMode T)
              (progn
                (setq vals (gg:split line ","))
                (if (>= (length vals) 5)
                  (progn
                    ; Confidence Summary format:
                    ; 0 Point, 1 Northing, 2 Easting, 3 Elevation, 4 Description, 5 QA_Status
                    ; Generated BC format:
                    ; 0 Point, 1 Northing, 2 Easting, 3 Elevation, 4 Description
                    (setq p (gg:trim (nth 0 vals)))
                    (setq n (atof (gg:trim (nth 1 vals))))
                    (setq e (atof (gg:trim (nth 2 vals))))
                    (setq z (atof (gg:trim (nth 3 vals))))
                    (setq d (gg:trim (nth 4 vals)))
                    (setq status "GREEN")
                    (if (>= (length vals) 6)
                      (setq status (strcase (gg:trim (nth 5 vals))))
                    )
                    (if (not (member status '("GREEN" "YELLOW" "RED")))
                      (setq status "YELLOW")
                    )

                    (setq layer (gg:status-layer status))
                    (setq pt (list e n z))
                    (setq points (append points (list pt)))

                    (gg:make-symbol-at pt symbolScale layer status)

                    (setq label (strcat p " " d " " status))
                    (setq textpt (list (+ e (* 1.0 symbolScale)) (+ n (* 1.0 symbolScale)) z))
                    (setq elevpt (list (+ e (* 1.0 symbolScale)) (- n (* 1.0 symbolScale)) z))

                    (gg:make-text textpt (* 1.0 symbolScale) label "GG_BC_TEXT")
                    (gg:make-text elevpt (* 1.0 symbolScale) (strcat "EL " (rtos z 2 3)) "GG_BC_ELEV")
                  )
                )
              )
            )
          )
        )
      )

      (close fh)

      (if (and make3d (> (length points) 1))
        (progn
          (command "_.3DPOLY")
          (foreach pt points (command pt))
          (command "")
          (if (entlast)
            (entmod (subst (cons 8 "GG_BC_3D_POLY") (assoc 8 (entget (entlast))) (entget (entlast))))
          )
        )
      )

      (setvar "CMDECHO" oldcmdecho)
      (princ (strcat "\nGradeGuard v9 import complete. Points imported: " (itoa (length points))))
      (princ)
    )
  )
)

(defun c:GGIMPORTV9 ()
  (gg:import-core nil)
)

(defun c:GGIMPORTV93D ()
  (gg:import-core T)
)

(princ "\nGradeGuard v9 Civil 3D import loaded. Run GGIMPORTV9 or GGIMPORTV93D.")
(princ)
