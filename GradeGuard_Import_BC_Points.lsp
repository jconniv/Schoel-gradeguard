; GradeGuard Import BC Points
; Commands:
;   GGIMPORTBC     Imports GradeGuard CSV points as AutoCAD POINT objects and text.
;   GGIMPORTBC3D   Imports GradeGuard CSV points and draws a 3D polyline through them.
;
; Expected CSV format with no header:
;   Point,Northing,Easting,Elevation,Description
;
; Example:
;   6000,12384.658,13819.540,615.838,BC PT

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

(defun gg:make-text (pt h txt layer / ent)
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

(defun gg:import-core (make3d / file fh line vals p n e z d pt label textpt elevpt points oldcmdecho)
  (setq file (getfiled "Select GradeGuard CSV" "" "csv" 0))
  (if (not file)
    (progn (princ "\nNo file selected.") (princ))
    (progn
      (setq oldcmdecho (getvar "CMDECHO"))
      (setvar "CMDECHO" 0)

      (gg:ensure-layer "GG_BC_POINTS" 2)
      (gg:ensure-layer "GG_BC_TEXT" 7)
      (gg:ensure-layer "GG_BC_ELEV" 3)
      (gg:ensure-layer "GG_BC_3D_POLY" 4)

      (setvar "PDMODE" 35)
      (setvar "PDSIZE" 1.0)

      (setq fh (open file "r"))
      (setq points '())

      (while (setq line (read-line fh))
        (if (> (strlen (vl-string-trim " \t\r\n" line)) 0)
          (progn
            (setq vals (gg:split line ","))
            (if (>= (length vals) 5)
              (progn
                (setq p (gg:trim (nth 0 vals)))
                (setq n (atof (gg:trim (nth 1 vals))))
                (setq e (atof (gg:trim (nth 2 vals))))
                (setq z (atof (gg:trim (nth 3 vals))))
                (setq d (gg:trim (nth 4 vals)))

                ; AutoCAD point uses X,Y,Z. GradeGuard CSV is Point, Northing, Easting, Elevation, Description.
                (setq pt (list e n z))
                (setq points (append points (list pt)))

                (entmakex (list (cons 0 "POINT") (cons 8 "GG_BC_POINTS") (cons 10 pt)))

                (setq label (strcat p " " d))
                (setq textpt (list (+ e 1.0) (+ n 1.0) z))
                (setq elevpt (list (+ e 1.0) (- n 1.0) z))

                (gg:make-text textpt 1.0 label "GG_BC_TEXT")
                (gg:make-text elevpt 1.0 (strcat "EL " (rtos z 2 3)) "GG_BC_ELEV")
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
      (princ (strcat "\nGradeGuard import complete. Points imported: " (itoa (length points))))
      (princ)
    )
  )
)

(defun c:GGIMPORTBC ()
  (gg:import-core nil)
)

(defun c:GGIMPORTBC3D ()
  (gg:import-core T)
)

(princ "\nGradeGuard LISP loaded. Run GGIMPORTBC or GGIMPORTBC3D.")
(princ)
