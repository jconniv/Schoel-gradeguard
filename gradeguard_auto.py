"""Automatic packages and calibrated PDF calculations. No source file is edited."""
import csv
from datetime import datetime
import html
import json
import math
from pathlib import Path
import tempfile

from gradeguard_pdf import fingerprint


def number(value, name, positive=False, nonnegative=False):
    value=float(value)
    if not math.isfinite(value) or (positive and value <= 0) or (nonnegative and value < 0):
        raise ValueError(name+' must be a finite '+('positive' if positive else 'nonnegative' if nonnegative else '')+' number.')
    return value


def new_run(folder, prefix):
    root=Path(folder)
    if not str(folder).strip(): raise ValueError('Choose an output folder.')
    root.mkdir(parents=True,exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix+'-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-',dir=root))


def summary_report(folder, manifest):
    (folder/'Calculation-Summary.json').write_text(json.dumps(manifest,indent=2,allow_nan=False),encoding='utf-8')
    rows=[]
    for item in manifest['tools']:
        rows.append('<tr>'+''.join('<td>'+html.escape(str(item.get(k,'')))+'</td>' for k in ('tool','status','total','calculated','withheld','note'))+'</tr>')
    page='''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GradeGuard automatic calculation</title><style>body{font:16px/1.5 system-ui,sans-serif;color:#173746;max-width:1100px;margin:32px auto;padding:20px}h1{border-bottom:5px solid #d6b65f}table{border-collapse:collapse;width:100%}th,td{padding:12px;border-bottom:1px solid #cddade;text-align:left}th{background:#e4edef}p{max-width:950px}</style><h1>GradeGuard automatic calculation</h1><p>__NOTE__</p><table><thead><tr><th>Tool</th><th>Status</th><th>Points</th><th>Calculated</th><th>Withheld</th><th>Details</th></tr></thead><tbody>__ROWS__</tbody></table><p>See Calculation-Summary.json for source identity and each tool folder for results and source records. Missing elevations are never replaced with zero.</p></html>'''
    path=folder/'Calculation-Summary.html'
    path.write_text(page.replace('__NOTE__',html.escape(manifest['note'])).replace('__ROWS__',''.join(rows)),encoding='utf-8')
    return str(path)


