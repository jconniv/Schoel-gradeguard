"""Read-only, versioned evidence for point inspection and connected profiles."""
import copy
import json
import math
from pathlib import Path

from gradeguard_pdf import fingerprint

SCHEMA='gradeguard.calculation-review.v1'


def finite(value):
    return value is not None and isinstance(value,(int,float)) and math.isfinite(value)


def profile_evidence(anchors,station,z,controls,closed_length=None):
    """Record operands alongside the calculator; never change its answer."""
    if z is None: return dict(method='Unresolved',formula='No controlling grades bracket this station.',controls=[])
    ordered=sorted(anchors)
    held=next((a for a in ordered if abs(a[0]-station)<=1e-7),None)
    if held:
        used=[held]; expected=held[1]; formula=f'Hold control at station {held[0]:.6f}: Z = {held[1]:.6f} ft.'
        method='Held design control'
    else:
        if closed_length and ordered:
            ordered=[(ordered[-1][0]-closed_length,*ordered[-1][1:])]+ordered+[(ordered[0][0]+closed_length,*ordered[0][1:])]
        pair=next(((a,b) for a,b in zip(ordered,ordered[1:]) if a[0]<station<b[0]),None)
        if not pair: raise ValueError('Calculation evidence could not locate its station bracket.')
        a,b=pair; used=[a,b]; fraction=(station-a[0])/(b[0]-a[0]); expected=a[1]+fraction*(b[1]-a[1])
        method='Linear interpolation along path station'
        formula=(f'Z = Z1 + (S - S1) / (S2 - S1) × (Z2 - Z1)\n'
                 f'Z = {a[1]:.6f} + ({station:.6f} - {a[0]:.6f}) / '
                 f'({b[0]:.6f} - {a[0]:.6f}) × ({b[1]:.6f} - {a[1]:.6f})\n'
                 f'Z = {expected:.6f} ft; bracket length = {b[0]-a[0]:.6f} ft.\n'
                 f'Bracket slope = {100*(b[1]-a[1])/(b[0]-a[0]):.6f}%.')
    if not math.isclose(expected,z,rel_tol=1e-12,abs_tol=1e-8): raise ValueError('Recorded formula does not match the calculated elevation.')
    sources=[]
    for anchor in used:
        c=next((c for c in controls if c['id']+' '+c.get('role','Spot')==anchor[2]),None)
        if c is None: continue
        item=copy.deepcopy(c); item['station']=anchor[0]
        item['top_elevation']=c['z']; item['printed_elevation']=c.get('printed_z',c['z'])
        item['conversion']=(f"{item['printed_elevation']:.6f} + {c['rise']:.6f} = {c['z']:.6f} ft (local rise applied once)"
                            if c['kind']=='CURB_FL' else f"Hold {c['z']:.6f} ft directly; no curb rise added.")
        sources.append(item)
    return dict(method=method,formula=formula,controls=sources)


def surface_evidence(xy,surface,finished_grade,deduction=0):
    import schoel_gradeguard as gg
    if surface is not None and finished_grade is not None:
        for face in surface.triangles:
            if not (min(v[0] for v in face)-1e-8<=xy[0]<=max(v[0] for v in face)+1e-8 and min(v[1] for v in face)-1e-8<=xy[1]<=max(v[1] for v in face)+1e-8): continue
            weights=gg._barycentric_xy(xy,face)
            if weights is None or min(weights)<-1e-10: continue
            value=sum(w*v[2] for w,v in zip(weights,face))
            if not math.isclose(value,finished_grade,rel_tol=1e-12,abs_tol=1e-7): continue
            terms=' + '.join(f'{w:.8f} × {v[2]:.6f}' for w,v in zip(weights,face))
            return dict(method='Surface triangle interpolation',triangle=[list(v) for v in face],weights=list(weights),controls=[],
                        formula=f'Finished grade = {terms} = {value:.6f} ft.\nFinal grade = {value:.6f} - {deduction:.6f} = {value-deduction:.6f} ft.')
    return dict(method='Recorded engine result',formula='Intermediate surface operands were not recorded for this result. See its original source record.',controls=[])


def save_review(folder,model):
    path=Path(folder)/'Calculation-Review.json'
    path.write_text(json.dumps(model,indent=2,allow_nan=False),encoding='utf-8')
    return str(path)


