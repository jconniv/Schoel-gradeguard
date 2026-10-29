# GradeGuard 9.6.0

Start with **Open plans → Calculate → Review results**. Open plans asks for the PDF and optional CAD inputs. Calculate chooses the matching CAD or guided PDF workflow. Review results links the plan, points and profiles; choose a saved `Calculation-Review.json` when reopening a run. See `EASY-WORKFLOW-9.6.0.txt`. PDF-only calculations remain review outputs.

Use **Advanced settings** for the detailed workflow below. The CAD route uses current tool settings; the PDF route supports reusable sheet setups. The underlying formulas and limits are documented in `AUTO-CALCULATE-9.5.0.txt`.

The animated dozer opening runs for five seconds, then opens the workspace. Click **Open GradeGuard**, press **Enter**, **Space**, or **Esc**, or close the opening window to skip it. Use **Replay opening** at the upper right to watch it again without resetting your job.

1. Start GradeGuard and choose **Open job** for an existing `.ggproj`, or start a new job.
2. In **Files & setup**, select the Civil DXF, certified PDF and output folder. Add the proposed LandXML/XML surface when available. Select the intended named surface; multi-surface XML requires a choice. DXF must use US survey feet; confirm unitless DXF only after checking the drawing. For 2D CAD with 3D contours, leave the surface blank.
3. Open **Tool settings** and select **Curb staking**, **Sidewalk**, **Storm / sanitary**, or **Grade grid**.
4. Use that tool's selection buttons and confirm its settings. For curb staking, pick true BC and flowline/gutter where appropriate, confirm the PDF curb detail and surface meaning, and enable contractor offsets only when needed. Offset distance, direction and flip-side controls are in the curb page.
5. Use the generate button at the bottom right. It remains visible while the settings scroll. BC generation retains its preflight confirmation and background worker. Job actions are disabled during that BC run.
6. Open **Results & review** for layer detection, activity and output paths. Successful BC generation or a BC processing error takes you there automatically. **Open output folder** opens the selected directory once it exists.
7. Review the outputs against the certified plans. All tools exclude review-only points from field CSVs and create a clearly named DO_NOT_STAKE report. BC uses `GradeGuard_DO_NOT_STAKE.csv`. Do not import that report as staking data.

**Save job** also retains surface selection, unit confirmation, sidewalk and structure settings. Changed drawings and old selections require review and re-picking. Reruns create a dated subfolder when prior GradeGuard outputs exist. See `CALCULATION-FIXES-9.3.1.txt` for corrected calculations and intentional limits.

File status checks only the path and extension. A found file is not a validated survey input. The existing US survey-foot workflow, coordinate references, control, benchmark, and curb detail still need office confirmation.

PDF grade review: In Project files, choose the PDF and matching DXF, then click **Read PDF grades / review curb controls**. Filter high/low or curb labels and inspect each callout on its sheet. Confirm the legend meaning, actual CAD curb E/N, local rise if converting a lower grade, units and vertical reference. Accept each control, save the control file, then save the job. Use OCR this page or Key a missed callout when text reading misses a label. OCR can misread digits; no observation becomes a control automatically. See PDF-REVIEW-9.4.0.txt for details.