def run_cad_package(job, enabled=('curb','sidewalk','grade'), progress=None):
    """Run existing checked engines sequentially because they share geometry state."""
    import schoel_gradeguard as gg
    enabled=list(dict.fromkeys(enabled))
    if not enabled or any(k not in ('curb','sidewalk','grade') for k in enabled):
        raise ValueError('Choose curb, sidewalk and/or grade calculations.')
    cad=job['cad_path']; pdf=job.get('pdf_path',''); surface=job.get('surface_path','')
    gg.validate_generation_inputs(cad,units_confirmed=job.get('unitless_us_survey_feet_confirmed',False))
    if not Path(pdf).is_file() or Path(pdf).suffix.lower()!='.pdf': raise ValueError('Choose the matching plan PDF.')
    import fitz
    with fitz.open(pdf) as plan:
        if plan.needs_pass or not len(plan): raise ValueError('Choose an accessible plan PDF with at least one sheet.')
    start=number(job.get('start_point',6000),'Start point',positive=True)
    if not start.is_integer(): raise ValueError('Start point must be an integer.')
    start=int(start)
    source_paths=[cad,pdf]+([surface] if surface else [])
    hashes={str(p):fingerprint(p) for p in source_paths}
    root=new_run(job['out_dir'],'Auto-CAD')
    manifest=dict(version=gg.APP_VERSION,workflow='DXF + PDF',sources=hashes,settings=job,tools=[],
                  note='Automatic curb, sidewalk and grade calculations. Combined_Approved_Field_Points.csv contains only points accepted by the existing field-export gate. Review every tool report.')
    common=dict(cad_path=cad,surface_path=surface,surface_name=job.get('surface_name',''),
                units_confirmed=job.get('unitless_us_survey_feet_confirmed',False),
                elev_layer_keywords=job.get('elevation_layer_keywords','CONTOUR,SURFACE'),
                contour_radius=number(job.get('contour_radius',50),'Contour radius',nonnegative=True))
    approved=[]
    for tool in enabled:
        if progress: progress('Calculating '+tool+'...')
        folder=root/tool
        try:
            if any(fingerprint(p)!=digest for p,digest in hashes.items()):
                raise ValueError('A source file changed during the run; rerun with stable inputs.')
            if tool=='curb':
                result=gg.run_generation(**common,pdf_path=pdf,out_dir=str(folder),start_pt=start,
                    spacing=number(job.get('spacing',25),'Curb spacing',positive=True),
                    default_curb_height=number(job.get('curb_height',.5),'Curb height',nonnegative=True),
                    curb_layer_keywords=job.get('curb_layer_keywords','P-BC,BC,CURB'),
                    label_radius=number(job.get('label_radius',5),'Label radius',nonnegative=True),
                    min_slope=number(job.get('min_slope',.003),'Minimum slope',nonnegative=True),
                    contour_check_tolerance=number(job.get('contour_warning_tolerance',.15),'Contour tolerance',nonnegative=True),
                    manual_anchor_path=job.get('anchor_path',''),selected_curb_path=job.get('selected_curb_path',''),
                    selected_flowline_path=job.get('selected_flowline_path',''),source_policy=job.get('source_mode','surface'),
                    surface_mode=job.get('surface_interpretation','bc'),create_offset=job.get('create_offset',False),
                    offset_distance=number(job.get('offset_distance',3),'Offset distance',nonnegative=True),
                    offset_mode=job.get('offset_mode','away_from_flowline'),offset_reverse=job.get('offset_reverse',False))
                field=result[0]; total=result[3]
            elif tool=='sidewalk':
                result=gg.run_edge_sidewalk_generation(**common,pdf_path=pdf,out_dir=str(folder),start_pt=start,
                    spacing=number(job.get('sidewalk_spacing',25),'Sidewalk spacing',positive=True),
                    description=job.get('sidewalk_description','SIDEWALK'),selected_sidewalk_path=job.get('selected_sidewalk_path',''),
                    cross_slope_percent=number(job.get('sidewalk_cross_slope',0),'Cross slope',nonnegative=True),
                    sidewalk_width=number(job.get('sidewalk_width',0),'Sidewalk width',nonnegative=True),
                    cross_slope_adjust=job.get('sidewalk_adjustment','direct'),auto_detect_width=job.get('sidewalk_auto_width',True))
                field=result[0]; total=result[3]
            else:
                sub=job.get('grade_grid_type','FINISHED GRADE').upper()=='SUBGRADE'
                result=gg.run_grade_grid_generation(**common,out_dir=str(folder),start_pt=start,
                    grid_spacing=number(job.get('grade_grid_spacing',25),'Grid spacing',positive=True),
                    subtract_amount=number(job.get('grade_grid_subtract',0),'Subgrade deduction',nonnegative=True) if sub else 0,
                    description='SUBGRADE' if sub else 'FINISHED GRADE',selected_curb_path=job.get('selected_curb_path',''))
                field=result[0]; total=result[2]
            with open(field,newline='',encoding='utf-8') as stream: rows=list(csv.reader(stream))
            tool_approved=list(rows)
            start+=int(total)
            package_total=total
            if tool=='curb' and job.get('create_offset',False):
                # Reserve every offset ID, including withheld points, before the next tool.
                offset_debug=folder/'Generated_BC_Offset_Points_with_sources.csv'
                with offset_debug.open(newline='',encoding='utf-8') as stream:
                    offset_rows=list(csv.reader(stream))[1:]
                if offset_rows: start=max(start,max(int(r[0]) for r in offset_rows)+1)
                package_total+=len(offset_rows)
                offset_field=folder/'Generated_BC_Offset_Points.csv'
                with offset_field.open(newline='',encoding='utf-8') as stream: offset_approved=list(csv.reader(stream))
                tool_approved.extend(offset_approved)
                manifest['offset_outputs']=[str(offset_field),str(offset_debug)]
            approved.extend(tool_approved)
            manifest['tools'].append(dict(tool=tool,status='Complete' if tool_approved else 'Review required',total=package_total,
                calculated=len(tool_approved),withheld=package_total-len(tool_approved),field_csv=field,note='Includes enabled curb offsets. See tool source and DO_NOT_STAKE reports.' if tool=='curb' else 'See tool source and DO_NOT_STAKE reports.'))
        except Exception as error:
            manifest['tools'].append(dict(tool=tool,status='Failed',total=0,calculated=0,withheld=0,note=str(error)))
    if any(fingerprint(p)!=digest for p,digest in hashes.items()):
        raise ValueError('A source changed during calculation. Discard this run and rerun with stable inputs.')
    ids=[r[0] for r in approved]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate point numbers in the combined package; individual outputs require review.')
    with (root/'Combined_Approved_Field_Points.csv').open('w',newline='',encoding='utf-8') as stream: csv.writer(stream).writerows(approved)
    manifest['approved_total']=len(approved)
    from gradeguard_review import merge_reviews
    review_files=[str(root/tool/'Calculation-Review.json') for tool in enabled if (root/tool/'Calculation-Review.json').is_file() and not any(t['tool']==tool and t['status']=='Failed' for t in manifest['tools'])]
    manifest['review_file']=merge_reviews(review_files,root)
    report=summary_report(root,manifest)
    return dict(folder=str(root),report=report,manifest=manifest)