def pdf_review(job,rows,transform_model):
    from gradeguard_auto import transform
    import schoel_gradeguard as gg
    a=complex(*transform_model['a']); b=complex(*transform_model['b'])
    points=[]
    for row in rows:
        p=(complex(row['x'],row['y'])-b)/a
        points.append(dict(id=str(row['point']),number=row['point'],tool=row['tool'],path=row['path'],station=row['station'] if row['station']!='' else None,
                           x=row['x'],y=row['y'],z=row['elevation'],approved=False,status=row['status'],source=row['source'],issue='',
                           pdf=[p.real,-p.imag],detail=row.get('detail',{})))
    paths=[dict(id=p['id'],points=[transform(transform_model,q) for q in p['points']],pdf=p['points'],closed=p['points'][0]==p['points'][-1],
                station_note='Station follows the selected PDF path; curve geometry is flattened from the plan.') for p in job.get('paths',[])]
    return dict(schema=SCHEMA,version=gg.APP_VERSION,workflow='PDF only',units='US survey feet',coordinate_frame=transform_model['mode'],
                sources={job['pdf_path']:job['pdf_sha256']},pdf_path=job['pdf_path'],pdf_sha256=job['pdf_sha256'],pdf_page=job.get('page',1),
                paths=paths,points=points,note='PDF-derived calculated review only. Inspection does not approve or change any elevation.')


def connected_paths(segments):
    """Split at actual endpoints and branches. Never join by point-number order."""
    edges=[]; adjacency={}; seen=set()
    def key(p): return tuple(round(float(x),8) for x in p)
    for segment in segments:
        a,b=key(segment.a),key(segment.b)
        edge=tuple(sorted((a,b)))
        if a==b or edge in seen: continue
        seen.add(edge); i=len(edges); edges.append((a,b))
        adjacency.setdefault(a,[]).append(i); adjacency.setdefault(b,[]).append(i)
    used=set(); paths=[]
    def walk(start,first):
        route=[start]; current=start; i=first
        while i not in used:
            used.add(i); a,b=edges[i]; following=b if a==current else a; route.append(following)
            if len(adjacency[following])!=2: break
            choices=[j for j in adjacency[following] if j not in used]
            if not choices: break
            current=following; i=choices[0]
        return route
    for node,ids in adjacency.items():
        if len(ids)!=2:
            for i in ids:
                if i not in used: paths.append(walk(node,i))
    for i,(a,b) in enumerate(edges):
        if i not in used: paths.append(walk(a,i))
    return [dict(id=f'Run {i+1}',points=p,closed=p[0]==p[-1],station_note='Display station along imported DXF segments; curve lengths are approximated. Disconnected runs remain separate.') for i,p in enumerate(paths)]


def assign_paths(points,paths):
    from scipy.spatial import cKDTree
    segments=[]; centres=[]; half=[]
    for path in paths:
        station=0.
        for a,b in zip(path['points'],path['points'][1:]):
            length=math.dist(a,b)
            segments.append((path['id'],station,a,b,length)); centres.append(((a[0]+b[0])/2,(a[1]+b[1])/2)); half.append(length/2)
            station+=length
    if not segments: return
    tree=cKDTree(centres); radius=max(half)+.35
    for p in points:
        xy=(p['x'],p['y']); matches=[]
        for i in tree.query_ball_point(xy,radius):
            name,station,a,b,length=segments[i]
            vx,vy=b[0]-a[0],b[1]-a[1]
            t=max(0.,min(1.,((xy[0]-a[0])*vx+(xy[1]-a[1])*vy)/(length*length)))
            d=math.hypot(xy[0]-a[0]-t*vx,xy[1]-a[1]-t*vy)
            if d<=.35: matches.append((d,name,station+t*length))
        if not matches: continue
        matches.sort(); best=matches[0]
        if any(abs(m[0]-best[0])<1e-7 and m[1]!=best[1] for m in matches[1:]):
            p['issue']+=' Profile association is ambiguous at a shared junction.'; continue
        p['path']=best[1]; p['station']=best[2]


