"""Read-only known-answer checks on public PDFs, downloaded separately.

These selected callouts were visually checked on 2026-10-28. They do not measure
complete recall, interpretation accuracy, registration, or survey accuracy.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from gradeguard_pdf import scan_pdf, fingerprint, export_review

parser=argparse.ArgumentParser()
parser.add_argument('--corpus',required=True,type=Path)
parser.add_argument('--report',required=True,type=Path)
args=parser.parse_args()
expected_hashes={
 'mesa-verde':'1220269b094a0e97d9cbbd5d6978fe8c04e7fb7cc354cc16e6512988bf4ab236',
 'north-royalton':'59a914668f3cf470a4e0d2736115fabdc18a2d5b44989055b68d91e0a2779837'}
answers=[(2,'HPS',141.30),(2,'LPS',141.55),(2,'FS',141.80),
         (3,'TC',142.43),(3,'TC/FS',142.45),(3,'RIM/FS',141.93),
         (3,'HP',142.23),(3,'FS',142.18),(4,'TC',151.97),(4,'FS',151.02)]
report={'scope':'Selected visually checked callouts only; not complete plan recognition or field validation.', 'files':[], 'checks':[]}
for name,expected_hash in expected_hashes.items():
 path=args.corpus/(name+'.pdf')
 assert fingerprint(path)==expected_hash,'Public source changed: '+name
 rows,info=scan_pdf(path)
 report['files'].append(dict(name=name,sha256=expected_hash,pages=len(info['pages']),
                             observations=len(rows),labels=dict(Counter(r.label for r in rows))))
 args.report.parent.mkdir(parents=True,exist_ok=True)
 export_review(args.report.parent/(name+'-observations-REVIEW_ONLY.csv'),rows)
 if name=='mesa-verde':
  for page,label,value in answers:
   count=sum((r.page,r.label,r.elevation)==(page,label,value) for r in rows)
   report['checks'].append(dict(page=page,label=label,expected=value,matches=count,status='PASS' if count else 'FAIL'))
report['pass']=sum(c['status']=='PASS' for c in report['checks'])
report['fail']=len(report['checks'])-report['pass']
args.report.write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
sys.exit(bool(report['fail']))
