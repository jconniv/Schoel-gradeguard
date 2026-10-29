"""Optional smoke test using installed AutoCAD Core Console and a synthetic DXF.

python tests/check_cad_import.py --report <result.txt>
Does not attach to, save, or change the user's open Civil 3D drawing.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import ezdxf

parser=argparse.ArgumentParser()
parser.add_argument('--engine',default=r'C:\Program Files\Autodesk\AutoCAD 2025\accoreconsole.exe')
parser.add_argument('--report',required=True)
args=parser.parse_args()
repo=Path(__file__).resolve().parents[1]
first=repo/'GradeGuard_v9_Civil3D_Import.lsp'
assert first.read_bytes()==(repo/'GradeGuard_Import_BC_Points.lsp').read_bytes()
with tempfile.TemporaryDirectory(prefix='gradeguard-cad-test-') as folder:
    root=Path(folder)
    drawing=root/'blank.dxf'; ezdxf.new('R2010').saveas(drawing)
    fixture=root/'points.csv'
    fixture.write_text('Point,Northing,Easting,Elevation,Description,QA_Status\n'
                       '6000,100,200,612.34,"BC, PC",GREEN\n'
                       '6001,110,210,613.25,BC,YELLOW\n'
                       '6002,120,220,,BC,RED\n'
                       '6003,130,230,0,BC,GREEN\n'
                       '6004,140,240,622.50,BC,RED\n'
                       '6005,150,NaN,623.50,BC,GREEN\n'
                       '6006,160,260,624.50,BC,UNKNOWN\n'
                       '6007,170,270,not-a-number,BC,GREEN\n')
    result=root/'result.txt'; script=root/'test.scr'
    script.write_text(f'''(load "{first.as_posix()}")
(defun getfiled (title default ext flags) "{fixture.as_posix()}")
(c:GGIMPORTV93D)
(setq pts (ssget "_X" '((0 . "POINT"))))
(setq polys (ssget "_X" '((0 . "POLYLINE,LWPOLYLINE"))))
(setq report (open "{result.as_posix()}" "w"))
(write-line (strcat "POINT_COUNT=" (itoa (if pts (sslength pts) 0))) report)
(write-line (strcat "POLYLINE_COUNT=" (itoa (if polys (sslength polys) 0))) report)
(write-line (strcat "INVALID_NUMBERS=" (vl-princ-to-string (mapcar 'gg931:number '("" "NaN" "1.2.3" "5x" "--5")))) report)
(write-line (strcat "QUOTED_COMMA=" (if (equal (gg931:csv "1,2,3,600,\\"BC, PC\\"") '("1" "2" "3" "600" "BC, PC")) "PASS" "FAIL")) report)
(if pts (repeat (setq idx (sslength pts)) (setq idx (1- idx)) (write-line (strcat "POINT=" (vl-princ-to-string (cdr (assoc 10 (entget (ssname pts idx)))))) report)))
(close report)
_.QUIT
_Y
''',encoding='utf-8')
    run=subprocess.run([args.engine,'/i',str(drawing),'/s',str(script),'/l','en-US'],cwd=root,capture_output=True,timeout=40)
    log=run.stdout.decode('utf-16-le',errors='replace')
    output=result.read_text() if result.exists() else ''
    expected=['POINT_COUNT=2','POLYLINE_COUNT=0','INVALID_NUMBERS=(nil nil nil nil nil)','QUOTED_COMMA=PASS',
              'POINT=(200.0 100.0 612.34)','POINT=(210.0 110.0 613.25)']
    success=run.returncode==0 and all(value in output for value in expected)
    report=Path(args.report);report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(('PASS' if success else 'FAIL')+' - AutoCAD 2025 Core Console synthetic import\n'+output+'\nEngine output:\n'+log,encoding='utf-8')
    assert success, f'Import smoke test failed; see {report}'
    print(output)