def write_cad_review(points,folder,tool,segments,cad_path,pdf_path='',surface_path='',surface=None,deduction=0,anchor_path='',settings=None,extra_points=None):
    import schoel_gradeguard as gg
    records=[]; source_files=[p for p in (cad_path,pdf_path,surface_path,anchor_path) if p and Path(p).is_file()]
    sources={p:fingerprint(p) for p in source_files}
    controls=[]
    if anchor_path and Path(anchor_path).is_file():
        data=json.loads(Path(anchor_path).read_text(encoding='utf-8'))
        if data.get('schema')=='gradeguard.pdf-controls.v1': controls=data.get('manual_spots',[])
    extras=extra_points or []; extra_ids={id(p) for p in extras}
    for point in list(points)+list(extras):
        z=point.elevation if point.elevation is not None else point.calculated_elevation
        if not finite(z): z=None
        accepted,_=gg.field_approval(point)
        detail=dict(method='Recorded engine result',formula='See the recorded source and QA notes below. Unrecorded intermediate operands are not inferred.',controls=[])
        if z is not None and 'LandXML/TIN surface' in point.source and point.confidence.split(';')[0]=='SURFACE_HIGH':
            detail=surface_evidence((point.easting,point.northing),surface,z+deduction,deduction)
        if 'MANUAL REVIEWED PDF' in point.source:
            matches=[c for c in controls if math.dist(c['cad'],(point.easting,point.northing))<=.35 and z is not None and abs(c['elev']-z)<1e-7]
            if len(matches)==1:
                c=matches[0]
                detail=dict(method='Reviewed PDF hold',formula=f"Printed grade {c['raw_elevation']:.6f} + local rise {c['curb_rise']:.6f} = {c['elev']:.6f} ft.",
                            controls=[dict(id=c['id'],page=c['page'],pdf_bbox=c['pdf_bbox'],printed=c['raw'],printed_elevation=c['raw_elevation'],top_elevation=c['elev'],conversion=c['meaning'],role=c['role'])])
        if z is None: detail=dict(method='Unresolved',formula='No usable elevation was recorded. A missing grade is not zero.',controls=[])
        records.append(dict(id=str(point.point),number=point.point,tool='CURB OFFSET' if id(point) in extra_ids else tool,path='',station=None,x=point.easting,y=point.northing,z=z,
                            approved=accepted,status='FIELD_ACCEPTED' if accepted else 'WITHHELD',source=point.source,issue=point.issue,detail=detail))
    paths=connected_paths(segments) if tool in ('CURB','SIDEWALK') else []
    assign_paths([p for p in records if p['tool']==tool],paths)
    model=dict(schema=SCHEMA,version=gg.APP_VERSION,workflow='CAD',units='US survey feet',coordinate_frame='survey',sources=sources,
               pdf_path=pdf_path,pdf_sha256=sources.get(pdf_path,''),paths=paths,points=records,settings=settings or {},
               note='Read-only calculation inspection. Acceptance states are copied from the existing export gate, not granted by this viewer. Profile station is for display; original coordinates and grades are unchanged.')
    return save_review(folder,model)


def merge_reviews(files,folder):
    models=[load_review(path) for path in files]
    if not models: return ''
    result=copy.deepcopy(models[0]); result['points']=[]; result['paths']=[]; result['sources']={}
    for i,model in enumerate(models):
        prefix=str(i+1)+': '
        result['sources'].update(model['sources'])
        for path in model['paths']:
            path['id']=prefix+path['id']; result['paths'].append(path)
        for point in model['points']:
            point['id']=prefix+point['id']
            if point['path']: point['path']=prefix+point['path']
            result['points'].append(point)
    result['workflow']='CAD package'
    return save_review(folder,result)


def load_review(path):
    model=json.loads(Path(path).read_text(encoding='utf-8'))
    if model.get('schema')!=SCHEMA: raise ValueError('Choose Calculation-Review.json from a version 9.6 or newer calculation run.')
    if len(model.get('points',[]))>250000: raise ValueError('Review package exceeds 250,000 points.')
    ids=set()
    for point in model['points']:
        if point['id'] in ids: raise ValueError('Duplicate review point identity.')
        ids.add(point['id'])
        if not finite(point['x']) or not finite(point['y']): raise ValueError('Nonfinite review coordinates.')
        if point['z'] is not None and not finite(point['z']): raise ValueError('Nonfinite review grade.')
        if point['station'] is not None and not finite(point['station']): raise ValueError('Nonfinite review station.')
    return model


def profile_edges(points,closed_length=None):
    """No slopes across missing grades, duplicate stations or different paths."""
    ordered=sorted((p for p in points if finite(p.get('station'))),key=lambda p:p['station'])
    pairs=list(zip(ordered,ordered[1:]))
    if closed_length and len(ordered)>1: pairs.append((ordered[-1],ordered[0]))
    edges=[]
    for a,b in pairs:
        span=b['station']-a['station']
        if span<0 and closed_length: span+=closed_length
        if a['path']!=b['path'] or span<=1e-8 or not finite(a['z']) or not finite(b['z']): continue
        slope=100*(b['z']-a['z'])/span
        edges.append(dict(a=a['id'],b=b['id'],length=span,slope=slope,flow='flat' if abs(slope)<1e-10 else (a['id'] if slope>0 else b['id'])))
    return edges