def calibrate(spec):
    """Similarity from PDF display coordinates; Y is inverted before fitting."""
    mode=spec.get('mode')
    pairs=spec.get('points',[])
    if len(pairs)<2: raise ValueError('Pick two calibration points.')
    p=[complex(number(q['pdf'][0],'PDF X'),-number(q['pdf'][1],'PDF Y')) for q in pairs]
    if abs(p[1]-p[0])<1e-8: raise ValueError('Calibration points must be different.')
    if mode=='local':
        distance=number(spec.get('length'),'Known plan distance',positive=True)
        a=distance/(p[1]-p[0]); b=-a*p[0]
        check=None
    elif mode=='survey':
        if len(pairs)<3: raise ValueError('Survey calibration requires two fits and an independent check point.')
        q=[complex(number(v['xy'][0],'Easting'),number(v['xy'][1],'Northing')) for v in pairs]
        if abs(q[1]-q[0])<1e-8: raise ValueError('Survey control coordinates must be different.')
        a=(q[1]-q[0])/(p[1]-p[0]); b=q[0]-a*p[0]
        relative=(p[2]-p[0])/(p[1]-p[0])
        if abs(relative.imag)<1e-5: raise ValueError('Use an independent check away from the calibration baseline.')
        check=abs(a*p[2]+b-q[2])
        tolerance=number(spec.get('tolerance'),'Independent check tolerance',positive=True)
        if check>tolerance: raise ValueError(f'Independent check residual {check:.4f} ft exceeds {tolerance:.4f} ft.')
    else: raise ValueError('Choose local plan coordinates or survey coordinates.')
    if spec.get('units')!='US survey feet': raise ValueError('Confirm US survey feet; no unit or datum conversion is performed.')
    if not str(spec.get('reference','')).strip(): raise ValueError('Record the scale/control and vertical reference checked.')
    return dict(a=[a.real,a.imag],b=[b.real,b.imag],mode=mode,scale=abs(a),check_residual=check)


def transform(model, point):
    a=complex(*model['a']); b=complex(*model['b'])
    q=a*complex(number(point[0],'PDF X'),-number(point[1],'PDF Y'))+b
    return (q.real,q.imag)


def distance(a,b): return math.hypot(a[0]-b[0],a[1]-b[1])


def path_stations(points):
    result=[0.0]
    for a,b in zip(points,points[1:]):
        length=distance(a,b)
        if length <= 1e-9: raise ValueError('Remove repeated consecutive path vertices.')
        result.append(result[-1]+length)
    if len(points)<2: raise ValueError('A path needs at least two points.')
    return result


def project_to_path(point,points,stations):
    matches=[]
    for i,(a,b) in enumerate(zip(points,points[1:])):
        vx,vy=b[0]-a[0],b[1]-a[1]
        t=max(0.,min(1.,((point[0]-a[0])*vx+(point[1]-a[1])*vy)/(vx*vx+vy*vy)))
        q=(a[0]+t*vx,a[1]+t*vy)
        matches.append((distance(point,q),stations[i]+t*(stations[i+1]-stations[i]),q))
    return min(matches,key=lambda x:x[0])


def point_at_station(station,points,stations):
    for i,(lo,hi) in enumerate(zip(stations,stations[1:])):
        if station<=hi+1e-9:
            t=(station-lo)/(hi-lo)
            return (points[i][0]+t*(points[i+1][0]-points[i][0]),points[i][1]+t*(points[i+1][1]-points[i][1]))
    return points[-1]


def interpolate_profile(anchors,station,closed_length=None):
    anchors=sorted(anchors,key=lambda a:a[0])
    for a,b in zip(anchors,anchors[1:]):
        if abs(a[0]-b[0])<=1e-7 and abs(a[1]-b[1])>1e-7:
            raise ValueError('Conflicting grades at one controlling station.')
    for s,z,source in anchors:
        if abs(station-s)<=1e-7: return z,'Held '+source
    if closed_length and anchors:
        anchors=[(anchors[-1][0]-closed_length,*anchors[-1][1:])]+anchors+[(anchors[0][0]+closed_length,*anchors[0][1:])]
    for a,b in zip(anchors,anchors[1:]):
        if a[0]<station<b[0]:
            t=(station-a[0])/(b[0]-a[0])
            return a[1]+t*(b[1]-a[1]),f'Linear station interpolation: {a[2]} -> {b[2]}'
    return None,'No grade controls bracket this station.'


def inside_polygon(point,polygon):
    x,y=point; inside=False
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        dx,dy=b[0]-a[0],b[1]-a[1]
        if abs(dx*(y-a[1])-dy*(x-a[0]))<1e-8 and min(a[0],b[0])-1e-8<=x<=max(a[0],b[0])+1e-8 and min(a[1],b[1])-1e-8<=y<=max(a[1],b[1])+1e-8:
            return True
        if (a[1]>y)!=(b[1]>y) and x < a[0]+(y-a[1])*dx/dy: inside=not inside
    return inside


def polygon_valid(poly):
    import schoel_gradeguard as gg
    if len(poly)<3: raise ValueError('Draw a grade boundary with at least three vertices.')
    edges=list(zip(poly,poly[1:]+poly[:1]))
    area=abs(sum(a[0]*b[1]-a[1]*b[0] for a,b in edges))/2
    if area<=1e-8: raise ValueError('Grade boundary has zero area.')
    for i,(a,b) in enumerate(edges):
        if distance(a,b)<1e-8: raise ValueError('Grade boundary has a repeated vertex.')
        for j,(c,d) in enumerate(edges):
            if j<=i+1 or (i==0 and j==len(edges)-1): continue
            if gg.segment_intersection(a,b,c,d): raise ValueError('Grade boundary crosses itself.')


def pdf_surface(controls,boundary):
    """Review TIN only. A set of spots does not define approved design breaklines."""
    import numpy as np
    from scipy.spatial import Delaunay
    import schoel_gradeguard as gg
    unique={}
    for c in controls:
        if c['kind']!='FG': continue
        xy=tuple(c['xy'])
        if not inside_polygon(xy,boundary): continue
        if xy in unique and abs(unique[xy]-c['z'])>1e-7: raise ValueError('Conflicting finished grades at the same point.')
        unique[xy]=c['z']
    if len(unique)<3: raise ValueError('At least three located finished-grade controls inside the boundary are needed for a review grid.')
    xy=np.array(list(unique)); zs=np.array(list(unique.values()))
    try: triangulation=Delaunay(xy)
    except Exception as error: raise ValueError('Finished-grade controls cannot form a surface; check for a straight line or duplicates.') from error
    triangles=[]
    for ids in triangulation.simplices:
        vertices=[tuple(xy[i]) for i in ids]
        samples=[tuple(sum(v[j] for v in vertices)/3 for j in (0,1))]
        samples.extend(((a[0]+b[0])/2,(a[1]+b[1])/2) for a,b in zip(vertices,vertices[1:]+vertices[:1]))
        # A triangle edge may not cross a boundary in its interior.
        crosses=False
        for a,b in zip(vertices,vertices[1:]+vertices[:1]):
            for c,d in zip(boundary,boundary[1:]+boundary[:1]):
                hit=gg.segment_intersection(a,b,c,d)
                if hit and 1e-7<hit[0]<1-1e-7 and 1e-7<hit[1]<1-1e-7: crosses=True
        if not crosses and all(inside_polygon(p,boundary) for p in samples):
            triangles.append(tuple((float(xy[i][0]),float(xy[i][1]),float(zs[i])) for i in ids))
    if not triangles: raise ValueError('No review triangles fit inside the selected boundary.')
    return gg.LandXMLSurface(triangles,source='PDF finished-grade review triangulation')


def calculate_pdf_job(job, output, progress=None):
    """Calculate all intermediate values from explicit sheet geometry/controls.

    PDF geometry and interpolation remain review products, even with survey
    coordinates. Never write a field-approved CSV from this inferred model.
    """
    import schoel_gradeguard as gg
    if job.get('schema')!='gradeguard.pdf-calculation.v1': raise ValueError('Unsupported PDF calculation job.')
    if fingerprint(job['pdf_path'])!=job['pdf_sha256']: raise ValueError('PDF changed; review the sheet setup again.')
    model=calibrate(job['calibration'])
    spacing=number(job['spacing'],'Path spacing',positive=True)
    grid_spacing=number(job['grid_spacing'],'Grid spacing',positive=True)
    snap=number(job['association_tolerance'],'Grade-to-path tolerance',positive=True)
    subtract=number(job.get('subgrade_depth',0),'Subgrade deduction',nonnegative=True)
    start=number(job.get('start_point',6000),'Start point',positive=True)
    if not start.is_integer(): raise ValueError('Start point must be an integer.')
    root=new_run(output,'Auto-PDF')
    controls=[]
    path_ids=[p['id'] for p in job.get('paths',[])]
    if len(path_ids)!=len(set(path_ids)): raise ValueError('Path names must be unique.')
    for c in job.get('controls',[]):
        kind=c['kind']
        if kind not in ('CURB_TOP','CURB_FL','SIDEWALK','FG'): raise ValueError('Choose each grade control meaning from the plan legend.')
        if kind!='FG' and c.get('path_id') not in path_ids: raise ValueError('Assign each curb/sidewalk control to an existing path.')
        z=number(c['z'],'Printed elevation')
        if kind=='CURB_FL': z+=number(c.get('rise'),'Local curb rise',nonnegative=True)
        controls.append(dict(c,xy=transform(model,c['pdf']),printed_z=number(c['z'],'Printed elevation'),z=z))
    rows=[]; items=[]
    for path in job.get('paths',[]):
        if progress: progress('Calculating '+path['id']+'...')
        try:
            if path['kind'] not in ('CURB','SIDEWALK'): raise ValueError('Unknown path type.')
            points=[transform(model,p) for p in path['points']]
            stations=path_stations(points); length=stations[-1]
            if length/spacing>25000: raise ValueError('Path would exceed 25,000 stations. Increase spacing.')
            closed=distance(points[0],points[-1])<1e-8
            anchors=[]
            for c in controls:
                if c.get('path_id')!=path['id']: continue
                if (path['kind']=='CURB' and c['kind'] not in ('CURB_TOP','CURB_FL')) or (path['kind']=='SIDEWALK' and c['kind']!='SIDEWALK'):
                    raise ValueError('Control meaning does not match its selected path.')
                separation,s,_=project_to_path(c['xy'],points,stations)
                if separation>snap: raise ValueError(f'Control {c["id"]} is {separation:.3f} ft from its path; verify its tick.')
                if closed and abs(s-length)<1e-7: s=0.0
                anchors.append((s,c['z'],c['id']+' '+c.get('role','Spot')))
            distinct=[]
            for anchor in sorted(anchors):
                if distinct and abs(anchor[0]-distinct[-1][0])<1e-7:
                    if abs(anchor[1]-distinct[-1][1])>1e-7: raise ValueError('Conflicting grades at one controlling station.')
                else: distinct.append(anchor)
            anchors=distinct
            samples=set(stations+[a[0] for a in anchors])
            samples.update(i*spacing for i in range(int(length/spacing)+1))
            if closed: samples.discard(length)
            pending=[]
            for station in sorted(samples):
                z,source=interpolate_profile(anchors,station,length if closed and len(anchors)>1 else None)
                xy=point_at_station(station,points,stations)
                from gradeguard_review import profile_evidence
                detail=profile_evidence(anchors,station,z,controls,length if closed and len(anchors)>1 else None)
                pending.append(dict(tool=path['kind'],path=path['id'],station=station,x=xy[0],y=xy[1],elevation=z,detail=detail,
                    source=source,status='CALCULATED_REVIEW' if z is not None else 'MISSING_GRADE'))
            for i,row in enumerate(pending):
                row['slope_to_next_percent']=''
                if i+1<len(pending): following=pending[i+1]; ds=following['station']-row['station']
                elif closed: following=pending[0]; ds=length-row['station']
                else: continue
                if ds>1e-9 and row['elevation'] is not None and following['elevation'] is not None:
                    row['slope_to_next_percent']=100*(following['elevation']-row['elevation'])/ds
            rows.extend(pending)
            calculated=sum(r['elevation'] is not None for r in pending)
            items.append(dict(tool=path['id'],status='Calculated for review',total=len(pending),calculated=calculated,withheld=len(pending)-calculated,
                              note='Station interpolation along selected linework; no extrapolation.'))
        except Exception as error:
            items.append(dict(tool=path['id'],status='Failed',total=0,calculated=0,withheld=0,note=str(error)))
    if job.get('calculate_grid',True):
        try:
            boundary=[transform(model,p) for p in job.get('boundary',[])]
            polygon_valid(boundary)
            surface=pdf_surface(controls,boundary)
            lo=(min(p[0] for p in boundary),min(p[1] for p in boundary)); hi=(max(p[0] for p in boundary),max(p[1] for p in boundary))
            nx=int((hi[0]-lo[0])/grid_spacing)+1; ny=int((hi[1]-lo[1])/grid_spacing)+1
            if nx*ny>25000: raise ValueError('Grid would exceed 25,000 points; increase spacing or reduce the boundary.')
            pending=[]
            for iy in range(ny):
                for ix in range(nx):
                    xy=(lo[0]+ix*grid_spacing,lo[1]+iy*grid_spacing)
                    if not inside_polygon(xy,boundary): continue
                    # The shared sampler can return review extrapolations by default.
                    # A PDF grid must stop at the actual control triangles.
                    z,_,confidence=gg.landxml_elevation_at(xy,surface,search_pad=0)
                    if confidence!='SURFACE_HIGH': z=None
                    from gradeguard_review import surface_evidence
                    detail=surface_evidence(xy,surface,z,subtract) if z is not None else dict(method='Unresolved',formula='No control triangle covers this grid location.',controls=[])
                    if z is not None: z-=subtract
                    pending.append(dict(tool='SUBGRADE' if subtract else 'FINISHED_GRADE',path='GRID',station='',x=xy[0],y=xy[1],
                        elevation=z,source=f'PDF spot triangulation; no designed breaklines; deduction {subtract:.4f} ft',
                        status='TIN_REVIEW' if z is not None else 'OUTSIDE_CONTROL_SURFACE',detail=detail))
            rows.extend(pending)
            calculated=sum(r['elevation'] is not None for r in pending)
            items.append(dict(tool='Grade grid',status='Calculated for review',total=len(pending),calculated=calculated,withheld=len(pending)-calculated,
                              note='Clipped to boundary/control surface. Inferred triangles require comparison to design; no field approval.'))
        except Exception as error:
            items.append(dict(tool='Grade grid',status='Failed',total=0,calculated=0,withheld=0,note=str(error)))
    if not items: raise ValueError('Select at least one curb/sidewalk path or enable a grade grid.')
    if fingerprint(job['pdf_path'])!=job['pdf_sha256']: raise ValueError('PDF changed during calculation; review the sheet setup again.')
    for index,row in enumerate(rows): row['point']=int(start)+index
    frame='LOCAL_PLAN_XY' if model['mode']=='local' else 'SURVEY_XY_PDF_DERIVED'
    fieldnames=['point','tool','path','station','x','y','elevation','slope_to_next_percent','status','source']
    csv_path=root/f'{frame}_CALCULATED_REVIEW_ONLY.csv'
    with csv_path.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fieldnames,extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
    (root/'PDF-Calculation-Setup.json').write_text(json.dumps(job,indent=2,allow_nan=False),encoding='utf-8')
    manifest=dict(version=gg.APP_VERSION,workflow='PDF only',coordinates=frame,calibration=model,tools=items,
                  source_sha256=job['pdf_sha256'],csv=str(csv_path),
                  note='PDF-derived calculated review points only. Local coordinates are not survey Easting/Northing. Survey calibration does not certify traced geometry or inferred grades. Curb and sidewalk are station-interpolated; grid triangles do not incorporate design breaklines. No field-approved CSV is created.')
    from gradeguard_review import pdf_review,save_review
    manifest['review_file']=save_review(root,pdf_review(job,rows,model))
    report=summary_report(root,manifest)
    return dict(folder=str(root),report=report,manifest=manifest,rows=rows)
