import os
import sys
import re
import math
import csv
import json
import traceback
import base64
import ctypes
import hashlib
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict, Any

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception:
    raise

try:
    import ezdxf
except Exception:
    ezdxf = None

try:
    import fitz
except Exception:
    fitz = None

Point2 = Tuple[float, float]
APP_VERSION = "9.6.0"

@dataclass
class GenPoint:
    point: int
    northing: float
    easting: float
    elevation: Optional[float]
    desc: str = "BC"
    source: str = ""
    issue: str = ""
    confidence: str = ""
    approved: Optional[bool] = None
    locked: bool = False
    calculated_elevation: Optional[float] = None

@dataclass
class GradeLabel:
    x: float
    y: float
    kind: str
    elev: float
    raw: str
    source: str

@dataclass
class ElevSegment:
    a: Point2
    b: Point2
    z: float
    layer: str
    source: str
    z2: Optional[float] = None

@dataclass
class CurbSegment:
    a: Point2
    b: Point2
    layer: str

@dataclass
class LandXMLSurface:
    triangles: List[Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]]
    # Civil 3D can export LandXML as PlanFeatures/FeatureLines instead of a TIN surface.
    # GradeGuard keeps those 3D breakline segments here so they can be used as the elevation model.
    segments: List[ElevSegment] = field(default_factory=list)
    source: str = "LandXML surface"
    name: str = ""
    linear_unit: str = "USSurveyFoot"

@dataclass
class ContourTinSurface:
    points: Any
    z: Any
    tri: Any
    source: str = "TIN created from 3D DXF contours"


# Global cache used by the Generate engine.  It lets GradeGuard know where the
# contour or surface actually crosses the selected BC layer.  This is important:
# a contour touching the BC line is a true BC elevation; a contour near the curb
# but not touching it is only fallback evidence.
CURB_SEGMENTS: List[CurbSegment] = []
FLOWLINE_SEGMENTS: List[CurbSegment] = []
# Exact contour crossing elevation overrides keyed by generated BC point coordinate.
# This prevents projected FL/gutter contour crossings from being reinterpreted later
# by the temporary TIN or by the opposite curb component.
CONTOUR_POINT_OVERRIDES: Dict[Tuple[int, int], Tuple[float, str, str, str]] = {}
CURRENT_CURB_HEIGHT: float = 0.50
CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF: bool = False
# Only true staking control points: PC, curve midpoint, PT, and straight vertices.
# Do not use endpoints of dense curve tessellation segments, or the CSV gets too many curve points.
CURB_CONTROL_POINTS: List[Point2] = []
# General non-curb linework used to find leader/tick endpoints for HP/LP callouts.
# This helps when the text insertion point is not the actual curb control tick.
DXF_LEADER_SEGMENTS: List[CurbSegment] = []



def reset_generation_state():
    global CURB_SEGMENTS, FLOWLINE_SEGMENTS, CURB_CONTROL_POINTS, CURB_PC_POINTS, CURB_PT_POINTS, DXF_LEADER_SEGMENTS, CONTOUR_POINT_OVERRIDES
    CURB_SEGMENTS, FLOWLINE_SEGMENTS, CURB_CONTROL_POINTS = [], [], []
    CURB_PC_POINTS, CURB_PT_POINTS, DXF_LEADER_SEGMENTS = [], [], []
    CONTOUR_POINT_OVERRIDES = {}


def held_result(conf, source):
    tokens = {t.strip() for t in (conf or '').upper().split(';')}
    return bool(tokens & {'HELD_CONTROL', 'PDF_TICK', 'HP_HARD_HOLD', 'LP_HARD_HOLD', 'MANUAL_FL_OUT'}) or 'MANUAL' in (source or '').upper()


def field_approval(point):
    try:
        if not all(math.isfinite(float(v)) for v in (point.easting, point.northing, point.elevation)):
            return False, 'Nonfinite coordinate or elevation.'
        if abs(float(point.elevation)) <= 1e-9:
            return False, 'Zero elevation requires explicit office review.'
    except (TypeError, ValueError):
        return False, 'Missing coordinate or elevation.'
    if point.approved is False:
        return False, 'Previous export approval was withheld.'
    tokens = {t.strip() for t in (point.confidence or '').upper().split(';')}
    text = ' '.join([point.source or '', point.issue or '']).upper()
    blocking = ('NOT BRACKETED', 'UNSAFE', 'UNKNOWN', 'WITHHELD', 'DO NOT STAKE', 'CONFLICTING', 'NO EXPLICIT OUT', 'FALLBACK', 'CROSS-SLOPE', 'CROSS SLOPE', 'REVIEW REQUIRED')
    if any(word in text for word in blocking) or any('REVIEW' in t or t.startswith('LOW') for t in tokens):
        return False, 'Unresolved calculation/source review.'
    trusted = {'HELD_CONTROL', 'PDF_TICK', 'HP_HARD_HOLD', 'LP_HARD_HOLD', 'MANUAL_FL_OUT', 'STRUCTURE_OUT',
               'BC_CONTOUR_EXACT', 'FL_CONTOUR_PLUS_CURB_EXACT', 'BC_CHAIN_CONTOUR_INTERP', 'BC_TOUCH', 'BC_TOUCH_INTERP',
               'FL_TOUCH_PLUS_CURB', 'FL_TOUCH_INTERP_PLUS_CURB', 'SURFACE_HIGH', 'FEATURELINE_HIGH',
               'PERP_CONTOUR_HIGH', 'PERP_CONTOUR', 'LOCAL_CONTOUR_HOLD', 'LOCAL_CONTOUR_PRORATE'}
    if tokens & trusted:
        return True, ''
    if point.approved is True and tokens == {'OFFSET'}:
        return True, ''
    return False, 'No approved elevation source. Review before staking.'


def component_key_for_point(p):
    local = safe_local_curb_chain(p)
    if not local:
        return None
    return tuple(sorted(tuple(sorted((seg.a, seg.b))) for seg in local))


def same_curb_run(a, b):
    ka = component_key_for_point(a)
    return ka is not None and ka == component_key_for_point(b)


def safe_local_curb_chain(p):
    """Station only an unbranched continuous chain; don't bridge drafted gaps."""
    local = _local_curb_segments_for_point(p, max_endpoint_gap=1e-6)
    if any(dist(a.b, b.a) > 1e-6 for a, b in zip(local, local[1:])):
        return []
    degree = {}
    for seg in local:
        for xy in (seg.a, seg.b):
            key = (round(xy[0], 6), round(xy[1], 6))
            degree[key] = degree.get(key, 0) + 1
    return local if all(count <= 2 for count in degree.values()) else []


def diagnostic_pairs(points):
    """Adjacent stations within continuous unbranched runs, retaining missing-grade gaps."""
    if not CURB_SEGMENTS:
        for a, b in zip(points, points[1:]):
            if a.elevation is not None and b.elevation is not None:
                yield a, b, dist((a.easting, a.northing), (b.easting, b.northing))
        return
    groups = {}
    for point in points:
        xy = (point.easting, point.northing)
        chain = safe_local_curb_chain(xy)
        if not chain:
            continue
        key = tuple(sorted(tuple(sorted((seg.a, seg.b))) for seg in chain))
        if key not in groups:
            groups[key] = (chain, [])
        chain, stations = groups[key]
        station, _, distance, _ = _chain_station_for_point_on_segments(xy, chain)
        if station is not None and distance <= 0.35:
            stations.append((station, point))
    for chain, stations in groups.values():
        stations.sort(key=lambda item: item[0])
        for (sa, a), (sb, b) in zip(stations, stations[1:]):
            if a.elevation is not None and b.elevation is not None:
                yield a, b, abs(sb - sa)


def validate_generation_inputs(cad_path, spacing=None, units_confirmed=False):
    if spacing is not None and (not math.isfinite(float(spacing)) or float(spacing) <= 0):
        raise ValueError('Spacing must be a finite positive distance.')
    if not os.path.isfile(cad_path) or os.path.splitext(cad_path)[1].lower() != '.dxf':
        raise ValueError('Select an existing local DXF file.')
    units = int(ezdxf.readfile(cad_path).units)
    if units == 21 or (units == 0 and units_confirmed):
        return
    if units == 0:
        raise ValueError('DXF units are unspecified. Confirm US survey feet in Project files after checking the source drawing; no conversion will be made.')
    raise ValueError(f'DXF unit code {units} does not match the US-survey-foot workflow (21). Re-export in matching units; coordinates have not been converted.')


def dist(a: Point2, b: Point2) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def lerp(a: Point2, b: Point2, t: float) -> Point2:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def point_segment_distance(p: Point2, a: Point2, b: Point2) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0:
        return dist(p, a)
    c2 = vx * vx + vy * vy
    if c2 <= 0:
        return dist(p, a)
    if c2 <= c1:
        return dist(p, b)
    t = c1 / c2
    proj = (ax + t * vx, ay + t * vy)
    return dist(p, proj)


def parse_grade_text(raw: str) -> List[Tuple[str, float]]:
    s = raw.replace("\\P", " ").replace("\n", " ").replace(":", " ").upper()
    out = []
    patterns = [
        # HP/LP callouts must be recognized before generic TC/BC/FL patterns.
        # Civil sheets often say HP BOC, LP BOC, HIGH POINT BOC, or LOW POINT BOC
        # instead of HP/TC or LP/TC.  These are top/back-of-curb controls and
        # must create BC HP / BC LP points at the leader arrow endpoint.
        # BOC is firm-specific: it can mean BACK OF CURB or BOTTOM OF CURB.
        # Never silently treat the abbreviation as a top/back elevation.
        (r"\b(?:H\.?\s*P\.?|HIGH\s*POINT|HIGHPOINT)\s*/?\s*B\.?\s*O\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "HP_BOC_AMBIG"),
        (r"\b(?:L\.?\s*P\.?|LOW\s*POINT|LOWPOINT)\s*/?\s*B\.?\s*O\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "LP_BOC_AMBIG"),
        (r"\b(?:H\.?\s*P\.?|HIGH\s*POINT|HIGHPOINT)\s*/?\s*BOTTOM\s*OF\s*CURB\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "HP_BOTTOM_CURB"),
        (r"\b(?:L\.?\s*P\.?|LOW\s*POINT|LOWPOINT)\s*/?\s*BOTTOM\s*OF\s*CURB\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "LP_BOTTOM_CURB"),
        (r"\b(?:H\.?\s*P\.?|HIGH\s*POINT|HIGHPOINT)\s*/?\s*(?:T\.?\s*C\.?|B\.?\s*C\.?|TOP\s*OF\s*CURB|BACK\s*OF\s*CURB)\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "HP_TC"),
        (r"\b(?:L\.?\s*P\.?|LOW\s*POINT|LOWPOINT)\s*/?\s*(?:T\.?\s*C\.?|B\.?\s*C\.?|TOP\s*OF\s*CURB|BACK\s*OF\s*CURB)\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "LP_TC"),
        (r"\bH\.?\s*P\.?\s*/?\s*T\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "HP_TC"),
        (r"\bL\.?\s*P\.?\s*/?\s*T\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "LP_TC"),
        (r"\b(?:H\.?\s*P\.?|HIGH\s*POINT|HIGHPOINT)\s*(?:EL(?:EV(?:ATION)?)?\.?\s*)?=?\s*([0-9]{2,4}\.[0-9]{1,3})", "HP"),
        (r"\b(?:L\.?\s*P\.?|LOW\s*POINT|LOWPOINT)\s*(?:EL(?:EV(?:ATION)?)?\.?\s*)?=?\s*([0-9]{2,4}\.[0-9]{1,3})", "LP"),
        (r"\bT\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "TC"),
        (r"\bB\.?\s*O\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "BOC_AMBIG"),
        (r"\bB\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "BC"),
        (r"\bF\.?\s*L\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "FL"),
        (r"\bFG\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "FG"),
        (r"\bFFE\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "FFE"),
        (r"\bELEV\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "ELEV"),
    ]
    occupied_spans = []
    for pat, kind in patterns:
        for m in re.finditer(pat, s):
            # Patterns are ordered strongest-first. Do not reinterpret HP/TC as
            # another HP/TC and a generic TC, or HIGH POINT BOC as generic BC.
            if any(m.start() < end and m.end() > start for start, end in occupied_spans):
                continue
            try:
                out.append((kind, float(m.group(1))))
                occupied_spans.append(m.span())
            except Exception:
                pass
    return out


def text_value(entity) -> str:
    if entity.dxftype() == "TEXT":
        return entity.dxf.text
    if entity.dxftype() == "MTEXT":
        return entity.text
    return ""


def entity_layer(entity) -> str:
    try:
        return entity.dxf.layer or ""
    except Exception:
        return ""


def arc_points_from_entity(ent, spacing: float = 1.0) -> List[Point2]:
    """Return WCS points for an ARC using max chord length style spacing.

    Important: ezdxf ARC.flattening() takes a SAGITTA value, not a station
    spacing. Earlier GradeGuard builds passed values such as 1, 2, or 8 into
    flattening(), which sometimes reduced real curb arcs to a single chord.
    That made picker curves look bad and made midpoint stake points plot beside
    the curb.

    This routine uses ezdxf's ConstructionArc to honor OCS/extrusion and then
    samples the arc by arc length so the display segments and snap geometry
    follow the true curve.
    """
    try:
        tool = ent.construction_tool()
        r = abs(float(tool.radius))
        span = abs(math.radians(float(tool.angle_span)))
        arc_len = max(r * span, 0.0)
        max_chord = max(float(spacing or 1.0), 0.10)
        n = max(2, int(math.ceil(arc_len / max_chord)) + 1)
        pts = [(float(v.x), float(v.y)) for v in ent.vertices(list(tool.angles(n)))]
        if len(pts) >= 2:
            return pts
    except Exception:
        pass
    try:
        # Fallback. Use a very small sagitta so flattening does not collapse
        # the arc into a long chord.
        sagitta = max(min(float(spacing or 1.0) / 20.0, 0.05), 0.005)
        pts = [(float(v.x), float(v.y)) for v in ent.flattening(sagitta)]
        if len(pts) >= 2:
            return pts
    except Exception:
        pass
    center = (float(ent.dxf.center.x), float(ent.dxf.center.y))
    r = float(ent.dxf.radius)
    start = math.radians(float(ent.dxf.start_angle))
    end = math.radians(float(ent.dxf.end_angle))
    if end < start:
        end += math.tau
    length = abs(end - start) * abs(r)
    n = max(2, int(math.ceil(length / max(float(spacing or 1.0), 0.10))) + 1)
    pts = []
    for i in range(n):
        t = i / (n - 1)
        a = start + (end - start) * t
        pts.append((center[0] + r * math.cos(a), center[1] + r * math.sin(a)))
    return pts


def bulge_segment_points(a: Point2, b: Point2, bulge: float, spacing: float = 5.0) -> List[Point2]:
    """Tessellate an AutoCAD bulge segment.

    LWPOLYLINE curb returns often store curves as bulges rather than ARC
    entities. Earlier GradeGuard builds connected the chord only, so the pick
    viewer and generated BC points ignored the true curved curb.
    """
    if abs(bulge) < 1e-12 or dist(a, b) <= 1e-12:
        return [a, b]
    x1, y1 = a
    x2, y2 = b
    dx, dy = x2 - x1, y2 - y1
    chord = math.hypot(dx, dy)
    theta = 4.0 * math.atan(float(bulge))
    # Radius is always positive; sign of theta controls direction.
    r = abs(chord / (2.0 * math.sin(theta / 2.0))) if abs(math.sin(theta / 2.0)) > 1e-12 else chord / 2.0
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    ux, uy = dx / chord, dy / chord
    # left normal to chord
    nx, ny = -uy, ux
    h = chord * (1.0 - float(bulge)**2) / (4.0 * float(bulge))
    # Signed offset also handles arcs exceeding 180 degrees.
    cx, cy = mx + nx * h, my + ny * h
    a0 = math.atan2(y1 - cy, x1 - cx)
    sweep = theta
    arc_len = abs(sweep) * r
    n = max(2, int(math.ceil(arc_len / max(spacing, 0.25))) + 1)
    pts: List[Point2] = []
    for i in range(n):
        t = i / (n - 1)
        ang = a0 + sweep * t
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    # Force exact endpoints so snapping/PC/PT stay clean.
    pts[0] = a
    pts[-1] = b
    return pts


def bulge_midpoint(a, b, bulge):
    """Exact half-angle point, independent of display tessellation spacing."""
    if abs(bulge) < 1e-12:
        return lerp(a, b, 0.5)
    # The arc midpoint lies on the perpendicular bisector, at half-chord * bulge.
    return ((a[0] + b[0]) / 2 + (b[1] - a[1]) * bulge / 2,
            (a[1] + b[1]) / 2 - (b[0] - a[0]) * bulge / 2)


def polyline_xy_to_wcs(ent, points):
    """Convert 2D polyline OCS coordinates; 3D polyline vertices already use WCS."""
    typ = ent.dxftype()
    if typ != 'LWPOLYLINE' and not (typ == 'POLYLINE' and ent.is_2d_polyline):
        return points
    elevation = ent.dxf.elevation
    z = float(elevation if typ == 'LWPOLYLINE' else elevation.z)
    ocs = ent.ocs()
    return [(v.x, v.y) for v in (ocs.to_wcs((x, y, z)) for x, y in points)]


def validate_saved_drawing(data, cad_path):
    if not cad_path:
        return
    saved = data.get('cad_path') or data.get('dxf_path') or ''
    if not saved or os.path.normcase(os.path.abspath(saved)) != os.path.normcase(os.path.abspath(cad_path)):
        raise ValueError('Saved controls belong to another or unspecified drawing. Re-pick/re-save them for the current DXF.')
    if data.get('dxf_sha256') != drawing_fingerprint(cad_path):
        raise ValueError('Saved controls predate drawing validation or the DXF has changed. Review and re-save these controls.')


def prepare_output_directory(out_dir):
    """Keep existing GradeGuard deliverables intact when a calculation is rerun."""
    if not str(out_dir).strip():
        raise ValueError('Select an output folder.')
    os.makedirs(out_dir, exist_ok=True)
    if any(name.startswith(('Generated_', 'GradeGuard_', 'FL_OUT_DO_NOT', 'Sidewalk_DO_NOT', 'Grade_Grid_DO_NOT'))
           and os.path.isfile(os.path.join(out_dir, name)) for name in os.listdir(out_dir)):
        from datetime import datetime
        import tempfile
        return tempfile.mkdtemp(prefix='Run-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-', dir=out_dir)
    return out_dir


def validate_numeric_settings(**settings):
    for name, value in settings.items():
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(name.replace('_', ' ') + ' must be finite and nonnegative.')


def polyline_points(ent, arc_spacing: float = 5.0) -> List[Point2]:
    """Return XY points for LINE/POLYLINE geometry, honoring LWPOLYLINE bulges.

    AutoCAD frequently stores curb returns inside a single LWPOLYLINE as bulge
    arcs. Treating those bulges as straight chords makes GradeGuard miss curves
    when the user manually picks true back of curb.
    """
    pts: List[Point2] = []
    if ent.dxftype() == "LWPOLYLINE":
        raw = [(float(p[0]), float(p[1]), float(p[2] or 0.0)) for p in ent.get_points("xyb")]
        if not raw:
            return []
        pairs = list(zip(raw[:-1], raw[1:]))
        if getattr(ent, "closed", False) and len(raw) > 1:
            pairs.append((raw[-1], raw[0]))
        for idx, (p0, p1) in enumerate(pairs):
            a = (p0[0], p0[1])
            b = (p1[0], p1[1])
            seg = bulge_segment_points(a, b, p0[2], spacing=arc_spacing)
            if pts:
                seg = seg[1:]
            pts.extend(seg)
        if not pts:
            pts = [(raw[0][0], raw[0][1])]
    elif ent.dxftype() == "POLYLINE":
        raw = []
        try:
            verts = list(ent.vertices)
        except Exception:
            verts = []
        for v in verts:
            loc = v.dxf.location
            bulge = float(getattr(v.dxf, "bulge", 0.0) or 0.0)
            raw.append((float(loc.x), float(loc.y), bulge))
        if not raw:
            return []
        pairs = list(zip(raw[:-1], raw[1:]))
        try:
            is_closed = bool(ent.is_closed)
        except Exception:
            is_closed = False
        if is_closed and len(raw) > 1:
            pairs.append((raw[-1], raw[0]))
        for p0, p1 in pairs:
            a = (p0[0], p0[1])
            b = (p1[0], p1[1])
            seg = bulge_segment_points(a, b, p0[2], spacing=arc_spacing)
            if pts:
                seg = seg[1:]
            pts.extend(seg)
    return polyline_xy_to_wcs(ent, pts)


def line_points(ent) -> List[Point2]:
    return [(float(ent.dxf.start.x), float(ent.dxf.start.y)), (float(ent.dxf.end.x), float(ent.dxf.end.y))]


def picker_entity_points(ent, spacing: float = 0.75, depth: int = 0) -> List[Point2]:
    """Return display/select points for picker windows.

    v7.4: do not hide CAD linework just because it is a SPLINE, ELLIPSE,
    CIRCLE, or inside a block INSERT. Some engineer exports place curb,
    sidewalk, and storm graphics in those entity types, so the picker must
    show them when the user turns the layer on.
    """
    if ent is None or depth > 3:
        return []
    typ = ent.dxftype()
    try:
        if typ == "LINE":
            return line_points(ent)
        if typ in ("LWPOLYLINE", "POLYLINE"):
            return polyline_points(ent, arc_spacing=spacing)
        if typ == "ARC":
            return arc_points_from_entity(ent, spacing=spacing)
        if typ == "CIRCLE":
            cx, cy = float(ent.dxf.center.x), float(ent.dxf.center.y)
            r = float(ent.dxf.radius)
            n = max(24, int(math.ceil((math.tau * r) / max(spacing, 0.25))))
            return [(cx + r * math.cos(math.tau*i/n), cy + r * math.sin(math.tau*i/n)) for i in range(n+1)]
        if typ == "ELLIPSE":
            pts = []
            try:
                for v in ent.vertices(params=ent.params(73)):
                    pts.append((float(v.x), float(v.y)))
            except Exception:
                try:
                    pts = [(float(v.x), float(v.y)) for v in ent.flattening(spacing)]
                except Exception:
                    pts = []
            return pts
        if typ == "SPLINE":
            try:
                return [(float(v.x), float(v.y)) for v in ent.flattening(spacing)]
            except Exception:
                pts = []
                try:
                    for p in ent.control_points:
                        pts.append((float(p[0]), float(p[1])))
                except Exception:
                    pass
                return pts
        if typ == "HATCH":
            # Use hatch boundary paths when available so exported curb/sidewalk hatch outlines can be seen.
            out = []
            try:
                for path in ent.paths:
                    if hasattr(path, 'vertices'):
                        pts = [(float(v[0]), float(v[1])) for v in path.vertices]
                        if len(pts) > 1:
                            if out and dist(out[-1], pts[0]) < 1e-6:
                                pts = pts[1:]
                            out.extend(pts)
            except Exception:
                pass
            return out
        if typ == "INSERT":
            out = []
            try:
                for ve in ent.virtual_entities():
                    pts = picker_entity_points(ve, spacing=spacing, depth=depth+1)
                    if len(pts) >= 2:
                        if out and dist(out[-1], pts[0]) < 1e-6:
                            pts = pts[1:]
                        out.extend(pts)
            except Exception:
                pass
            return out
    except Exception:
        return []
    return []


def sample_segment(a: Point2, b: Point2, spacing: float) -> List[Point2]:
    L = dist(a, b)
    if not math.isfinite(float(spacing)) or spacing <= 0:
        raise ValueError("Point spacing must be a finite positive distance.")
    if L == 0:
        return [a]
    n = int(math.floor(L / spacing))
    pts = [a]
    for i in range(1, n + 1):
        d = i * spacing
        if d < L - 0.01:
            pts.append(lerp(a, b, d / L))
    pts.append(b)
    return pts


def sample_polyline(pts: List[Point2], spacing: float) -> List[Point2]:
    out = []
    for i in range(len(pts) - 1):
        seg = sample_segment(pts[i], pts[i+1], spacing)
        if out:
            seg = seg[1:]
        out.extend(seg)
    return out



def _point_at_half_station(dense: List[Point2]) -> Optional[Point2]:
    """Return a point near half station along an already tessellated curve."""
    if len(dense) < 2:
        return dense[0] if dense else None
    lengths = [0.0]
    total = 0.0
    for a, b in zip(dense[:-1], dense[1:]):
        total += dist(a, b)
        lengths.append(total)
    if total <= 1e-9:
        return dense[len(dense)//2]
    half = total / 2.0
    for i in range(1, len(lengths)):
        if lengths[i] >= half:
            seg_len = lengths[i] - lengths[i-1]
            if seg_len <= 1e-9:
                return dense[i]
            t = (half - lengths[i-1]) / seg_len
            return lerp(dense[i-1], dense[i], t)
    return dense[-1]


def arc_key_points_from_entity(ent) -> List[Point2]:
    """Exact angular PC/MID/PT in WCS, including negative extrusion."""
    start, end = float(ent.dxf.start_angle), float(ent.dxf.end_angle)
    if end < start:
        end += 360.0
    return [(float(v.x), float(v.y)) for v in ent.vertices([start, (start + end) / 2, end])]


def generic_curve_key_points_from_entity(ent, spacing: float = 0.5) -> List[Point2]:
    """Return PC/MID/PT style key points for non-ARC curve graphics.

    Some civil exports do not store curved curb/sidewalk pieces as ARC or
    bulged LWPOLYLINE. They may come through as SPLINE, ELLIPSE, CIRCLE, or
    block INSERT geometry. The picker can show those objects, but earlier
    generation code ignored them, which made calculated points miss curves.
    This routine uses the same flattened WCS display geometry as the picker,
    then returns only the start, station-midpoint, and end point for staking.
    """
    try:
        dense = picker_entity_points(ent, spacing=spacing)
    except Exception:
        dense = []
    dense = [(float(x), float(y)) for x, y in dense if math.isfinite(float(x)) and math.isfinite(float(y))]
    dense = dedupe_points(dense, tol=0.01)
    if len(dense) < 2:
        return []
    mid = _point_at_half_station(dense) or dense[len(dense)//2]
    return [dense[0], mid, dense[-1]]


def staking_points_from_entity(ent, spacing: float) -> Tuple[List[Point2], List[Point2]]:
    """Generate staking points and true controls for a curb entity.

    Straight curb runs get points every spacing feet plus vertices.
    Curved curb returns get ONLY PC, midpoint, and PT.  Dense tessellation is
    still used elsewhere for intersections, but it should not become stakeout
    points.
    """
    typ = ent.dxftype()
    stake: List[Point2] = []
    controls: List[Point2] = []

    def add_seq(seq: List[Point2]):
        nonlocal stake
        if not seq:
            return
        if stake and dist(stake[-1], seq[0]) <= 0.05:
            seq = seq[1:]
        stake.extend(seq)

    if typ == 'LINE':
        pts = line_points(ent)
        seq = sample_segment(pts[0], pts[1], spacing)
        add_seq(seq)
        controls.extend([pts[0], pts[1]])

    elif typ == 'ARC':
        keys = arc_key_points_from_entity(ent)
        add_seq(keys)
        controls.extend(keys)

    elif typ == 'LWPOLYLINE':
        raw = [(float(p[0]), float(p[1]), float(p[2] or 0.0)) for p in ent.get_points('xyb')]
        if not raw:
            return [], []
        pairs = list(zip(raw[:-1], raw[1:]))
        if getattr(ent, 'closed', False) and len(raw) > 1:
            pairs.append((raw[-1], raw[0]))
        for p0, p1 in pairs:
            a = (p0[0], p0[1]); b = (p1[0], p1[1]); bulge = p0[2]
            if abs(bulge) > 1e-12:
                mid = bulge_midpoint(a, b, bulge)
                keys = [a, mid, b]
                add_seq(keys)
                controls.extend(keys)
            else:
                seq = sample_segment(a, b, spacing)
                add_seq(seq)
                controls.extend([a, b])

    elif typ == 'POLYLINE':
        raw = []
        try:
            verts = list(ent.vertices)
        except Exception:
            verts = []
        for v in verts:
            loc = v.dxf.location
            bulge = float(getattr(v.dxf, 'bulge', 0.0) or 0.0)
            raw.append((float(loc.x), float(loc.y), bulge))
        if not raw:
            return [], []
        pairs = list(zip(raw[:-1], raw[1:]))
        try:
            is_closed = bool(ent.is_closed)
        except Exception:
            is_closed = False
        if is_closed and len(raw) > 1:
            pairs.append((raw[-1], raw[0]))
        for p0, p1 in pairs:
            a = (p0[0], p0[1]); b = (p1[0], p1[1]); bulge = p0[2]
            if abs(bulge) > 1e-12:
                mid = bulge_midpoint(a, b, bulge)
                keys = [a, mid, b]
                add_seq(keys)
                controls.extend(keys)
            else:
                seq = sample_segment(a, b, spacing)
                add_seq(seq)
                controls.extend([a, b])

    elif typ in ('SPLINE', 'ELLIPSE', 'CIRCLE'):
        # v7.4: generated points must match what the picker can see.
        # Treat these nonstandard curve entities as a curve and create only PC/MID/PT.
        keys = generic_curve_key_points_from_entity(ent, spacing=max(0.25, min(1.0, spacing/10.0)))
        if keys:
            add_seq(keys)
            controls.extend(keys)

    elif typ == 'INSERT':
        # v7.4: some exports place curb/sidewalk linework inside blocks.
        # Generate from the virtual entities instead of ignoring the block.
        try:
            for child in ent.virtual_entities():
                child_stake, child_controls = staking_points_from_entity(child, spacing)
                add_seq(child_stake)
                controls.extend(child_controls)
        except Exception:
            keys = generic_curve_key_points_from_entity(ent, spacing=max(0.25, min(1.0, spacing/10.0)))
            if keys:
                add_seq(keys)
                controls.extend(keys)

    return dedupe_points(polyline_xy_to_wcs(ent, stake)), dedupe_points(polyline_xy_to_wcs(ent, controls))

def dedupe_points(points: List[Point2], tol: float = 0.05) -> List[Point2]:
    """Preserve first-seen points while avoiding an O(n^2) full-list scan."""
    if tol <= 0:
        return list(dict.fromkeys(points))
    out = []
    buckets: Dict[Tuple[int, int], List[Point2]] = {}
    for p in points:
        cell = (math.floor(p[0] / tol), math.floor(p[1] / tol))
        nearby = (
            q
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for q in buckets.get((cell[0] + dx, cell[1] + dy), ())
        )
        if any(dist(p, q) <= tol for q in nearby):
            continue
        out.append(p)
        buckets.setdefault(cell, []).append(p)
    return out




def pc_pt_points_from_entity(ent) -> Tuple[List[Point2], List[Point2]]:
    """Return true PC/PT points for curved DXF entities.

    GradeGuard stakes curves with PC/MID/PT only.  These separate lists let the
    field CSV use descriptions like BC PC, BC PT, SIDEWALK PC, and SIDEWALK PT
    without changing normal straight-run descriptions.
    """
    typ = ent.dxftype()
    pcs: List[Point2] = []
    pts: List[Point2] = []
    try:
        if typ == 'ARC':
            keys = arc_key_points_from_entity(ent)
            if len(keys) >= 3:
                pcs.append(keys[0]); pts.append(keys[-1])
        elif typ == 'LWPOLYLINE':
            raw = [(float(p[0]), float(p[1]), float(p[2] or 0.0)) for p in ent.get_points('xyb')]
            pairs = list(zip(raw[:-1], raw[1:]))
            if getattr(ent, 'closed', False) and len(raw) > 1:
                pairs.append((raw[-1], raw[0]))
            for p0, p1 in pairs:
                if abs(float(p0[2] or 0.0)) > 1e-12:
                    pcs.append((p0[0], p0[1])); pts.append((p1[0], p1[1]))
        elif typ == 'POLYLINE':
            raw = []
            try:
                verts = list(ent.vertices)
            except Exception:
                verts = []
            for v in verts:
                loc = v.dxf.location
                raw.append((float(loc.x), float(loc.y), float(getattr(v.dxf, 'bulge', 0.0) or 0.0)))
            pairs = list(zip(raw[:-1], raw[1:]))
            try:
                is_closed = bool(ent.is_closed)
            except Exception:
                is_closed = False
            if is_closed and len(raw) > 1:
                pairs.append((raw[-1], raw[0]))
            for p0, p1 in pairs:
                if abs(float(p0[2] or 0.0)) > 1e-12:
                    pcs.append((p0[0], p0[1])); pts.append((p1[0], p1[1]))
        elif typ in ('SPLINE', 'ELLIPSE', 'CIRCLE'):
            keys = generic_curve_key_points_from_entity(ent, spacing=0.5)
            if len(keys) >= 3:
                pcs.append(keys[0]); pts.append(keys[-1])
        elif typ == 'INSERT':
            try:
                for child in ent.virtual_entities():
                    cpcs, cpts = pc_pt_points_from_entity(child)
                    pcs.extend(cpcs); pts.extend(cpts)
            except Exception:
                keys = generic_curve_key_points_from_entity(ent, spacing=0.5)
                if len(keys) >= 3:
                    pcs.append(keys[0]); pts.append(keys[-1])
    except Exception:
        pass
    return polyline_xy_to_wcs(ent, pcs), polyline_xy_to_wcs(ent, pts)


def _near_any_control(point: GenPoint, controls: List[Point2], tol: float = 0.08) -> bool:
    p = (float(point.easting), float(point.northing))
    return any(dist(p, q) <= tol for q in controls)


def apply_pc_pt_descriptions(points: List[GenPoint], base_desc: str = 'BC'):
    """Tag curve PC/PT descriptions. HP/LP rules run after this and override if needed."""
    pc_desc = f"{base_desc} PC"
    pt_desc = f"{base_desc} PT"
    for p in points:
        if _near_any_control(p, CURB_PC_POINTS):
            p.desc = pc_desc
        elif _near_any_control(p, CURB_PT_POINTS):
            p.desc = pt_desc
        else:
            p.desc = base_desc

def entity_z(ent) -> Optional[float]:
    candidates = []
    for attr in ("elevation",):
        try:
            candidates.append(float(getattr(ent.dxf, attr)))
        except Exception:
            pass
    try:
        candidates.append(float(ent.dxf.start.z))
    except Exception:
        pass
    try:
        candidates.append(float(ent.dxf.center.z))
    except Exception:
        pass
    for z in candidates:
        # v8.3 safety: a zero Z on a contour/hardscape entity is almost always a
        # CAD placeholder, not a staking grade.  Do not let it become an exact
        # contour crossing.  If the project truly uses zero elevation, it should
        # come from a verified surface/PDF/manual hold, not an untrusted entity Z.
        if valid_grade_z(z):
            return z
    return None


def _z_values_for_segments(segments: List[ElevSegment]) -> List[float]:
    vals = []
    for es in segments or []:
        try:
            vals.append(float(es.z))
            if es.z2 is not None:
                vals.append(float(es.z2))
        except Exception:
            pass
    return vals

def _median(vals: List[float]) -> Optional[float]:
    vals = sorted(float(v) for v in vals if valid_grade_z(v))
    if not vals:
        return None
    n = len(vals)
    return vals[n//2] if n % 2 else (vals[n//2-1] + vals[n//2]) / 2.0

def _segment_near_selected_curb(es: ElevSegment, max_dist: float = 150.0) -> bool:
    """Keep contour/surface segments that are near the selected curb/flowline geometry.

    This prevents far-away contours or bad same-layer construction lines from
    controlling a curb point.  Exact BC/FL contour math should only use contours
    in the curb neighborhood.
    """
    refs = list(CURB_SEGMENTS or []) + list(FLOWLINE_SEGMENTS or [])
    if not refs:
        return True
    for cs in refs:
        if (point_segment_distance(es.a, cs.a, cs.b) <= max_dist or
            point_segment_distance(es.b, cs.a, cs.b) <= max_dist or
            point_segment_distance(cs.a, es.a, es.b) <= max_dist or
            point_segment_distance(cs.b, es.a, es.b) <= max_dist):
            return True
    return False

def sanitize_dxf_elevation_segments_for_bc(segments: List[ElevSegment], max_dist_to_curb: float = 175.0) -> Tuple[List[ElevSegment], Dict[str, Any]]:
    """Filter unsafe DXF contour/surface segments before BC elevation solving.

    v8.3 rule: do not allow zero-Z, wildly out-of-range, or far-away contour
    fragments to produce believable BC elevations.  If a point cannot be solved
    after this filter, it will stay 0.000 for review.
    """
    stats = {"input_segments": len(segments or []), "removed_zero_or_invalid_z": 0, "removed_far_from_selected_curb": 0, "removed_project_outlier_z": 0, "kept_segments": 0, "median_z": None, "accepted_z_window": ""}
    prelim = []
    for es in segments or []:
        z1 = float(es.z) if valid_grade_z(es.z) else None
        z2 = float(es.z2) if es.z2 is not None and valid_grade_z(es.z2) else z1
        if z1 is None or z2 is None:
            stats["removed_zero_or_invalid_z"] += 1
            continue
        if not _segment_near_selected_curb(es, max_dist=max_dist_to_curb):
            stats["removed_far_from_selected_curb"] += 1
            continue
        prelim.append(ElevSegment(es.a, es.b, z1, es.layer, es.source, z2))
    vals = _z_values_for_segments(prelim)
    med = _median(vals)
    stats["median_z"] = med
    if med is None:
        stats["kept_segments"] = 0
        return [], stats
    # Wide enough for real projects, tight enough to reject pipe/text/outlier geometry
    # accidentally put on contour layers.  Outliers like 0, 560, 5053, 17872 are removed.
    lo = med - 75.0
    hi = med + 75.0
    stats["accepted_z_window"] = f"{lo:.3f} to {hi:.3f}"
    out = []
    for es in prelim:
        zvals = [float(es.z)] + ([float(es.z2)] if es.z2 is not None else [])
        if all(lo <= z <= hi for z in zvals):
            out.append(es)
        else:
            stats["removed_project_outlier_z"] += 1
    stats["kept_segments"] = len(out)
    return out, stats




def _xml_local(tag: str) -> str:
    return tag.split('}', 1)[-1] if '}' in tag else tag

def _parse_landxml_xyz(text: str) -> Optional[Tuple[float, float, float]]:
    """LandXML PointType is Northing Easting Elevation, regardless of magnitude."""
    try:
        values = [float(v) for v in re.split(r'[,\s]+', (text or '').strip()) if v]
    except ValueError:
        return None
    if len(values) != 3 or not all(math.isfinite(v) for v in values):
        return None
    n, e, z = values
    return e, n, z


def load_landxml_surface(path: str, surface_name: str = "") -> Optional[LandXMLSurface]:
    """Read one explicitly identified model, keeping point IDs local to each surface.

    This application currently calculates in US survey feet. No implicit unit,
    datum, axis, or coordinate transformation is performed.
    """
    if not path:
        return None
    if not os.path.isfile(path):
        raise ValueError("Selected LandXML file does not exist: " + path)
    try:
        root = ET.parse(path).getroot()
    except Exception as ex:
        raise ValueError(f"Could not read selected LandXML: {ex}") from ex
    units = [el for el in root.iter() if _xml_local(el.tag) in ('Imperial', 'Metric')]
    if len(units) != 1 or units[0].attrib.get('linearUnit', '').lower() != 'ussurveyfoot':
        raise ValueError("LandXML must explicitly declare USSurveyFoot for this US-survey-foot workflow. Re-export in the project units; no conversion was applied.")
    if units[0].attrib.get('elevationUnit', '').lower() not in ('feet', 'foot', 'ussurveyfoot'):
        raise ValueError("LandXML elevation units must explicitly be feet. Confirm the vertical reference before export.")
    surfaces = [el for el in root.iter() if _xml_local(el.tag) == 'Surface']
    if surface_name:
        surfaces = [el for el in surfaces if el.attrib.get('name', '') == surface_name]
        if len(surfaces) != 1:
            raise ValueError(f"Select one uniquely named LandXML surface: {surface_name!r} was not unique or was absent.")
    elif len(surfaces) > 1:
        names = ', '.join(el.attrib.get('name', '<unnamed>') for el in surfaces)
        raise ValueError("LandXML contains multiple surfaces. Enter the proposed surface name in Project files: " + names)
    if not surfaces:
        raise ValueError("No TIN surface found. Export the intended Civil 3D surface as points AND faces; feature-line-only XML is not a surface model.")
    selected = surfaces[0]
    pnts = {}
    for el in selected.iter():
        if _xml_local(el.tag) != 'P':
            continue
        pid = el.attrib.get('id')
        xyz = _parse_landxml_xyz(el.text or '')
        if not pid or xyz is None or pid in pnts:
            raise ValueError("Invalid or duplicate point ID within selected LandXML surface.")
        pnts[pid] = xyz
    faces = []
    for el in selected.iter():
        if _xml_local(el.tag) != 'F':
            continue
        if el.attrib.get('i', '0').lower() in ('1', 'true'):
            continue
        ids = (el.text or '').split()
        if len(ids) != 3 or not all(i in pnts for i in ids):
            raise ValueError("LandXML face references missing/invalid vertices.")
        tri = tuple(pnts[i] for i in ids)
        if _barycentric_xy((tri[0][0], tri[0][1]), tri) is None:
            raise ValueError("Selected LandXML contains a degenerate triangle.")
        faces.append(tri)
    if not faces:
        raise ValueError("Selected LandXML surface has no usable faces.")
    name = selected.attrib.get('name', '')
    return LandXMLSurface(faces, source=os.path.basename(path) + ' / ' + name, name=name)

def _barycentric_xy(p: Point2, tri):
    x,y = p; (x1,y1,z1),(x2,y2,z2),(x3,y3,z3) = tri
    den = (y2-y3)*(x1-x3)+(x3-x2)*(y1-y3)
    if abs(den) < 1e-12:
        return None
    l1 = ((y2-y3)*(x-x3)+(x3-x2)*(y-y3))/den
    l2 = ((y3-y1)*(x-x3)+(x1-x3)*(y-y3))/den
    l3 = 1-l1-l2
    return l1,l2,l3

def landxml_elevation_at(p: Point2, surface: Optional[LandXMLSurface], search_pad: float = 10.0) -> Tuple[Optional[float], str, str]:
    if surface is None:
        return None, '', ''
    # 1) True TIN faces when present.
    best = None
    best_edge = 1e99
    for tri in surface.triangles:
        xs=[tri[0][0],tri[1][0],tri[2][0]]; ys=[tri[0][1],tri[1][1],tri[2][1]]
        if p[0] < min(xs)-search_pad or p[0] > max(xs)+search_pad or p[1] < min(ys)-search_pad or p[1] > max(ys)+search_pad:
            continue
        bc=_barycentric_xy(p, tri)
        if bc is None:
            continue
        l1,l2,l3=bc
        if l1 >= -1e-10 and l2 >= -1e-10 and l3 >= -1e-10:
            z = l1*tri[0][2] + l2*tri[1][2] + l3*tri[2][2]
            return z, f"LandXML/TIN surface {surface.source}", "SURFACE_HIGH"
        d = min(point_segment_distance(p, (tri[0][0],tri[0][1]), (tri[1][0],tri[1][1])),
                point_segment_distance(p, (tri[1][0],tri[1][1]), (tri[2][0],tri[2][1])),
                point_segment_distance(p, (tri[2][0],tri[2][1]), (tri[0][0],tri[0][1])))
        if d < best_edge:
            best_edge = d; best = tri
    if best is not None and best_edge <= search_pad:
        bc=_barycentric_xy(p, best)
        if bc:
            l1,l2,l3=bc
            z = l1*best[0][2] + l2*best[1][2] + l3*best[2][2]
            return z, f"LandXML/TIN surface edge {surface.source}, point {best_edge:.2f} ft outside triangle", "SURFACE_REVIEW"

    # 2) Civil 3D PlanFeature/FeatureLine export. Use nearest 3D feature line segment.
    best_seg = None
    best_d = 1e99
    for seg in getattr(surface, 'segments', []) or []:
        d = point_segment_distance(p, seg.a, seg.b)
        if d < best_d:
            best_d = d
            best_seg = seg
    if best_seg is not None and best_d <= search_pad:
        z = segment_z_at_point(p, best_seg)
        conf = "FEATURELINE_HIGH" if best_d <= 0.25 else "FEATURELINE_REVIEW"
        return z, f"LandXML FeatureLine {best_seg.layer}, nearest {best_d:.2f} ft, {surface.source}", conf

    return None, 'LandXML surface/feature lines do not cover this point', ''



def valid_grade_z(z) -> bool:
    try:
        return math.isfinite(float(z)) and abs(float(z)) > 1e-9
    except (TypeError, ValueError):
        return False


def is_surface_layer(layer: str) -> bool:
    n = normalize_layer_name(layer)
    surface_terms = [
        "TOPOPSURFACE", "SURFACE", "TIN", "PROPOSEDSURFACE", "DESIGNSURFACE",
        "GRADESURFACE", "GRADINGSURFACE", "TPOSURFACE", "TOPOSURFACE"
    ]
    return any(t in n for t in surface_terms)


def segment_z_at_point(p: Point2, seg: ElevSegment) -> float:
    # Interpolate Z along a segment using projection of p onto the segment.
    ax, ay = seg.a
    bx, by = seg.b
    px, py = p
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return float(seg.z)
    t = max(0.0, min(1.0, (vx * wx + vy * wy) / c2))
    z1 = float(seg.z)
    z2 = float(seg.z2) if seg.z2 is not None else z1
    return z1 + (z2 - z1) * t


def layer_matches(layer: str, keywords: List[str]) -> bool:
    if not keywords:
        return False
    u = layer.upper()
    nu = normalize_layer_name(layer)
    for k in keywords:
        kk = k.strip()
        if not kk or kk.upper() == "AUTO":
            continue
        if kk.upper() in u or normalize_layer_name(kk) in nu:
            return True
    return False


def normalize_layer_name(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (name or "").upper())


def classify_curb_layer(layer: str) -> Tuple[str, str, bool]:
    """Return confidence, assumed meaning, auto_use.
    Auto use is intentionally conservative: only strong BC/back-of-curb names are used automatically.
    Generic curb layers are listed as medium so the user can intentionally include them.
    """
    raw = (layer or "").upper()
    n = normalize_layer_name(layer)

    exclude_terms = [
        "EXISTING", "EXIST", "DEMO", "DEMOLITION", "REMOVE", "REMOV", "RMV",
        "TOBEREMOVED", "ABANDON", "OLD", "HATCH", "TEXT", "NOTE", "ANNO", "LABEL"
    ]
    fc_terms = ["FACEOFCURB", "FOC", "FCURB", "FC", "EOP", "EDGEOFPAVEMENT", "PAVEMENTEDGE"]
    strong_terms = [
        "SITEPBC", "SITEBC", "PBC", "BOC", "BACKOFCURB", "BACKCURB",
        "CURBBACK", "TOPBACKOFCURB", "TBC", "BCURB", "BC"
    ]
    medium_terms = ["CURB", "CURBLINE", "SITECURB", "CCURB", "PCURB", "CURBANDGUTTER", "CG", "CANDG", "HARDSCAPE", "HARDSCAPES", "EHARDSCAPE", "PHARDSCAPE", "SITEHARDSCAPE"]

    # Existing/demo/remove layers should not be selected unless the user explicitly types the exact layer keyword.
    if any(t in n or t in raw for t in exclude_terms):
        return "EXCLUDED", "Existing/demo/annotation layer", False
    if any(t == n or t in n for t in fc_terms):
        return "LOW", "Face of curb / edge of pavement, not BC unless offset", False
    # Avoid classifying random words ending in BC as curb. Require exact BC token or strong phrase.
    tokens = re.split(r"[^A-Z0-9]+", raw)
    if any(t in n for t in strong_terms if t != "BC") or "BC" in tokens or n in ("BC", "PBC", "SITEBC", "SITEPBC"):
        return "HIGH", "Back of curb", True
    if any(t in n for t in medium_terms):
        return "MEDIUM", "Curb layer, verify BC or FC", False
    return "NO", "Not a curb layer", False


def should_use_curb_layer(layer: str, keywords: List[str]) -> Tuple[bool, str, str]:
    custom_keywords = [k for k in keywords if k.strip() and k.strip().upper() != "AUTO"]
    conf, meaning, auto_use = classify_curb_layer(layer)
    # Always keep excluded layers out unless user typed a direct matching custom keyword.
    custom_match = layer_matches(layer, custom_keywords)
    if conf == "EXCLUDED" and not custom_match:
        return False, conf, meaning
    if custom_match:
        if conf == "LOW":
            return True, "USER-LOW", meaning + " selected by user"
        if conf == "EXCLUDED":
            return True, "USER-OVERRIDE", meaning + " selected by user"
        return True, "USER" if conf in ("NO", "MEDIUM") else conf, meaning + " selected by keyword"
    if auto_use:
        return True, conf, meaning
    return False, conf, meaning


def detect_curb_layers(dxf_path: str, keywords: Optional[List[str]] = None, document=None) -> List[Dict[str, str]]:
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed. Double click install_requirements.bat first.")
    doc = document if document is not None else ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    layers = sorted(set(entity_layer(e) for e in msp if entity_layer(e)))
    out = []
    keywords = keywords or ["AUTO"]
    for layer in layers:
        use, conf, meaning = should_use_curb_layer(layer, keywords)
        if conf != "NO" or use:
            out.append({"layer": layer, "confidence": conf, "meaning": meaning, "use": "YES" if use else "NO"})
    order = {"HIGH":0, "USER":1, "USER-LOW":2, "USER-OVERRIDE":3, "MEDIUM":4, "LOW":5, "EXCLUDED":6, "NO":7}
    out.sort(key=lambda d: (order.get(d["confidence"], 9), d["layer"].upper()))
    return out



def load_selected_curb_handles(path: str, cad_path: str = '') -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, encoding='utf-8') as stream:
            data=json.load(stream)
    except Exception as ex:
        raise ValueError(f'Cannot read geometry selection: {ex}') from ex
    if cad_path:
        if not isinstance(data,dict) or not data.get('dxf_path'):
            raise ValueError('This selection has no drawing identity. Pick the geometry again for this DXF.')
        if os.path.normcase(os.path.abspath(data['dxf_path'])) != os.path.normcase(os.path.abspath(cad_path)):
            raise ValueError('Saved geometry belongs to another DXF. Pick geometry for the current drawing.')
        saved=data.get('dxf_sha256')
        if not saved or saved != drawing_fingerprint(cad_path):
            raise ValueError('DXF changed or the selection predates fingerprint validation. Pick the geometry again.')
    if isinstance(data,dict):
        return [str(h) for h in data.get('handles',[])]
    if isinstance(data,list) and not cad_path:
        return list(map(str,data))
    raise ValueError('Invalid geometry-selection file.')


def drawing_fingerprint(path):
    with open(path,'rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save_selected_curb_handles(path: str, handles: List[str], dxf_path: str = ""):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "version": "8.5.5",
            "purpose": "Manually selected DXF entity handles. Used for BC or FL/gutter depending on selection file.",
            "dxf_path": dxf_path,
            "dxf_sha256": drawing_fingerprint(dxf_path) if dxf_path else "",
            "handles": sorted(set(str(h) for h in handles)),
        }, f, indent=2)




def selected_handles_to_segments(dxf_path: str, handles: List[str], layer_fallback: str = "Manual Selection") -> List[CurbSegment]:
    """Return 2D segments from selected DXF entity handles.
    Used to let the user identify true back of curb and true gutter/flowline when
    an engineer puts all curb graphics on one generic layer like P-CURB.
    """
    if not dxf_path or not handles or ezdxf is None:
        return []
    hset = set(str(h) for h in handles if str(h).strip())
    out: List[CurbSegment] = []
    try:
        doc = ezdxf.readfile(dxf_path)
        for ent in doc.modelspace():
            h = str(getattr(ent.dxf, "handle", ""))
            if h not in hset:
                continue
            typ = ent.dxftype()
            try:
                if typ == "LINE":
                    pts = line_points(ent)
                elif typ in ("LWPOLYLINE", "POLYLINE"):
                    pts = polyline_points(ent, arc_spacing=2.0)
                elif typ == "ARC":
                    pts = arc_points_from_entity(ent, spacing=0.50)
                elif typ in ("SPLINE", "ELLIPSE", "CIRCLE", "HATCH", "INSERT"):
                    pts = picker_entity_points(ent, spacing=0.50)
                else:
                    continue
                lyr = entity_layer(ent) or layer_fallback
                for a, b in zip(pts[:-1], pts[1:]):
                    if dist(a, b) > 0.001:
                        out.append(CurbSegment(a, b, lyr))
            except Exception:
                continue
    except Exception:
        return []
    return out

def load_dxf_geometry_and_elevations(dxf_path: str, curb_keywords: List[str], elev_keywords: List[str], spacing: float, selected_curb_handles: Optional[List[str]] = None) -> Tuple[List[Point2], List[str], List[GradeLabel], List[ElevSegment], Dict[str, Any]]:
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed. Double click install_requirements.bat first.")
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    selected_handle_set = set(str(h) for h in (selected_curb_handles or []) if str(h).strip())
    manual_handle_mode = bool(selected_handle_set)
    global CURB_SEGMENTS, CURB_CONTROL_POINTS, CURB_PC_POINTS, CURB_PT_POINTS, DXF_LEADER_SEGMENTS
    CURB_SEGMENTS = []
    CURB_CONTROL_POINTS = []
    CURB_PC_POINTS = []
    CURB_PT_POINTS = []
    DXF_LEADER_SEGMENTS = []
    selected = []
    points: List[Point2] = []
    labels: List[GradeLabel] = []
    elev_segments: List[ElevSegment] = []
    stats = {"cad_grade_labels": 0, "elevation_segments": 0, "elevation_layers": {}, "all_layers": [], "curb_layer_candidates": []}
    all_layers = sorted(set(entity_layer(e) for e in msp))
    stats["all_layers"] = all_layers
    stats["curb_layer_candidates"] = detect_curb_layers(dxf_path, curb_keywords, document=doc)

    for ent in msp:
        layer = entity_layer(ent)
        typ = ent.dxftype()
        if typ in ("TEXT", "MTEXT"):
            raw = text_value(ent)
            parsed = parse_grade_text(raw)
            if parsed:
                try:
                    ins = ent.dxf.insert
                    x, y = float(ins.x), float(ins.y)
                except Exception:
                    x, y = 0.0, 0.0
                for kind, elev in parsed:
                    if valid_grade_z(elev):
                        labels.append(GradeLabel(x, y, kind, elev, raw, f"CAD text layer {layer}"))
        use_for_elev = layer_matches(layer, elev_keywords) or ("AUTO" in [k.upper() for k in elev_keywords] and (is_surface_layer(layer) or "CONT" in normalize_layer_name(layer)))
        if use_for_elev:
            try:
                # Surface/TIN/breakline layers may carry changing Z from endpoint to endpoint.
                # Contour layers usually carry constant Z.  GradeGuard stores both.
                if typ == "LINE":
                    a = (float(ent.dxf.start.x), float(ent.dxf.start.y))
                    b = (float(ent.dxf.end.x), float(ent.dxf.end.y))
                    z1 = float(ent.dxf.start.z) if valid_grade_z(ent.dxf.start.z) else entity_z(ent)
                    z2 = float(ent.dxf.end.z) if math.isfinite(float(ent.dxf.end.z)) else None
                    if z1 is not None and z2 is not None and dist(a, b) > 0.001:
                        elev_segments.append(ElevSegment(a, b, float(z1), layer, f"DXF surface/contour layer {layer}", float(z2)))
                        stats["elevation_layers"][layer] = stats["elevation_layers"].get(layer, 0) + 1
                elif typ == "POLYLINE":
                    verts = []
                    for v in ent.vertices:
                        loc = v.dxf.location
                        z = float(loc.z) if valid_grade_z(loc.z) else entity_z(ent)
                        if z is not None:
                            verts.append(((float(loc.x), float(loc.y)), float(z)))
                    for (a, za), (b, zb) in zip(verts[:-1], verts[1:]):
                        if dist(a, b) > 0.001:
                            elev_segments.append(ElevSegment(a, b, za, layer, f"DXF surface/contour layer {layer}", zb))
                            stats["elevation_layers"][layer] = stats["elevation_layers"].get(layer, 0) + 1
                elif typ == "LWPOLYLINE":
                    z = entity_z(ent)
                    if z is not None:
                        seg_pts = polyline_points(ent, arc_spacing=5.0)
                        for a, b in zip(seg_pts[:-1], seg_pts[1:]):
                            if dist(a, b) > 0.001:
                                elev_segments.append(ElevSegment(a, b, z, layer, f"DXF contour layer {layer}", z))
                                stats["elevation_layers"][layer] = stats["elevation_layers"].get(layer, 0) + 1
                elif typ == "ARC":
                    z = entity_z(ent)
                    if z is not None:
                        seg_pts = arc_points_from_entity(ent, spacing=8.0)
                        for a, b in zip(seg_pts[:-1], seg_pts[1:]):
                            if dist(a, b) > 0.001:
                                elev_segments.append(ElevSegment(a, b, z, layer, f"DXF contour arc layer {layer}", z))
                                stats["elevation_layers"][layer] = stats["elevation_layers"].get(layer, 0) + 1
                elif typ in ("3DFACE", "SOLID", "TRACE"):
                    pts = []
                    for attr in ["vtx0", "vtx1", "vtx2", "vtx3"]:
                        try:
                            v = getattr(ent.dxf, attr)
                            if valid_grade_z(v.z):
                                pts.append(((float(v.x), float(v.y)), float(v.z)))
                        except Exception:
                            pass
                    for (a, za), (b, zb) in zip(pts, pts[1:] + pts[:1]):
                        if dist(a, b) > 0.001:
                            elev_segments.append(ElevSegment(a, b, za, layer, f"DXF surface face layer {layer}", zb))
                            stats["elevation_layers"][layer] = stats["elevation_layers"].get(layer, 0) + 1
            except Exception:
                pass
        # Cache general leader/tick linework for HP/LP callout anchoring.
        # We intentionally skip the selected curb entities themselves and obvious contour/surface layers.
        try:
            nlayer = normalize_layer_name(layer)
            if typ in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "LEADER") and not any(t in nlayer for t in ["CONT", "CONTOUR", "SURFACE", "TIN", "FEATURE"]):
                if typ == "LINE":
                    lpts = line_points(ent)
                elif typ in ("LWPOLYLINE", "POLYLINE"):
                    lpts = polyline_points(ent, arc_spacing=6.0)
                elif typ == "LEADER":
                    # Civil 3D spot labels often export as LEADER entities.
                    # The first vertex is normally the arrow/tick point on the curb.
                    try:
                        lpts = [(float(v[0]), float(v[1])) for v in ent.vertices]
                    except Exception:
                        lpts = []
                else:
                    lpts = arc_points_from_entity(ent, spacing=6.0)
                for la, lb in zip(lpts[:-1], lpts[1:]):
                    if dist(la, lb) > 0.25:
                        DXF_LEADER_SEGMENTS.append(CurbSegment(la, lb, layer))
        except Exception:
            pass

        # If the user manually picked BC entities, use only those handles.
        # This solves projects where all curb objects are on one layer such as P-CURB.
        ent_handle = str(getattr(ent.dxf, "handle", ""))
        if manual_handle_mode:
            use_curb = ent_handle in selected_handle_set
            curb_conf = "MANUAL"
            curb_meaning = "User-picked back of curb entity"
        else:
            use_curb, curb_conf, curb_meaning = should_use_curb_layer(layer, curb_keywords)
        if not use_curb:
            continue
        selected.append(f"{layer} [{curb_conf}: {curb_meaning}]")
        try:
            if typ == "LINE":
                pts_line = line_points(ent)
                CURB_SEGMENTS.append(CurbSegment(pts_line[0], pts_line[1], layer))
                stake_pts, control_pts = staking_points_from_entity(ent, spacing)
                pc_pts, pt_pts = pc_pt_points_from_entity(ent)
                CURB_PC_POINTS.extend(pc_pts)
                CURB_PT_POINTS.extend(pt_pts)
                points.extend(stake_pts)
                CURB_CONTROL_POINTS.extend(control_pts)
            elif typ in ("LWPOLYLINE", "POLYLINE"):
                # Dense tessellation is ONLY for intersections and picking.
                # Stakeout points on curves are PC, midpoint, and PT only.
                dense_pts = polyline_points(ent, arc_spacing=2.0)
                if len(dense_pts) >= 2:
                    for aa, bb in zip(dense_pts[:-1], dense_pts[1:]):
                        if dist(aa, bb) > 0.001:
                            CURB_SEGMENTS.append(CurbSegment(aa, bb, layer))
                stake_pts, control_pts = staking_points_from_entity(ent, spacing)
                pc_pts, pt_pts = pc_pt_points_from_entity(ent)
                CURB_PC_POINTS.extend(pc_pts)
                CURB_PT_POINTS.extend(pt_pts)
                points.extend(stake_pts)
                CURB_CONTROL_POINTS.extend(control_pts)
            elif typ == "ARC":
                dense_pts = arc_points_from_entity(ent, spacing=0.50)
                for aa, bb in zip(dense_pts[:-1], dense_pts[1:]):
                    if dist(aa, bb) > 0.001:
                        CURB_SEGMENTS.append(CurbSegment(aa, bb, layer))
                stake_pts, control_pts = staking_points_from_entity(ent, spacing)
                pc_pts, pt_pts = pc_pt_points_from_entity(ent)
                CURB_PC_POINTS.extend(pc_pts)
                CURB_PT_POINTS.extend(pt_pts)
                points.extend(stake_pts)
                CURB_CONTROL_POINTS.extend(control_pts)
            elif typ in ("SPLINE", "ELLIPSE", "CIRCLE", "HATCH", "INSERT"):
                dense_pts = picker_entity_points(ent, spacing=0.50)
                for aa, bb in zip(dense_pts[:-1], dense_pts[1:]):
                    if dist(aa, bb) > 0.001:
                        CURB_SEGMENTS.append(CurbSegment(aa, bb, layer))
                stake_pts, control_pts = staking_points_from_entity(ent, spacing)
                pc_pts, pt_pts = pc_pt_points_from_entity(ent)
                CURB_PC_POINTS.extend(pc_pts)
                CURB_PT_POINTS.extend(pt_pts)
                points.extend(stake_pts)
                CURB_CONTROL_POINTS.extend(control_pts)
        except Exception:
            continue
    stats["cad_grade_labels"] = len(labels)
    stats["elevation_segments"] = len(elev_segments)
    stats["curb_segments"] = len(CURB_SEGMENTS)
    stats["manual_picked_curb_entities"] = len(selected_handle_set) if manual_handle_mode else 0
    return dedupe_points(points), sorted(set(selected)), labels, elev_segments, stats


def project_point_on_segment(p: Point2, a: Point2, b: Point2) -> Point2:
    """Return the closest point on segment AB to point P.

    v4.7 bug fix: some HP/LP and contour crossing code called this helper,
    but the build only had closest_point_on_segment(). This wrapper keeps the
    existing geometry logic stable and prevents the export crash.
    """
    ax, ay = a
    bx, by = b
    px, py = p
    vx, vy = bx - ax, by - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return a
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / c2))
    return (ax + t * vx, ay + t * vy)


def snap_points_to_selected_curb_geometry(points: List[Point2], max_snap: float = 3.0) -> Tuple[List[Point2], int, float]:
    """Snap generated BC points back onto the saved/picked BC geometry.

    v7.4 fix: some curve midpoint points were being created from the right
    entity type but could still plot slightly off the visible BC chain when the
    DXF contained exploded/parallel hardscape arcs. Before assigning elevations
    or writing the CSV, force every generated BC point to the closest point on
    the selected BC segment chain. This keeps PC/MID/PT and 25 ft points on the
    exact linework the user saved as back of curb.
    """
    global CURB_SEGMENTS
    if not points or not CURB_SEGMENTS:
        return points, 0, 0.0
    snapped: List[Point2] = []
    count = 0
    max_move = 0.0
    for p in points:
        best_q = p
        best_d = None
        for seg in CURB_SEGMENTS:
            q = project_point_on_segment(p, seg.a, seg.b)
            d = dist(p, q)
            if best_d is None or d < best_d:
                best_d = d
                best_q = q
        if best_d is not None and best_d <= max_snap:
            if best_d > 0.001:
                count += 1
                max_move = max(max_move, best_d)
            snapped.append(best_q)
        else:
            snapped.append(p)
    return dedupe_points(snapped, tol=0.03), count, max_move


def is_contour_crossing_source(seg: ElevSegment) -> bool:
    """True when an elevation segment is a contour or contour-like line.

    GradeGuard should add a BC point anywhere a real contour crosses the selected
    back-of-curb line. Do not use dense TIN/surface triangle edges for this rule,
    because those can create hundreds of unnecessary points. Surface/TIN is still
    used to calculate elevations, but contour crossings become staking points.
    """
    name = normalize_layer_name((seg.layer or "") + " " + (seg.source or ""))
    if "CONT" in name or "CONTOUR" in name or "INDEXCONT" in name or "INTERCONT" in name:
        return True
    # Many Civil 3D contour exports are constant-Z polylines on grading/elevation layers.
    if seg.z2 is not None and abs(float(seg.z2) - float(seg.z)) <= 1e-6:
        if any(t in name for t in ["GRADE", "ELEV", "TOPO"]):
            return True
    return False


def _nearest_point_on_curb(p: Point2, max_dist: float = 8.0) -> Tuple[Optional[Point2], float]:
    """Project a point, usually an FL/gutter contour crossing, to the nearest selected BC segment.

    When contours cross the gutter line, the stake still needs to be placed on BC.
    This helper finds the BC point at the same curb station as closely as possible.
    """
    best = None
    best_d = 1e99
    for cs in CURB_SEGMENTS:
        q = project_point_on_segment(p, cs.a, cs.b)
        d = dist(p, q)
        if d < best_d:
            best = q
            best_d = d
    if best is not None and best_d <= max_dist:
        return best, best_d
    return None, best_d


def add_curb_geometry_control_points(base_points: List[Point2], tolerance: float = 0.05) -> Tuple[List[Point2], int]:
    """Ensure BC points exist at curb geometry controls.

    This covers line endpoints, polyline vertices, and the PC/PT of curb returns.
    The DXF may draw arcs as true arcs or as sampled segments. Either way, the
    segment endpoints are treated as required geometry control points.
    """
    pts = list(base_points)
    added = 0
    controls = CURB_CONTROL_POINTS if CURB_CONTROL_POINTS else []
    for q in controls:
        if not any(dist(q, existing) <= tolerance for existing in pts):
            pts.append(q)
            added += 1
    return dedupe_points(pts), added


def get_current_curb_height_for_overrides() -> float:
    try:
        return float(CURRENT_CURB_HEIGHT)
    except Exception:
        return 0.50

def point_override_key(p: Point2) -> Tuple[int, int]:
    """Rounded coordinate key for exact contour-crossing overrides."""
    return (int(round(float(p[0]) * 1000.0)), int(round(float(p[1]) * 1000.0)))

def set_contour_point_override(p: Point2, elev: float, src: str, conf: str, issue: str = "") -> None:
    key = point_override_key(p)
    old = CONTOUR_POINT_OVERRIDES.get(key)
    if old and old[2] == 'BC_CONTOUR_EXACT' and conf != 'BC_CONTOUR_EXACT':
        return
    if old and old[2] == conf and abs(old[0] - elev) > 1e-6:
        CONTOUR_POINT_OVERRIDES[key] = (float(elev), src, 'CONTROL_CONFLICT_REVIEW', 'Conflicting exact contour elevations at one point.')
        return
    CONTOUR_POINT_OVERRIDES[key] = (float(elev), src, conf, issue)

def get_contour_point_override(p: Point2) -> Optional[Tuple[float, str, str, str]]:
    return CONTOUR_POINT_OVERRIDES.get(point_override_key(p))

def add_contour_crossing_bc_points(base_points: List[Point2], elev_segments: List[ElevSegment], tolerance: float = 0.35) -> Tuple[List[Point2], int, int]:
    """Add BC points anywhere contours cross the curb.

    Hard staking rules:
    1) If a contour physically crosses/touches the selected BACK OF CURB line,
       place a BC point at that exact crossing. The contour elevation is the BC elevation.
    2) If a contour physically crosses/touches the selected FLOWLINE/GUTTER line,
       place a BC point at the nearest station on the selected BC line. The FL contour
       elevation becomes BC only after adding the verified curb height.

    This prevents missing grade-control points that fall between the normal 25 ft interval.
    """
    global CURB_SEGMENTS, FLOWLINE_SEGMENTS, CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF
    pts = list(base_points)
    added_bc = 0
    added_fl = 0
    if not CURB_SEGMENTS or not elev_segments:
        return dedupe_points(pts), added_bc, added_fl

    contour_segments = [es for es in elev_segments if is_contour_crossing_source(es)]

    # Contours crossing true back of curb: add the actual crossing point.
    for cs in CURB_SEGMENTS:
        for es in contour_segments:
            inter = segment_intersection(cs.a, cs.b, es.a, es.b)
            if inter is None:
                continue
            _t, _u, q = inter
            z = segment_z_at_point(q, es)
            set_contour_point_override(q, z, f"{z:.3f} exact contour crossing selected BACK OF CURB on {es.layer}", "BC_CONTOUR_EXACT", "Contour crossed selected BACK OF CURB, so GradeGuard used the contour elevation directly.")
            if any(dist(q, existing) <= tolerance for existing in pts):
                continue
            pts.append(q)
            added_bc += 1

    # Contours crossing selected flowline/gutter: add a point on BC at the matching station.
    for fs in FLOWLINE_SEGMENTS:
        for es in contour_segments:
            inter = segment_intersection(fs.a, fs.b, es.a, es.b)
            if inter is None:
                continue
            _t, _u, q_fl = inter
            q_bc, d_bc = _nearest_point_on_curb(q_fl, max_dist=12.0)
            if q_bc is None:
                continue
            z_fl = segment_z_at_point(q_fl, es)
            if not CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF:
                # Add the station point, but do not give it a believable elevation.
                # The field CSV will show 0.000 through the safety rules.
                if not any(dist(q_bc, existing) <= tolerance for existing in pts):
                    pts.append(q_bc)
                    added_fl += 1
                continue
            z_bc = z_fl + get_current_curb_height_for_overrides()
            set_contour_point_override(q_bc, z_bc, f"{z_bc:.3f} from contour crossing selected FLOWLINE/GUTTER on {es.layer}: {z_fl:.3f} + PDF verified curb height {get_current_curb_height_for_overrides():.2f}", "FL_CONTOUR_PLUS_CURB_EXACT", "Contour crossed selected FLOWLINE/GUTTER/EP, so GradeGuard added the PDF verified curb height to compute BC.")
            if any(dist(q_bc, existing) <= tolerance for existing in pts):
                continue
            pts.append(q_bc)
            added_fl += 1

    return dedupe_points(pts), added_bc, added_fl


def _nearest_curb_projection_any(p: Point2) -> Tuple[Optional[Point2], float]:
    best = None
    best_d = 1e99
    for cs in CURB_SEGMENTS:
        q = project_point_on_segment(p, cs.a, cs.b)
        d = dist(p, q)
        if d < best_d:
            best = q
            best_d = d
    return best, best_d


def _callout_tick_point_from_leaders(label: GradeLabel, max_label_dist: float = 95.0, max_curb_dist: float = 3.0) -> Optional[Point2]:
    """Find the actual leader/tick endpoint for a HP/LP callout.

    Civil sheets often store the text insertion point away from the curb. The
    true control location is the arrow/leader endpoint. We search nearby CAD
    leader/tick linework and use the endpoint closest to the selected BC.
    """
    if not DXF_LEADER_SEGMENTS or not CURB_SEGMENTS:
        return None
    lp = (label.x, label.y)
    best_q = None
    best_score = 1e99
    for seg in DXF_LEADER_SEGMENTS:
        # Only consider leader segments reasonably close to the label text.
        near_label = min(dist(lp, seg.a), dist(lp, seg.b), point_segment_distance(lp, seg.a, seg.b))
        if near_label > max_label_dist:
            continue
        for cand in (seg.a, seg.b):
            q, dc = _nearest_curb_projection_any(cand)
            if q is None or dc > max_curb_dist:
                continue
            # Prefer a candidate that is close to the curb and whose leader is close to the text.
            score = dc * 10.0 + near_label + dist(cand, q)
            if score < best_score:
                best_score = score
                best_q = q
    return best_q


def add_high_low_callout_points(base_points: List[Point2], labels: List[GradeLabel], tolerance: float = 0.35, max_label_to_curb_dist: float = 140.0) -> Tuple[List[Point2], List[GradeLabel], int, int]:
    """Add a staking point where a HP/LP callout controls the curb.

    GradeGuard normally creates points at PC/MID/PT, spacing stations, and contour
    crossings. That can miss a true high point or low point when the plan callout
    falls between normal stations. This routine fixes that by using CAD/PDF HP/LP
    callouts as grade-control anchors:

    - Find HP, LP, HP/TC, or LP/TC labels.
    - Project the label/tick coordinate to the nearest selected back-of-curb segment.
    - Add a BC point at that projected curb location when it is not already present.
    - Add a second anchored GradeLabel at the projected point so the elevation held
      at that point is exactly the plan callout value.

    PDF text boxes are still treated carefully. They only become generated points
    when they can be projected to selected BC geometry inside the distance limit.
    """
    pts = list(base_points)
    anchored_labels: List[GradeLabel] = []
    added_hp = 0
    added_lp = 0
    if not CURB_SEGMENTS or not labels:
        return dedupe_points(pts), anchored_labels, added_hp, added_lp

    hp_lp_kinds = {"HP", "LP", "HP_TC", "LP_TC"}
    used: List[Point2] = []
    for lab in labels:
        if (lab.kind or "").upper() not in hp_lp_kinds:
            continue
        # First try to use the CAD leader/tick endpoint. If no usable leader is found,
        # fall back to projecting the text insertion point to the selected BC.
        q = _callout_tick_point_from_leaders(lab)
        if q is None:
            q, d = _nearest_point_on_curb((lab.x, lab.y), max_dist=0.35)
            if q is None:
                continue
        # Avoid repeated HP/LP anchors from duplicate text objects.
        if any(dist(q, u) <= max(2.0, tolerance * 4.0) for u in used):
            continue
        used.append(q)
        if not any(dist(q, existing) <= tolerance for existing in pts):
            pts.append(q)
            if lab.kind.upper().startswith("HP"):
                added_hp += 1
            else:
                added_lp += 1
        else:
            # The point already exists, but still add the projected label so that
            # the description and elevation can hold as HP/LP if it is the nearest control.
            if lab.kind.upper().startswith("HP"):
                added_hp += 0
            else:
                added_lp += 0
        anchored_labels.append(GradeLabel(q[0], q[1], lab.kind, lab.elev, lab.raw, f"{lab.source} projected to selected BC as HP/LP callout hold"))
    return dedupe_points(pts), anchored_labels, added_hp, added_lp


def tag_high_low_points(points: List[GenPoint], max_gap: float = 55.0, min_change: float = 0.02):
    """Flag local extrema for review without changing a design grade or a stake description."""
    before, after = {}, {}
    for a, b, distance in diagnostic_pairs(points):
        if 0.1 < distance <= max_gap:
            after[id(a)] = b
            before[id(b)] = a
    for cur in points:
        prev, nxt = before.get(id(cur)), after.get(id(cur))
        if prev is None or nxt is None or cur.locked:
            continue
        kind = ''
        if cur.elevation >= prev.elevation + min_change and cur.elevation >= nxt.elevation + min_change:
            kind = 'HIGH'
        elif cur.elevation <= prev.elevation - min_change and cur.elevation <= nxt.elevation - min_change:
            kind = 'LOW'
        if kind:
            note = f'Possible {kind} point from adjacent grades; verify the physical plan control and drainage.'
            cur.issue = (cur.issue + ' ' if cur.issue else '') + note



def parse_pdf_detail_rules_text(txt: str) -> Dict[str, Any]:
    """Read curb/sidewalk detail clues from PDF text.

    This is intentionally conservative. It does not automatically change point
    elevations from a detail note unless the source being converted is a true
    FL/gutter source. It records project-specific curb/sidewalk clues in the
    report so the user can verify the curb height, sidewalk thickness, header
    curb, turn-down curb, and ramp/transition areas.
    """
    up = (txt or "").upper().replace("\n", " ")
    rules = []
    def add(kind, value_ft, raw):
        rules.append({"kind": kind, "value_ft": value_ft, "raw": raw[:160]})
    # Common curb heights. 6 inches = 0.50 ft, 4 inches = 0.333 ft.
    for m in re.finditer(r'(\b\d+(?:\.\d+)?)\s*(?:IN|INCH|INCHES|\")\s+(?:VERTICAL\s+)?(?:CURB|HEADER\s+CURB|CURB\s+HEIGHT)', up):
        inches = float(m.group(1)); add("curb_height", inches/12.0, m.group(0))
    for m in re.finditer(r'(?:CURB|HEADER\s+CURB|CURB\s+HEIGHT)[^\d]{0,20}(\d+(?:\.\d+)?)\s*(?:IN|INCH|INCHES|\")', up):
        inches = float(m.group(1)); add("curb_height", inches/12.0, m.group(0))
    if "6\"" in up and "CURB" in up:
        add("curb_height_possible", 0.50, "6 inch curb note/detail found")
    if "4\"" in up and "CURB" in up:
        add("curb_height_possible", 4.0/12.0, "4 inch curb note/detail found")
    if "HEADER CURB" in up:
        add("curb_type", None, "HEADER CURB detail/note found, do not blindly use standard 0.50 ft")
    if "TURN DOWN" in up or "TURNDOWN" in up:
        add("sidewalk_turn_down", None, "Sidewalk turn-down detail/note found")
    if "SIDEWALK" in up:
        for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(?:IN|INCH|INCHES|\")\s+(?:CONCRETE\s+)?SIDEWALK', up):
            add("sidewalk_thickness", float(m.group(1))/12.0, m.group(0))
        for m in re.finditer(r'SIDEWALK[^\d]{0,25}(\d+(?:\.\d+)?)\s*(?:IN|INCH|INCHES|\")', up):
            add("sidewalk_thickness", float(m.group(1))/12.0, m.group(0))
    if "HANDICAP" in up or "ADA" in up or "RAMP" in up:
        add("transition_or_ramp", None, "ADA/ramp/transition note found, review curb height/edge elevations")
    return {"rules": rules}


def scan_pdf_for_curb_terms(pdf_path: str) -> Dict[str, Any]:
    result = {"curb_terms": [], "grade_terms": [], "possible_detail_pages": [], "grade_values": [], "detail_rules": []}
    if not pdf_path or not os.path.exists(pdf_path):
        return result
    if fitz is None:
        result["error"] = "PyMuPDF is not installed."
        return result
    terms = ["CURB", "GUTTER", "HEADER CURB", "TURN DOWN CURB", "FLOW LINE", "TOP OF CURB", "T.C", "F.L", "HP", "LP", "MATCH EXISTING"]
    detail_terms = ["DETAIL", "CURB AND GUTTER", "HEADER CURB", "TURN DOWN", "SIDEWALK"]
    doc = fitz.open(pdf_path)
    for i, page in enumerate(doc):
        txt = page.get_text("text")
        up = txt.upper()
        detail_rules = parse_pdf_detail_rules_text(txt).get("rules", [])
        for r in detail_rules:
            r = dict(r); r["page"] = i + 1; result["detail_rules"].append(r)
        hits = [t for t in terms if t in up]
        if hits:
            result["curb_terms"].append({"page": i+1, "hits": hits[:12]})
        if any(t in up for t in detail_terms):
            result["possible_detail_pages"].append(i+1)
        parsed = parse_grade_text(txt)
        for kind, elev in parsed:
            result["grade_terms"].append({"page": i+1, "kind": kind, "elev": elev})
            result["grade_values"].append(elev)
    return result





def determine_verified_curb_height_from_pdf(pdf_scan: Dict[str, Any], user_default: float = 0.50) -> Tuple[float, bool, str]:
    """Return the curb height that is safe to use for FL/gutter to BC conversion.

    GradeGuard's field-safety rule is that the certified civil PDF governs over
    CAD/DXF.  A flowline/gutter contour can only be raised to BC when the plan
    PDF gives a usable curb height/detail.  If the PDF contains multiple or
    conflicting curb heights, the program keeps the user's value only as a
    review item and will not treat FL/gutter controls as high-confidence.
    """
    rules = (pdf_scan or {}).get("detail_rules", []) or []
    exact = []
    possible = []
    review_terms = []
    for r in rules:
        k = str(r.get("kind", "")).lower()
        v = r.get("value_ft")
        raw = str(r.get("raw", ""))
        if k == "curb_height" and isinstance(v, (int, float)):
            exact.append(float(v))
        elif k == "curb_height_possible" and isinstance(v, (int, float)):
            possible.append(float(v))
        elif k in ("curb_type", "transition_or_ramp", "sidewalk_turn_down"):
            review_terms.append(raw)
    # If one explicit height is found, it governs, but only when it is a plausible curb
    # vertical height.  This prevents notes such as pipe sizes, slopes, or dimensions
    # near curb details from being accidentally treated as a 1.50 ft curb height.
    plausible_exact = [x for x in exact if 0.25 <= float(x) <= 0.75]
    rejected_exact = [x for x in exact if not (0.25 <= float(x) <= 0.75)]
    unique_exact = sorted(set(round(x, 4) for x in plausible_exact))
    if len(unique_exact) == 1:
        note = f"PDF detail verified curb height {unique_exact[0]:.3f} ft."
        if rejected_exact:
            note += f" Ignored non-curb-height PDF dimensions {sorted(set(round(x,4) for x in rejected_exact))}."
        return float(unique_exact[0]), True, note
    # If no exact height but one likely 6 inch note is found, use it but keep a warning.
    unique_possible = sorted(set(round(x, 4) for x in possible if 0.25 <= float(x) <= 0.75))
    if not unique_exact and len(unique_possible) == 1:
        return float(unique_possible[0]), True, f"PDF likely curb height {unique_possible[0]:.3f} ft from detail notes. Verify if catch/spill/header curb."
    if len(unique_exact) > 1:
        return float(user_default), False, f"PDF has multiple plausible curb heights {unique_exact}. FL/gutter derived elevations will be zeroed unless manually reviewed."
    if rejected_exact:
        return float(user_default), False, f"PDF dimensions {sorted(set(round(x,4) for x in rejected_exact))} were found near curb details but are outside the safe curb-height range. Using user curb height for review only unless manually verified."
    return float(user_default), False, "No verified curb height was read from the certified PDF. FL/gutter derived BC elevations will be zeroed for review."

def find_plan_page(doc, sheet_code: str, title_keyword: str, fallback_index: int) -> int:
    sheet_code = sheet_code.upper()
    title_keyword = title_keyword.upper()
    best_i = None
    best_score = -1
    for i, page in enumerate(doc):
        txt = page.get_text("text").upper()
        score = 0
        if sheet_code in txt:
            score += 3
        if title_keyword in txt:
            score += 3
        if "SHEET LIST" in txt:
            score -= 5
        if "COVER" in txt:
            score -= 3
        # For layout page, coordinate callouts are a strong signal.
        if title_keyword.startswith("LAYOUT"):
            score += len(re.findall(r"N:\s*[0-9]{6,8}\.[0-9]+\s+E:\s*[0-9]{5,8}\.[0-9]+", txt)) * 4
        # For grading page, spot grades are a strong signal.
        if title_keyword.startswith("GRADING"):
            score += len(re.findall(r"(T\.?C\.?|F\.?L\.?|F\.?G\.?|H\.?P\.?|L\.?P\.?)", txt))
        if score > best_score:
            best_score = score
            best_i = i
    if best_i is not None and best_score > 0:
        return best_i
    if 0 <= fallback_index < len(doc):
        return fallback_index
    return 0

def block_text(block) -> str:
    return " ".join("".join(span.get("text", "") for span in line.get("spans", [])) for line in block.get("lines", []))


def affine_from_layout_coordinates(pdf_path: str) -> Tuple[Optional[Tuple[List[float], List[float]]], Dict[str, Any]]:
    """Build a rough PDF sheet to CAD coordinate transform using the layout coordinate callouts on C-4.0.
    This is approximate because the text boxes are not always placed exactly on the leader point.
    """
    info = {"layout_page": None, "anchors": 0, "rmse_ft": None, "warning": ""}
    if fitz is None or not pdf_path or not os.path.exists(pdf_path):
        return None, info
    try:
        import numpy as np
    except Exception:
        info["warning"] = "numpy not available, PDF spot alignment disabled"
        return None, info
    doc = fitz.open(pdf_path)
    pi = find_plan_page(doc, "C-4.0", "LAYOUT", 4)
    # If the guessed page does not contain enough coordinate callouts, scan all pages and use the page with the most.
    def count_coord_blocks(page):
        c = 0
        for bb in page.get_text("dict").get("blocks", []):
            if bb.get("type") == 0 and re.search(r"N:\s*[0-9]{6,8}\.[0-9]+\s+E:\s*[0-9]{5,8}\.[0-9]+", block_text(bb)):
                c += 1
        return c
    if count_coord_blocks(doc[pi]) < 3:
        counts = [(count_coord_blocks(pg), idx) for idx, pg in enumerate(doc)]
        pi = max(counts)[1]
    info["layout_page"] = pi + 1
    anchors = []
    for b in doc[pi].get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        txt = block_text(b)
        m = re.search(r"N:\s*([0-9]{6,8}\.[0-9]+)\s+E:\s*([0-9]{5,8}\.[0-9]+)", txt)
        if not m:
            continue
        x0, y0, x1, y1 = b["bbox"]
        px, py = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        n = float(m.group(1)); e = float(m.group(2))
        anchors.append((px, py, e, n, txt))
    info["anchors"] = len(anchors)
    if len(anchors) < 3:
        info["warning"] = "Not enough coordinate callouts found on layout sheet for PDF spot alignment"
        return None, info
    A = np.array([[a[0], a[1], 1.0] for a in anchors], dtype=float)
    E = np.array([a[2] for a in anchors], dtype=float)
    N = np.array([a[3] for a in anchors], dtype=float)
    # Text centers provide a preview transform, not controlled physical tick positions.
    ce = np.linalg.lstsq(A, E, rcond=None)[0]
    cn = np.linalg.lstsq(A, N, rcond=None)[0]
    errs = []
    for a in anchors:
        v = np.array([a[0], a[1], 1.0])
        errs.append(math.hypot(float(ce @ v) - a[2], float(cn @ v) - a[3]))
    rmse = math.sqrt(sum(e*e for e in errs) / len(errs)) if errs else None
    info["rmse_ft"] = rmse
    info["warning"] = "Text-center alignment and cross-sheet registration are unverified. Automatic PDF grades are review-only; use explicit CAD tick controls."
    doc.close()
    return (ce.tolist(), cn.tolist()), info




def extract_layout_alignment_points(pdf_path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Find coordinate callouts on the C-4.0 layout sheet and return PDF=>CAD alignment points.
    This auto-scales and rotates the PDF to CAD so the user does not have to manually pick alignment points.
    """
    info = {"layout_page": None, "anchors": 0, "warning": ""}
    pts: List[Dict[str, Any]] = []
    if fitz is None or not pdf_path or not os.path.exists(pdf_path):
        info["warning"] = "PDF not available or PyMuPDF not installed."
        return pts, info
    try:
        doc = fitz.open(pdf_path)
        pi = find_plan_page(doc, "C-4.0", "LAYOUT", 4)
        def count_coord_blocks(page):
            c = 0
            for bb in page.get_text("dict").get("blocks", []):
                if bb.get("type") == 0 and re.search(r"N:\s*[0-9]{6,8}\.[0-9]+\s+E:\s*[0-9]{5,8}\.[0-9]+", block_text(bb)):
                    c += 1
            return c
        if count_coord_blocks(doc[pi]) < 3:
            counts = [(count_coord_blocks(pg), idx) for idx, pg in enumerate(doc)]
            pi = max(counts)[1]
        info["layout_page"] = pi + 1
        for b in doc[pi].get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            txt = block_text(b)
            m = re.search(r"N:\s*([0-9]{6,8}\.[0-9]+)\s+E:\s*([0-9]{5,8}\.[0-9]+)", txt)
            if not m:
                continue
            x0, y0, x1, y1 = b["bbox"]
            px, py = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            n = float(m.group(1)); e = float(m.group(2))
            pts.append({"page": pi + 1, "pdf": [px, py], "cad": [e, n], "raw": txt.strip()[:160]})
        info["anchors"] = len(pts)
        if len(pts) < 2:
            info["warning"] = "Not enough layout coordinate callouts found for auto PDF match."
        return pts, info
    except Exception as e:
        info["warning"] = f"Auto PDF match failed: {e}"
        return pts, info


def apply_affine(model, px: float, py: float) -> Point2:
    ce, cn = model
    e = ce[0] * px + ce[1] * py + ce[2]
    n = cn[0] * px + cn[1] * py + cn[2]
    return (float(e), float(n))



# -----------------------------
# v8.4.4 PDF-to-DXF auto alignment and leader endpoint extraction
# -----------------------------

def _pdf_page_line_segments(page) -> List[Tuple[Point2, Point2]]:
    """Return vector line segments from a PDF page in PDF page coordinates.

    PyMuPDF exposes most CAD-exported PDF leaders, ticks, contour fragments, and
    curb linework through page.get_drawings().  We only need the endpoints here:
    the exact graphics are not changed.  This is used so PDF grade labels can be
    anchored at the arrow/leader/tick endpoint instead of the text box center.
    """
    segs: List[Tuple[Point2, Point2]] = []
    if fitz is None:
        return segs
    try:
        for d in page.get_drawings():
            last = None
            for item in d.get("items", []) or []:
                if not item:
                    continue
                op = item[0]
                try:
                    if op == "l" and len(item) >= 3:
                        p1 = item[1]; p2 = item[2]
                        a = (float(p1.x), float(p1.y)); b = (float(p2.x), float(p2.y))
                        if math.hypot(a[0]-b[0], a[1]-b[1]) > 0.5:
                            segs.append((a, b))
                    elif op == "re" and len(item) >= 2:
                        r = item[1]
                        pts = [(float(r.x0), float(r.y0)), (float(r.x1), float(r.y0)), (float(r.x1), float(r.y1)), (float(r.x0), float(r.y1))]
                        for a, b in zip(pts, pts[1:] + pts[:1]):
                            segs.append((a, b))
                    elif op in ("c", "qu"):
                        # Curves are not leaders most of the time.  Use the start/end if PyMuPDF supplies them.
                        pts = []
                        for v in item[1:]:
                            if hasattr(v, "x") and hasattr(v, "y"):
                                pts.append((float(v.x), float(v.y)))
                        if len(pts) >= 2:
                            a, b = pts[0], pts[-1]
                            if math.hypot(a[0]-b[0], a[1]-b[1]) > 0.5:
                                segs.append((a, b))
                except Exception:
                    continue
    except Exception:
        pass
    return segs


def _point_to_pdf_bbox_distance(pt: Point2, bbox) -> float:
    x, y = pt
    x0, y0, x1, y1 = [float(v) for v in bbox]
    dx = 0.0 if x0 <= x <= x1 else min(abs(x-x0), abs(x-x1))
    dy = 0.0 if y0 <= y <= y1 else min(abs(y-y0), abs(y-y1))
    return math.hypot(dx, dy)


def _pdf_leader_tip_for_text_block(page, bbox, max_leader_dist: float = 95.0, line_segments=None) -> Optional[Point2]:
    """Find the probable arrow/tick endpoint for a PDF grade callout text block.

    Rule: the text box does not hold.  The arrow, leader, or tick endpoint holds.
    We search PDF vector segments near the text and choose the endpoint farthest
    away from the text block center, because CAD/PDF leaders generally run from
    the label toward the physical control point.
    """
    segs = _pdf_page_line_segments(page) if line_segments is None else line_segments
    if not segs:
        return None
    x0, y0, x1, y1 = [float(v) for v in bbox]
    cx, cy = (x0+x1)/2.0, (y0+y1)/2.0
    best = None
    best_score = 1e99
    for a, b in segs:
        near = min(_point_to_pdf_bbox_distance(a, bbox), _point_to_pdf_bbox_distance(b, bbox))
        # Also allow a leader that passes close to the text box without ending inside it.
        near = min(near, point_segment_distance((cx, cy), a, b))
        if near > max_leader_dist:
            continue
        la = math.hypot(a[0]-cx, a[1]-cy)
        lb = math.hypot(b[0]-cx, b[1]-cy)
        tip = a if la > lb else b
        tip_dist = max(la, lb)
        length = math.hypot(a[0]-b[0], a[1]-b[1])
        if length < 4.0:
            continue
        # Lower score wins: close to text, long enough leader, tip away from text.
        score = near * 10.0 - min(tip_dist, 200.0) * 0.15 - min(length, 200.0) * 0.05
        if score < best_score:
            best_score = score
            best = tip
    return best


def _pdf_vector_bbox_for_page(page) -> Optional[Tuple[float, float, float, float]]:
    segs = _pdf_page_line_segments(page)
    if not segs:
        return None
    xs, ys = [], []
    r = page.rect
    # Filter out obvious sheet border/titleblock only lines by ignoring full-page border length.
    for a, b in segs:
        length = math.hypot(a[0]-b[0], a[1]-b[1])
        if length > max(float(r.width), float(r.height)) * 0.85:
            continue
        for p in (a, b):
            # Keep plan graphics inside the page.  A small margin helps avoid title border noise.
            if 5 <= p[0] <= r.width - 5 and 5 <= p[1] <= r.height - 5:
                xs.append(p[0]); ys.append(p[1])
    if len(xs) < 8:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _cad_bbox_from_reference_points(cad_reference_points: Optional[List[Point2]]) -> Optional[Tuple[float, float, float, float]]:
    pts = cad_reference_points or []
    clean = [(float(x), float(y)) for x, y in pts if math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(clean) < 2:
        return None
    xs = [p[0] for p in clean]; ys = [p[1] for p in clean]
    if max(xs)-min(xs) < 1e-6 or max(ys)-min(ys) < 1e-6:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def fallback_pdf_to_cad_model_from_extents(pdf_path: str, grading_page_index: int, cad_reference_points: Optional[List[Point2]]) -> Tuple[Optional[Tuple[List[float], List[float]]], Dict[str, Any]]:
    """Fallback PDF=>CAD model when the PDF has no coordinate callouts.

    This does NOT scale or move the DXF.  It scales, flips, and shifts the PDF
    graphics to the CAD/DXF extents.  It is approximate, but it lets GradeGuard
    use the PDF leader endpoint as the initial callout location and then project
    to the selected BC chain.  The review report marks this as an automatic
    extent match so the user knows it was not a survey control transformation.
    """
    info = {"method": "none", "warning": "", "rmse_ft": None, "pdf_scale_ft_per_point": None}
    if fitz is None or not pdf_path or not os.path.exists(pdf_path):
        info["warning"] = "PDF unavailable for auto PDF/DXF match."
        return None, info
    cadbb = _cad_bbox_from_reference_points(cad_reference_points)
    if cadbb is None:
        info["warning"] = "Not enough CAD/DXF reference geometry for auto PDF/DXF match."
        return None, info
    try:
        doc = fitz.open(pdf_path)
        page = doc[grading_page_index]
        pdfbb = _pdf_vector_bbox_for_page(page)
        if pdfbb is None:
            info["warning"] = "Could not read enough PDF vector linework for auto PDF/DXF match."
            return None, info
        px0, py0, px1, py1 = pdfbb
        cxp, cyp = (px0+px1)/2.0, (py0+py1)/2.0
        ex0, ny0, ex1, ny1 = cadbb
        cxe, cyn = (ex0+ex1)/2.0, (ny0+ny1)/2.0
        pw, ph = max(px1-px0, 1e-9), max(py1-py0, 1e-9)
        cw, ch = max(ex1-ex0, 1e-9), max(ny1-ny0, 1e-9)
        scale = min(cw/pw, ch/ph)
        # PDF y grows down. CAD northing grows up.  Flip y.
        ce = [scale, 0.0, cxe - scale*cxp]
        cn = [0.0, -scale, cyn + scale*cyp]
        info.update({
            "method": "v8.4.4 automatic PDF vector extents matched to fixed DXF extents",
            "warning": "Auto PDF/DXF match used extents because no coordinate callouts were found. PDF leader endpoints are projected to selected BC; review dense areas.",
            "pdf_scale_ft_per_point": scale,
            "pdf_bbox": [px0, py0, px1, py1],
            "cad_bbox": [ex0, ny0, ex1, ny1],
        })
        return (ce, cn), info
    except Exception as e:
        info["warning"] = f"Auto PDF/DXF extent match failed: {e}"
        return None, info


def parse_pdf_grade_block(raw: str) -> List[Tuple[str, float]]:
    """Read observations with their printed labels, without assuming a curb meaning."""
    from gradeguard_pdf import parse_callouts
    return [(label, value) for label, value, _flags in parse_callouts(raw)]


def extract_pdf_spot_labels(pdf_path: str, default_curb_height: float, cad_reference_points: Optional[List[Point2]] = None) -> Tuple[List[GradeLabel], Dict[str, Any]]:
    """Scan every sheet for review; PDF text boxes are not CAD control positions.

    Use Read PDF grades to verify meanings and save drawing-bound CAD ticks.
    No guessed sheet alignment or abbreviation can promote a PDF observation.
    """
    from gradeguard_pdf import scan_pdf
    rows, info = scan_pdf(pdf_path)
    info['pdf_spot_labels'] = len(rows)
    info['hp_lp_observations'] = sum(any(token in r.label for token in ('HP','LP','HIGH POINT','LOW POINT')) for r in rows)
    info['alignment'] = {'warning': 'PDF observations have no verified CAD position. Use reviewed CAD tick controls.'}
    info['observations'] = [dict(page=r.page,label=r.label,elevation=r.elevation,raw=r.raw,
                                 bbox=r.bbox,method=r.method,flags=r.flags) for r in rows]
    return [], info


def pdf_hp_lp_hard_hold_is_trusted(label: GradeLabel, alignment: Dict[str, Any]) -> bool:
    """Allow automatic HP/LP holds only with controlled position evidence."""
    alignment = alignment or {}
    warning = str(alignment.get("warning") or "").strip()
    rmse = alignment.get("rmse_ft")
    controlled_alignment = (
        not warning
        and isinstance(rmse, (int, float))
        and math.isfinite(float(rmse))
        and float(rmse) <= 1.0
    )
    has_vector_tip = "LEADER/ARROW/TICK ENDPOINT" in (label.source or "").upper()
    return controlled_alignment and has_vector_tip


def label_priority(kind: str) -> int:
    """Lower number wins. TC/top curb callouts must control BC staking."""
    k = (kind or "").upper()
    if k in ("TC", "BC", "HP_TC", "LP_TC", "HP", "LP"):
        return 0
    if k == "FL":
        return 1
    if k in ("FG", "SPOT", "ELEV"):
        return 2
    if k == "FFE":
        return 4
    return 3


def nearest_label_elevation(p: Point2, labels: List[GradeLabel], default_curb_height: float, label_radius: float) -> Tuple[Optional[float], str, str]:
    allowed = {'TC', 'BC', 'HP', 'LP', 'HP_TC', 'LP_TC', 'FL', 'FG', 'SPOT', 'ELEV'}
    candidates = []
    for lab in labels:
        if lab.kind not in allowed or not valid_grade_z(lab.elev):
            continue
        d = dist(p, (lab.x, lab.y))
        if d > label_radius:
            continue
        source = lab.source.upper()
        rank = 0 if 'MANUAL' in source else 1 if source.startswith('PDF') else 2
        candidates.append((rank, label_priority(lab.kind), d, lab))
    if not candidates:
        return None, '', ''
    candidates.sort(key=lambda item: item[:3])
    _rank, _kind, d, best = candidates[0]
    z = best.elev + default_curb_height if best.kind == 'FL' else best.elev
    # Keep the actual source, not a fabricated CAD label for a manual PDF hold.
    src = f"{best.source}; {best.kind} HELD CONTROL from {best.raw}; distance {d:.3f} ft"
    conf = 'HELD_CONTROL' if d <= 0.35 else 'LABEL_REVIEW'
    # Conflicting equally located controls must be resolved by the operator.
    same = [item[3] for item in candidates if item[:2] == candidates[0][:2] and abs(item[2]-d) <= 1e-6]
    if any(abs(lab.elev-best.elev) > 1e-6 for lab in same):
        return None, src + '; conflicting held controls', 'CONTROL_CONFLICT_REVIEW'
    return float(z), src, conf



def closest_point_t_on_segment(p: Point2, a: Point2, b: Point2) -> Tuple[float, Point2, float]:
    ax, ay = a
    bx, by = b
    px, py = p
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return 0.0, a, dist(p, a)
    t = max(0.0, min(1.0, (vx * wx + vy * wy) / c2))
    q = (ax + t * vx, ay + t * vy)
    return t, q, dist(p, q)


def segment_intersection(a: Point2, b: Point2, c: Point2, d: Point2) -> Optional[Tuple[float, float, Point2]]:
    """Return t on AB, u on CD, and intersection point if two 2D segments cross."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-9:
        return None
    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    if -1e-7 <= t <= 1.0000001 and -1e-7 <= u <= 1.0000001:
        pt = (ax + t * rx, ay + t * ry)
        return max(0.0, min(1.0, t)), max(0.0, min(1.0, u)), pt
    return None





def segment_intersection_params(a: Point2, b: Point2, c: Point2, d: Point2, tolerance: float = 1e-7):
    """Return (ok, t_on_ab, u_on_cd, point) for finite segment intersection.

    Older GradeGuard builds used segment_intersection(), while the sidewalk
    perpendicular contour interpolation calls this helper.  Keep both helpers
    so BC, sidewalk, and contour workflows use the same math.
    """
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return False, None, None, None
    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    if -tolerance <= t <= 1.0 + tolerance and -tolerance <= u <= 1.0 + tolerance:
        tt = max(0.0, min(1.0, t))
        uu = max(0.0, min(1.0, u))
        pt = (ax + tt * rx, ay + tt * ry)
        return True, tt, uu, pt
    return False, None, None, None


def line_intersection(a: Point2, b: Point2, c: Point2, d: Point2) -> Optional[Point2]:
    """Return the intersection point of two finite 2D segments.
    Wrapper used by sidewalk/curb contour crossing tools.
    """
    inter = segment_intersection(a, b, c, d)
    if inter is None:
        return None
    return inter[2]


def bc_touching_elevation(p: Point2, elev_segments: List[ElevSegment], search_radius: float, default_curb_height: float) -> Tuple[Optional[float], str, str, float]:
    """Calculate elevation from where surface/contour geometry actually touches the BC layer.

    Priority inside this secondary method:
    1. Find the selected BC segment that the generated point lies on.
    2. Find contour/surface segments that intersect that same BC segment.
    3. If the generated point is at that intersection, hold the crossing elevation directly.
    4. If the generated point is between two BC crossing anchors, interpolate along the BC segment.
    5. If nothing touches the BC segment, return None and let the older fallback logic handle it.

    This avoids the bad behavior where a nearby contour or label bleeds across the curb.
    """
    global CURB_SEGMENTS
    if not CURB_SEGMENTS or not elev_segments:
        return None, "no BC geometry/contour touch data available", "", 0.0

    # Find the actual BC segment for this generated point.
    nearest = []
    for cs in CURB_SEGMENTS:
        t, q, d = closest_point_t_on_segment(p, cs.a, cs.b)
        if d <= 2.0:  # generated points should be on BC; allow small tolerance for DXF quirks
            nearest.append((d, t, q, cs))
    if not nearest:
        return None, "point is not close enough to a selected BC segment for BC-touch contour logic", "", 0.0
    nearest.sort(key=lambda x: x[0])
    bc_d, p_t, p_q, cs = nearest[0]

    anchors = []
    # Only look at elevation geometry that truly intersects this same BC segment.
    for es in elev_segments:
        inter = segment_intersection(cs.a, cs.b, es.a, es.b)
        if inter is None:
            continue
        t, u, q = inter
        z = segment_z_at_point(q, es)
        src = f"{z:.3f} at BC/contour touch on {es.layer}"
        conf = "BC_TOUCH"
        # If the elevation source is explicitly FL/gutter/face, convert to BC.
        # Ordinary contours intersecting the BC line are already BC elevation and are NOT raised.
        if is_flowline_or_gutter_source(es.layer + " " + es.source):
            z += default_curb_height
            src += f" converted from FL/gutter by +{default_curb_height:.2f}"
            conf = "FL_TOUCH_PLUS_CURB"
        anchors.append((t, z, q, src, conf))

    if not anchors:
        return None, "no contour/surface segment actually touches the selected BC segment", "", 0.0

    anchors.sort(key=lambda x: x[0])
    # Direct hit at the generated point.
    direct = sorted(anchors, key=lambda a: abs(a[0] - p_t))
    if direct and abs(direct[0][0] - p_t) * max(dist(cs.a, cs.b), 1.0) <= max(0.35, search_radius * 0.01):
        t, z, q, src, conf = direct[0]
        return z, src + " direct hold at generated BC point", conf, dist(p, q)

    before = [a for a in anchors if a[0] <= p_t]
    after = [a for a in anchors if a[0] >= p_t]
    if before and after:
        lo = before[-1]
        hi = after[0]
        if abs(hi[0] - lo[0]) > 1e-9:
            f = (p_t - lo[0]) / (hi[0] - lo[0])
            z = lo[1] + (hi[1] - lo[1]) * f
            return z, f"interpolated along same BC segment between contour/surface touches: {lo[3]} and {hi[3]}", "BC_TOUCH_INTERP", min(dist(p, lo[2]), dist(p, hi[2]))
        return lo[1], lo[3], lo[4], dist(p, lo[2])

    # One-sided anchors are not enough to make a final curb grade, but can be a low-confidence fallback.
    nearest_anchor = min(anchors, key=lambda a: abs(a[0] - p_t))
    t, z, q, src, conf = nearest_anchor
    if dist(p, q) <= search_radius:
        return z, src + " one-sided BC touch fallback, review", "LOW_BC_TOUCH", dist(p, q)
    return None, "BC touches exist, but no bracketing contour/surface touches around this generated point", "", min(dist(p, a[2]) for a in anchors)



def flowline_touching_elevation(p: Point2, elev_segments: List[ElevSegment], search_radius: float, default_curb_height: float) -> Tuple[Optional[float], str, str, float]:
    """Use selected gutter/flowline geometry to interpret contour hits.

    Survey rule:
    - If a contour physically crosses the selected BACK OF CURB line, it is already a BC elevation.
    - If a contour physically crosses the selected FLOWLINE/GUTTER line, add curb height to stake BC.
    This function handles the second case. It is intentionally secondary to BC touch and C-5.0 TC holds.
    """
    global FLOWLINE_SEGMENTS
    if not FLOWLINE_SEGMENTS or not elev_segments:
        return None, "no selected flowline/gutter geometry available", "", 0.0
    # Find nearest selected flowline segment to this BC point.  A nearby FL crossing is only useful
    # when it is close enough to represent the matching curb section.
    nearest_fl=[]
    for fs in FLOWLINE_SEGMENTS:
        ft, fq, fd = closest_point_t_on_segment(p, fs.a, fs.b)
        if fd <= max(8.0, search_radius * 0.20):
            nearest_fl.append((fd, ft, fq, fs))
    if not nearest_fl:
        return None, "no selected flowline/gutter segment near this BC point", "", 0.0
    nearest_fl.sort(key=lambda x:x[0])
    fd, p_t, p_q, fs = nearest_fl[0]
    anchors=[]
    for es in elev_segments:
        inter = segment_intersection(fs.a, fs.b, es.a, es.b)
        if inter is None:
            continue
        t,u,q=inter
        z = segment_z_at_point(q, es) + default_curb_height
        src = f"{z:.3f} BC from contour/surface crossing selected FL/gutter on {es.layer} + curb height {default_curb_height:.2f}"
        anchors.append((t,z,q,src,"FL_TOUCH_PLUS_CURB"))
    if not anchors:
        return None, "no contour/surface segment touches selected flowline/gutter", "", fd
    anchors.sort(key=lambda x:x[0])
    direct=sorted(anchors, key=lambda a: abs(a[0]-p_t))
    if direct and abs(direct[0][0]-p_t) * max(dist(fs.a,fs.b),1.0) <= max(0.50, search_radius*0.015):
        t,z,q,src,conf=direct[0]
        return z, src + " direct hold from selected FL/gutter crossing", conf, max(fd, dist(p_q,q))
    before=[a for a in anchors if a[0] <= p_t]
    after=[a for a in anchors if a[0] >= p_t]
    if before and after:
        lo=before[-1]; hi=after[0]
        if abs(hi[0]-lo[0]) > 1e-9:
            f=(p_t-lo[0])/(hi[0]-lo[0])
            z=lo[1]+(hi[1]-lo[1])*f
            return z, f"interpolated along selected FL/gutter between contour touches, then + curb height: {lo[3]} and {hi[3]}", "FL_TOUCH_INTERP_PLUS_CURB", fd
        return lo[1], lo[3], lo[4], fd
    # One-sided flowline touch is review only.
    near=min(anchors, key=lambda a: abs(a[0]-p_t))
    if dist(p_q, near[2]) <= search_radius:
        return near[1], near[3] + " one-sided FL/gutter fallback, review", "LOW_FL_TOUCH_PLUS_CURB", max(fd, dist(p_q,near[2]))
    return None, "selected FL/gutter touches exist but do not bracket this point", "", fd



def nearest_curb_segment_with_projection(p: Point2) -> Tuple[Optional[CurbSegment], Optional[Point2], float, float]:
    """Return nearest current selected geometry segment, projected point, distance, and t station.
    In the sidewalk generator CURB_SEGMENTS represents the selected sidewalk edge.
    """
    best = (None, None, 1e99, 0.0)
    for seg in CURB_SEGMENTS:
        ax, ay = seg.a; bx, by = seg.b
        vx, vy = bx-ax, by-ay
        den = vx*vx + vy*vy
        if den <= 1e-12:
            continue
        t = max(0.0, min(1.0, ((p[0]-ax)*vx + (p[1]-ay)*vy)/den))
        q = (ax + t*vx, ay + t*vy)
        d = dist(p, q)
        if d < best[2]:
            best = (seg, q, d, t)
    return best


def segment_tangent_normal(seg: CurbSegment) -> Tuple[Point2, Point2]:
    vx = seg.b[0] - seg.a[0]
    vy = seg.b[1] - seg.a[1]
    L = math.hypot(vx, vy) or 1.0
    tx, ty = vx/L, vy/L
    return (tx, ty), (-ty, tx)


def add_edge_contour_crossing_points(base_points: List[Point2], elev_segments: List[ElevSegment], tolerance: float = 0.25) -> Tuple[List[Point2], int]:
    """Add a point anywhere a real contour/surface segment crosses selected edge geometry.
    This is used for sidewalk and 2D workflows so contour crossings become staking/elevation controls.
    """
    pts = list(base_points)
    added = 0
    if not CURB_SEGMENTS:
        return dedupe_points(pts), added
    contour_segments = [es for es in elev_segments if is_contour_crossing_source(es)]
    for cs in CURB_SEGMENTS:
        for es in contour_segments:
            q = line_intersection(cs.a, cs.b, es.a, es.b)
            if q is None:
                continue
            if point_segment_distance(q, cs.a, cs.b) <= tolerance and point_segment_distance(q, es.a, es.b) <= tolerance:
                if not any(dist(q, p0) <= tolerance for p0 in pts):
                    pts.append(q); added += 1
    return dedupe_points(pts), added


def add_grade_callout_points_to_selected_edge(base_points: List[Point2], labels: List[GradeLabel], tolerance: float = 0.35, max_label_to_edge_dist: float = 140.0) -> Tuple[List[Point2], List[GradeLabel], int]:
    """Add sidewalk/edge points from CAD leaders and grade callouts.
    Unlike BC HP/LP, sidewalk staking needs FG/TC/LP/HP callout arrow endpoints too.
    The leader/tick endpoint is projected to the selected edge, then held as a grade label.
    """
    pts = list(base_points)
    anchored: List[GradeLabel] = []
    added = 0
    if not CURB_SEGMENTS or not labels:
        return dedupe_points(pts), anchored, added
    allowed = {"FG", "SPOT", "ELEV", "TC", "BC", "HP", "LP", "HP_TC", "LP_TC"}
    used: List[Point2] = []
    for lab in labels:
        if (lab.kind or '').upper() not in allowed:
            continue
        q = _callout_tick_point_from_leaders(lab, max_label_dist=120.0, max_curb_dist=6.0)
        if q is None:
            q, d = _nearest_point_on_curb((lab.x, lab.y), max_dist=max_label_to_edge_dist)
            if q is None:
                continue
        if any(dist(q, u) <= max(2.0, tolerance*4.0) for u in used):
            continue
        used.append(q)
        if not any(dist(q, existing) <= tolerance for existing in pts):
            pts.append(q); added += 1
        anchored.append(GradeLabel(q[0], q[1], lab.kind, lab.elev, lab.raw, f"{lab.source} projected to selected sidewalk/edge from leader/tick"))
    return dedupe_points(pts), anchored, added


def sidewalk_cross_slope_auto_adjust(p: Point2, elev: Optional[float], src: str, segments: List[ElevSegment], width: float, slope_percent: float, mode: str, radius: float) -> Tuple[Optional[float], str]:
    """Apply sidewalk cross slope only when requested.
    Direct means no change. Add/subtract are explicit. Auto samples left/right of the selected edge;
    if the chosen elevation came from the opposite side, it applies width*slope toward the selected edge.
    """
    if elev is None:
        return elev, src
    m = (mode or 'direct').lower()
    try:
        delta = float(width or 0.0) * float(slope_percent or 0.0) / 100.0
    except Exception:
        delta = 0.0
    if delta <= 0 or m.startswith('direct'):
        return elev, src
    if m.startswith('auto') and ('MANUAL' in src.upper() or 'HELD CONTROL' in src.upper() or 'LANDXML' in src.upper()):
        return elev, src + '; held design grade retained'
    if m.startswith('add'):
        return elev + delta, src + f"; sidewalk cross slope +{delta:.3f} ft"
    if m.startswith('subtract'):
        return elev - delta, src + f"; sidewalk cross slope -{delta:.3f} ft"
    seg, q, d, t = nearest_curb_segment_with_projection(p)
    if seg is None:
        return elev, src + "; auto cross slope unavailable, no selected edge segment"
    (_, _), n = segment_tangent_normal(seg)
    left_pt = (p[0] + n[0]*float(width), p[1] + n[1]*float(width))
    right_pt = (p[0] - n[0]*float(width), p[1] - n[1]*float(width))
    lz, lsrc, lconf, ld = contour_elevation(left_pt, segments, max(radius, width+5.0))
    rz, rsrc, rconf, rd = contour_elevation(right_pt, segments, max(radius, width+5.0))
    # If the source was a selected-edge touch/interpolation, keep it exactly.
    if any(k in (src or '').upper() for k in ['BC/CONTOUR TOUCH', 'TOUCH ON', 'SELECTED BC', 'CONTOUR TOUCH', 'SAME BC SEGMENT']):
        return elev, src + "; selected edge elevation held, no cross-slope adjustment"
    if lz is not None and rz is not None:
        if lz > rz:
            return elev + delta, src + f"; auto cross slope detected right side lower and adjusted toward selected edge +{delta:.3f} ft"
        if rz > lz:
            return elev - delta, src + f"; auto cross slope detected left side lower and adjusted toward selected edge -{delta:.3f} ft"
    return elev, src + "; auto cross slope could not determine left/right from contours, direct elevation kept"



def enforce_sidewalk_cross_slope_pairs(points: List[Point2], elevs: List[Tuple[Optional[float], str, str, str]], width: float, max_slope_percent: float, mode: str) -> List[Tuple[Optional[float], str, str, str]]:
    """Check selected edge-of-sidewalk points against opposite-edge points.

    Cross slope is perpendicular to the sidewalk run.  For a 10 ft sidewalk at 1%,
    the maximum edge-to-edge elevation difference is 0.10 ft.  This function pairs
    points that are about one sidewalk width apart and nearly perpendicular to the
    nearest selected sidewalk segment.  In auto mode it clamps the high side down
    to the maximum allowed difference so the exported points do not exceed the
    entered cross-slope limit.  In other modes it only adds a review note.
    """
    try:
        width = float(width or 0.0)
        max_slope_percent = float(max_slope_percent or 0.0)
    except Exception:
        return elevs
    if width <= 0 or max_slope_percent <= 0 or not points or len(points) != len(elevs):
        return elevs
    out = [list(e) for e in elevs]
    auto_enforce = 'auto' in (mode or '').lower()
    paired = set()
    tol = max(1.0, width * 0.25)
    min_d = max(1.0, width - tol)
    max_d = width + tol
    max_slope = max_slope_percent / 100.0
    for i, p in enumerate(points):
        zi = out[i][0]
        if zi is None:
            continue
        seg, q, dseg, tpos = nearest_curb_segment_with_projection(p)
        if seg is None:
            continue
        (tvec, nvec) = segment_tangent_normal(seg)
        best = None
        for j, qpt in enumerate(points):
            if i == j or (min(i,j), max(i,j)) in paired:
                continue
            zj = out[j][0]
            if zj is None:
                continue
            vx = qpt[0] - p[0]; vy = qpt[1] - p[1]
            dist = math.hypot(vx, vy)
            if dist < min_d or dist > max_d:
                continue
            ux = vx / dist; uy = vy / dist
            # Perpendicular to sidewalk run means vector between pair is mostly normal to tangent.
            along = abs(ux * tvec[0] + uy * tvec[1])
            normal = abs(ux * nvec[0] + uy * nvec[1])
            if normal < 0.70 or along > 0.45:
                continue
            score = abs(dist - width) + along * width
            if best is None or score < best[0]:
                best = (score, j, dist)
        if best is None:
            continue
        _, j, dist = best
        paired.add((min(i,j), max(i,j)))
        zj = out[j][0]
        diff = abs(float(zi) - float(zj))
        allowed = dist * max_slope
        slope_pct = (diff / dist) * 100.0 if dist else 0.0
        if diff <= allowed + 0.005:
            note = f"; sidewalk cross-slope QA pass {slope_pct:.2f}% over {dist:.2f} ft"
            out[i][1] = (out[i][1] or '') + note
            out[j][1] = (out[j][1] or '') + note
            continue
        msg = f"Review: edge-to-edge sidewalk cross slope {slope_pct:.2f}% over {dist:.2f} ft exceeds max {max_slope_percent:.2f}%."
        if auto_enforce and not (held_result(out[i][3],out[i][1]) or held_result(out[j][3],out[j][1]) or 'LANDXML' in (out[i][1]+out[j][1]).upper()):
            # Preserve the lower edge for drainage and clamp the higher edge to lower + allowed.
            if float(zi) > float(zj):
                newz = float(zj) + allowed
                out[i][0] = round(newz, 3)
                out[i][1] = (out[i][1] or '') + f"; auto clamped to max {max_slope_percent:.2f}% cross slope from point pair"
                out[i][2] = ((out[i][2] + ' ') if out[i][2] else '') + msg + " High side adjusted."
                out[i][3] = out[i][3] or 'CROSS_SLOPE_ADJUSTED'
            else:
                newz = float(zi) + allowed
                out[j][0] = round(newz, 3)
                out[j][1] = (out[j][1] or '') + f"; auto clamped to max {max_slope_percent:.2f}% cross slope from point pair"
                out[j][2] = ((out[j][2] + ' ') if out[j][2] else '') + msg + " High side adjusted."
                out[j][3] = out[j][3] or 'CROSS_SLOPE_ADJUSTED'
        else:
            out[i][2] = ((out[i][2] + ' ') if out[i][2] else '') + msg
            out[j][2] = ((out[j][2] + ' ') if out[j][2] else '') + msg
            out[i][3] = out[i][3] or 'CROSS_SLOPE_REVIEW'
            out[j][3] = out[j][3] or 'CROSS_SLOPE_REVIEW'
    return [tuple(x) for x in out]

def is_contour_layer_name(layer: str) -> bool:
    n = normalize_layer_name(layer)
    return any(t in n for t in ["CONTOUR", "TOPOPMINORCONTOURS", "TOPOPMAJORCONTOURS", "TOPOGRADINGCONTOURS", "MAJORCONTOURS", "MINORCONTOURS"])


def build_contour_tin_surface(segments: List[ElevSegment], max_segment_sample: float = 25.0) -> Optional[ContourTinSurface]:
    """Build a lightweight TIN from 3D contour/grade segments.

    This is for 2D sidewalk and curb jobs where there is no LandXML surface but the
    DXF has 3D contour polylines.  The TIN lets GradeGuard prorate between contours
    instead of using the nearest contour elevation.
    """
    try:
        import numpy as _np
        from scipy.spatial import Delaunay
    except Exception:
        return None
    samples = []
    for es in segments:
        if not valid_grade_z(es.z):
            continue
        # Use contour layers first. Also accept 3D surface lines when present.
        if not (is_contour_layer_name(es.layer) or is_surface_layer(es.layer)):
            continue
        ax, ay = es.a; bx, by = es.b
        length = math.hypot(bx-ax, by-ay)
        steps = max(1, int(math.ceil(length / max(5.0, float(max_segment_sample)))))
        for i in range(steps + 1):
            t = i / steps
            x = ax + (bx-ax)*t
            y = ay + (by-ay)*t
            z = float(es.z) + ((float(es.z2) if es.z2 is not None else float(es.z)) - float(es.z))*t
            if valid_grade_z(z):
                samples.append((x,y,z))
    if len(samples) < 3:
        return None
    # Deduplicate XY.  If Civil 3D exports duplicate vertices, average their Z values.
    buckets = {}
    for x,y,z in samples:
        key = (round(x,3), round(y,3))
        buckets.setdefault(key, []).append(z)
    pts=[]; zs=[]
    for (x,y), zlist in buckets.items():
        if max(zlist) - min(zlist) > 1e-6:
            return None
        pts.append((x,y)); zs.append(zlist[0])
    if len(pts) < 3:
        return None
    arr = _np.array(pts, dtype=float)
    zarr = _np.array(zs, dtype=float)
    try:
        tri = Delaunay(arr)
    except Exception:
        return None
    return ContourTinSurface(points=arr, z=zarr, tri=tri)


def contour_tin_elevation(p: Point2, tin: Optional[ContourTinSurface]) -> Tuple[Optional[float], str, str]:
    """Interpolate finished grade from a TIN built from 3D contours."""
    if tin is None:
        return None, "no contour TIN available", ""
    try:
        import numpy as _np
        pt = _np.array([p[0], p[1]], dtype=float)
        simplex = tin.tri.find_simplex(pt)
        if simplex < 0:
            return None, "point falls outside the contour TIN", ""
        transform = tin.tri.transform[simplex]
        delta = pt - transform[2]
        bary = _np.dot(transform[:2, :], delta)
        bary = _np.append(bary, 1 - bary.sum())
        verts = tin.tri.simplices[simplex]
        z = float(_np.dot(bary, tin.z[verts]))
        return z, f"prorated from 3D contour TIN, triangle vertices {tin.z[verts][0]:.2f}, {tin.z[verts][1]:.2f}, {tin.z[verts][2]:.2f}", "TIN_CONTOUR"
    except Exception as ex:
        return None, f"contour TIN interpolation failed: {ex}", ""




def normal_contour_interpolation(p: Point2, segments: List[ElevSegment], search_len: float = 80.0, min_delta_z: float = 0.01) -> Tuple[Optional[float], str, str]:
    """Prorate sidewalk elevation between 3D contours along the cross-slope direction.

    For sidewalk work, the cross slope is perpendicular to the direction of travel.
    This routine finds the nearest selected sidewalk segment, builds a perpendicular
    sample line through the staking point, finds where 3D contour segments cross that
    sample line, then linearly interpolates the elevation at the selected edge point.

    This is different from averaging and different from nearest-contour guessing.
    It is a true proportional interpolation between contour hits.
    """
    if not segments:
        return None, "no contour segments available for perpendicular interpolation", ""
    seg, q, dseg, tpos = nearest_curb_segment_with_projection(p)
    if seg is None:
        return None, "no selected sidewalk edge available for perpendicular contour interpolation", ""
    try:
        (_, _), nvec = segment_tangent_normal(seg)
    except Exception:
        return None, "could not compute sidewalk perpendicular direction", ""
    # Use a centered perpendicular line through the point.
    a = (p[0] - nvec[0]*search_len, p[1] - nvec[1]*search_len)
    b = (p[0] + nvec[0]*search_len, p[1] + nvec[1]*search_len)
    hits = []
    for es in segments:
        if not valid_grade_z(es.z):
            continue
        if not is_contour_layer_name(es.layer):
            continue
        ok, t, u, ip = segment_intersection_params(a, b, es.a, es.b, tolerance=1e-7)
        if not ok:
            continue
        # signed station along normal, where p is 0.  t is on a->b.
        station = (t - 0.5) * 2.0 * search_len
        z = float(es.z) + ((float(es.z2) if es.z2 is not None else float(es.z)) - float(es.z)) * u
        if valid_grade_z(z):
            hits.append((station, z, es.layer, ip))
    if len(hits) < 2:
        return None, "not enough contour crossings on perpendicular sidewalk sample line", ""
    # Deduplicate nearby same contour crossings.
    hits.sort(key=lambda h: h[0])
    cleaned=[]
    for h in hits:
        if cleaned and abs(h[0]-cleaned[-1][0]) < 0.05 and abs(h[1]-cleaned[-1][1]) < 0.01:
            continue
        cleaned.append(h)
    hits=cleaned
    # Best case: contour hits bracket the selected edge point station 0.
    lower=[h for h in hits if h[0] <= 0]
    upper=[h for h in hits if h[0] >= 0]
    if lower and upper:
        lo=max(lower, key=lambda h:h[0])
        hi=min(upper, key=lambda h:h[0])
        if abs(hi[0]-lo[0]) < 1e-6:
            z=(lo[1]+hi[1])/2.0
            return z, f"perpendicular sidewalk contour hold at selected edge from {lo[2]} / {hi[2]}", "PERP_CONTOUR_HIGH"
        if abs(hi[1]-lo[1]) >= min_delta_z:
            ratio=(0-lo[0])/(hi[0]-lo[0])
            z=lo[1] + (hi[1]-lo[1])*ratio
            return z, f"prorated perpendicular between contours {lo[1]:.2f} and {hi[1]:.2f} along sidewalk cross-slope direction", "PERP_CONTOUR"
    # Fallback: use two closest different-elevation contour hits, but flag review.
    hits2=sorted(hits, key=lambda h: abs(h[0]))
    for i in range(len(hits2)):
        for j in range(i+1, len(hits2)):
            h1,h2=hits2[i],hits2[j]
            if abs(h1[1]-h2[1]) < min_delta_z or abs(h1[0]-h2[0]) < 1e-6:
                continue
            lo,hi=sorted([h1,h2], key=lambda h:h[0])
            ratio=(0-lo[0])/(hi[0]-lo[0])
            z=lo[1] + (hi[1]-lo[1])*ratio
            return z, f"review prorated from closest perpendicular contour hits {lo[1]:.2f} and {hi[1]:.2f}; selected edge was not cleanly bracketed", "PERP_CONTOUR_REVIEW"
    return None, "perpendicular contour hits found, but they did not provide usable different elevations", ""

def assign_sidewalk_elevations(points: List[Point2], labels: List[GradeLabel], segments: List[ElevSegment], contour_radius: float, landxml_surface: Optional[LandXMLSurface] = None, contour_tin: Optional[ContourTinSurface] = None) -> List[Tuple[Optional[float], str, str, str]]:
    """Sidewalk-specific elevation order.

    1. CAD/PDF leader or spot callout tick projected to the selected sidewalk edge.
    2. LandXML/Civil 3D surface if available.
    3. TIN built from 3D contour polylines.  This prorates between contours.
    4. Contour crossing/interpolation along the selected edge.
    5. Low-confidence nearby contour fallback.
    """
    out=[]
    for p in points:
        elev, src, conf = nearest_label_elevation(p, labels, 0.0, 8.0)
        if elev is not None:
            out.append((elev, src + "; sidewalk callout held first", "", conf or "HIGH")); continue
        selev, ssrc, sconf = landxml_elevation_at(p, landxml_surface, search_pad=max(5.0, min(contour_radius, 25.0)))
        if selev is not None:
            out.append((selev, ssrc + "; sidewalk finished grade from Civil 3D/LandXML", "", sconf or "SURFACE")); continue
        pelev, psrc, pconf = normal_contour_interpolation(p, segments, search_len=max(60.0, contour_radius))
        if pelev is not None:
            out.append((pelev, psrc + "; sidewalk FG prorated between contours perpendicular to walk", "", pconf)); continue
        belev, bsrc, bconf, bd = bc_touching_elevation(p, segments, contour_radius, 0.0)
        if belev is not None:
            out.append((belev, bsrc + "; contour touch/interpolation on selected sidewalk edge", "", bconf or "EDGE_TOUCH")); continue
        telev, tsrc, tconf = contour_tin_elevation(p, contour_tin)
        if telev is not None:
            out.append((telev, tsrc + "; temporary TIN from 3D contours used as fallback only; review", "Review: TIN fallback used because no local perpendicular contour bracket touched this sidewalk edge.", tconf or "TIN_REVIEW")); continue
        celev, csrc, cconf, cd = contour_elevation(p, segments, contour_radius)
        if celev is not None:
            out.append((celev, csrc + "; nearby contour fallback, review", "Review: nearby contour fallback used because no bracketing contour/TIN/spot was found", cconf or "LOW")); continue
        out.append((None, "no sidewalk elevation source found", "Missing sidewalk elevation. Need LandXML, 3D contours, spot/leader callout, or manual hold.", ""))
    return out

def contour_elevation(p: Point2, segments: List[ElevSegment], radius: float, nearest_count: int = 8) -> Tuple[Optional[float], str, str, float]:
    if not segments:
        return None, "no DXF surface/contour elevation segments found", "", 0.0
    distances = []
    for seg in segments:
        d = point_segment_distance(p, seg.a, seg.b)
        if d <= radius:
            distances.append((d, seg))
    if not distances:
        return None, f"no DXF surface/contour segment within {radius:.1f} ft", "", 0.0
    distances.sort(key=lambda t: t[0])

    # If a Topo-P-Surface/TIN style layer exists nearby, trust it before contours.
    surface_near = [(d, seg) for d, seg in distances if is_surface_layer(seg.layer)]
    if surface_near:
        near = surface_near[:nearest_count]
        if near[0][0] <= 0.25:
            d, seg = near[0]
            z = segment_z_at_point(p, seg)
            return z, f"on/near DXF surface {z:.3f} from {seg.layer}", "HIGH", d
        num = 0.0
        den = 0.0
        used = []
        for d, seg in near:
            z = segment_z_at_point(p, seg)
            w = 1.0 / max(d, 0.25) ** 2
            num += z * w
            den += w
            used.append(f"{z:.2f}@{d:.1f}ft {seg.layer}")
        if den:
            z = num / den
            return z, "interpolated from Topo-P-Surface/TIN: " + "; ".join(used[:5]), "SURFACE", near[0][0]

    near = distances[:nearest_count]
    if near[0][0] <= 0.20:
        d, seg = near[0]
        z = segment_z_at_point(p, seg)
        return z, f"on or near DXF contour/elevation {z:.2f} from {seg.layer}", "HIGH", d
    # If all close contours are the same elevation, use that elevation directly.
    zvals = [round(segment_z_at_point(p, seg), 3) for _, seg in near]
    unique_z = sorted(set(zvals))
    if len(unique_z) == 1:
        d, seg = near[0]
        z = segment_z_at_point(p, seg)
        return z, f"nearest DXF contour/elevation {z:.2f} from {seg.layer}", "MEDIUM", d
    # IDW from nearest contour or grade feature segments.
    num = 0.0
    den = 0.0
    used_layers = []
    for d, seg in near:
        z = segment_z_at_point(p, seg)
        w = 1.0 / max(d, 0.25) ** 2
        num += z * w
        den += w
        used_layers.append(f"{z:.2f}@{d:.1f}ft")
    elev = num / den if den else None
    if elev is None:
        return None, "could not interpolate from DXF contours/surface", "", near[0][0]
    return elev, "interpolated from nearest DXF contours/elevation features: " + ", ".join(used_layers[:5]), "MEDIUM", near[0][0]



def local_contour_prorated_elevation(p: Point2, segments: List[ElevSegment], radius: float, near_hold_tol: float = 1.50) -> Tuple[Optional[float], str, str, float]:
    """Only interpolate when contour projections geometrically bracket the point.

    No project-elevation or contour-interval assumptions. Near-but-not-on values
    remain review evidence; exact contacts use the existing 0.35-foot direct gate.
    """
    hits = []
    for seg in segments:
        if not is_contour_layer_name(seg.layer):
            continue
        _t, q, d = closest_point_t_on_segment(p, seg.a, seg.b)
        z = segment_z_at_point(q, seg)
        if valid_grade_z(z) and d <= radius:
            hits.append((d, z, q, seg))
    if not hits:
        return None, 'No valid local contours.', '', 0.0
    hits.sort(key=lambda h: h[0])
    d, z, _q, seg = hits[0]
    if d <= min(0.35, near_hold_tol):
        return z, f'Exact local contour at {seg.layer}', 'LOCAL_CONTOUR_HOLD', d
    pairs = []
    for i, a in enumerate(hits):
        for b in hits[i+1:]:
            if abs(a[1]-b[1]) <= 1e-9:
                continue
            va = (a[2][0]-p[0], a[2][1]-p[1])
            vb = (b[2][0]-p[0], b[2][1]-p[1])
            # Opposite projections and point on their joining segment, not merely
            # two different elevation numbers on the same side of the point.
            if va[0]*vb[0]+va[1]*vb[1] >= 0 or point_segment_distance(p, a[2], b[2]) > 1e-6:
                continue
            pairs.append((a[0]+b[0], a, b))
    if not pairs:
        return None, 'Local contours do not spatially bracket point.', 'LOCAL_CONTOUR_REVIEW', d
    _span, a, b = min(pairs, key=lambda v: v[0])
    z = a[1] + a[0]/(a[0]+b[0])*(b[1]-a[1])
    return z, f'Local contour bracket {a[1]:.3f} / {b[1]:.3f}', 'LOCAL_CONTOUR_PRORATE', min(a[0], b[0])

def is_flowline_or_gutter_source(src: str) -> bool:
    """Detect elevation sources that are explicitly flowline/gutter/face-of-curb.
    A contour line at BC is already top/back of curb. A contour/grade at FL/gutter needs curb height added.
    Do not guess from distance alone.
    """
    n = normalize_layer_name(src or "")
    terms = [
        "FLOWLINE", "FLOW", "FL", "GUTTER", "LIPGUTTER", "LOG",
        "FACEOFCURB", "FOC", "FCURB", "FC", "GUTTERLINE"
    ]
    return any(t in n for t in terms)


def is_back_curb_source(src: str) -> bool:
    """Detect sources that explicitly claim to be top/back of curb."""
    n = normalize_layer_name(src or "")
    terms = [
        "BACKOFCURB", "BOC", "SITEPBC", "SITEBC", "PBC",
        "TOPBACKOFCURB", "TBC", "TOPOFCURB", "TC"
    ]
    return any(t in n for t in terms)


def contour_bc_rule(p, celev, csrc, cconf, nearest_d, default_curb_height, surface_mode='bc'):
    if celev is None:
        return None, csrc, cconf, ''
    mode = (surface_mode or '').lower()
    if mode.startswith(('bc', 'top', 'back')):
        return celev, csrc + '; explicit top/back grade interpretation', cconf, ''
    if mode.startswith(('flow', 'gutter', 'pavement')):
        return celev + default_curb_height, csrc + f'; explicit FL/pavement + curb {default_curb_height:.3f}', cconf, ''
    if is_back_curb_source(csrc):
        return celev, csrc, cconf, ''
    if is_flowline_or_gutter_source(csrc) and CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF:
        return celev + default_curb_height, csrc + f'; FL + confirmed curb {default_curb_height:.3f}', cconf, ''
    return celev, csrc, 'SURFACE_MEANING_REVIEW', 'Review required: select top/back grade or flowline/pavement meaning for this surface.'



def _bc_chain_station_for_point(p: Point2) -> Tuple[Optional[float], Optional[Point2], float, Optional[int]]:
    """Project a point onto the selected BC chain and return cumulative station.

    This intentionally uses the saved selected BC geometry, not a TIN and not a
    nearby contour.  It lets GradeGuard interpolate/prorate along the curb run
    between actual contour crossings on BC and projected contour crossings from
    FL/gutter.
    """
    global CURB_SEGMENTS
    if not CURB_SEGMENTS:
        return None, None, 1e99, None
    cum = 0.0
    best = (None, None, 1e99, None)  # station, q, d, index
    for i, seg in enumerate(CURB_SEGMENTS):
        L = max(dist(seg.a, seg.b), 1e-12)
        t, q, d = closest_point_t_on_segment(p, seg.a, seg.b)
        st = cum + max(0.0, min(1.0, t)) * L
        if d < best[2]:
            best = (st, q, d, i)
        cum += L
    return best




def _chain_station_for_point_on_segments(p: Point2, curb_segments: List[CurbSegment]) -> Tuple[Optional[float], Optional[Point2], float, Optional[int]]:
    """Project a point onto a specific curb component and return station.

    v8.4.9: This is the important fix for jobs with several curb pieces or
    parallel curb/gutter lines selected at the same time.  The old global station
    could accidentally compare a point on one curb run to a contour anchor on a
    different curb run.  This function stations only on the local curb component.
    """
    if not curb_segments:
        return None, None, 1e99, None
    cum = 0.0
    best = (None, None, 1e99, None)
    for i, seg in enumerate(curb_segments):
        L = max(dist(seg.a, seg.b), 1e-12)
        t, q, d = closest_point_t_on_segment(p, seg.a, seg.b)
        st = cum + max(0.0, min(1.0, t)) * L
        if d < best[2]:
            best = (st, q, d, i)
        cum += L
    return best


def _nearest_curb_segment_index(p: Point2, curb_segments: List[CurbSegment]) -> Optional[int]:
    best_i = None
    best_d = 1e99
    for i, seg in enumerate(curb_segments):
        _t, _q, d = closest_point_t_on_segment(p, seg.a, seg.b)
        if d < best_d:
            best_i = i
            best_d = d
    return best_i


def _segments_touch(a: CurbSegment, b: CurbSegment, tol: float) -> bool:
    return (
        dist(a.a, b.a) <= tol or dist(a.a, b.b) <= tol or
        dist(a.b, b.a) <= tol or dist(a.b, b.b) <= tol
    )


def _curb_endpoint_index(segments, tolerance):
    cell = max(abs(float(tolerance)), 0.01)
    index = {}
    for i, seg in enumerate(segments):
        for x, y in (seg.a, seg.b):
            index.setdefault((math.floor(x / cell), math.floor(y / cell)), set()).add(i)
    return index, cell


def _curb_endpoint_candidates(index, cell, endpoints, tolerance):
    candidates = set()
    for x, y in endpoints:
        for ix in range(math.floor((x - tolerance) / cell), math.floor((x + tolerance) / cell) + 1):
            for iy in range(math.floor((y - tolerance) / cell), math.floor((y + tolerance) / cell) + 1):
                candidates.update(index.get((ix, iy), ()))
    # Original order preserves equal-distance tie decisions in station ordering.
    return sorted(candidates)


def _order_connected_curb_run(run: List[CurbSegment], start_i: int = 0, tol: float = 1.50) -> List[CurbSegment]:
    """Order a connected set of curb segments into a station chain.

    This is intentionally simple and conservative.  It follows endpoint-to-endpoint
    connectivity so a curb run grades like the drawn curb, instead of making every
    point use only the single nearest short segment.
    """
    if len(run) <= 1:
        return run[:]

    endpoint_index, endpoint_cell = _curb_endpoint_index(run, tol + 1e-9)
    remaining = dict(enumerate(run))
    ordered = [remaining.pop(start_i if 0 <= start_i < len(remaining) else 0)]

    changed = True
    while remaining and changed:
        changed = False
        head = ordered[0]
        tail = ordered[-1]

        best = None
        best_where = None
        best_rev = False
        best_d = tol + 1e-9
        candidates = _curb_endpoint_candidates(endpoint_index, endpoint_cell, (head.a, tail.b), tol + 1e-9)
        for i in candidates:
            if i not in remaining:
                continue
            seg = remaining[i]
            tests = [
                (dist(seg.b, head.a), 'front', False),
                (dist(seg.a, head.a), 'front', True),
                (dist(seg.a, tail.b), 'back', False),
                (dist(seg.b, tail.b), 'back', True),
            ]
            for d, where, rev in tests:
                if d <= best_d:
                    best = i; best_where = where; best_rev = rev; best_d = d

        if best is not None:
            seg = remaining.pop(best)
            if best_rev:
                seg = CurbSegment(seg.b, seg.a, seg.layer)
            if best_where == 'front':
                ordered.insert(0, seg)
            else:
                ordered.append(seg)
            changed = True

    # If branches or gaps remain, append by nearest endpoint so stationing still has a stable order.
    while remaining:
        tail = ordered[-1]
        best = min(remaining, key=lambda i: min(dist(tail.b, remaining[i].a), dist(tail.b, remaining[i].b)))
        seg = remaining.pop(best)
        if dist(tail.b, seg.b) < dist(tail.b, seg.a):
            seg = CurbSegment(seg.b, seg.a, seg.layer)
        ordered.append(seg)
    return ordered


def _local_curb_segments_for_point(p: Point2, max_endpoint_gap: float = 1.50) -> List[CurbSegment]:
    """Return the connected selected-BC curb run that controls the point.

    v8.5.5 survey correction:
    Elevations must be prorated along the curb run, not from one isolated segment
    and not from all unrelated curb pieces.  This returns the connected component
    containing the nearest selected BACK OF CURB segment, then orders that component
    into a station chain.
    """
    global CURB_SEGMENTS
    if not CURB_SEGMENTS:
        return []
    start_i = _nearest_curb_segment_index(p, CURB_SEGMENTS)
    if start_i is None:
        return []

    endpoint_index, endpoint_cell = _curb_endpoint_index(CURB_SEGMENTS, max_endpoint_gap)
    visited = {start_i}
    stack = [start_i]
    while stack:
        i = stack.pop()
        candidates = _curb_endpoint_candidates(endpoint_index, endpoint_cell, (CURB_SEGMENTS[i].a, CURB_SEGMENTS[i].b), max_endpoint_gap)
        for j in candidates:
            seg = CURB_SEGMENTS[j]
            if j in visited:
                continue
            if _segments_touch(CURB_SEGMENTS[i], seg, max_endpoint_gap):
                visited.add(j)
                stack.append(j)

    run_indices = sorted(visited)
    run = [CURB_SEGMENTS[i] for i in run_indices]
    local_start = run_indices.index(start_i)
    return _order_connected_curb_run(run, local_start, tol=max_endpoint_gap)


def _chain_endpoint_extension_anchor(p: Point2, curb_segments: List[CurbSegment], contour_segments: List[ElevSegment], search_radius: float) -> Optional[Tuple[float, float, Point2, str, str]]:
    """Find a contour that controls just beyond the start/end of a curb run.

    v8.4.9: If the curb stops short but the plan/DXF contour continues just
    beyond that end, the end BC point must be able to see that contour by station
    extension.  This solves cases like the south curb end where the 610 contour
    is just beyond the drawn curb end.

    Returns:
        station, elevation, point_on_bc, source, confidence
    """
    if not curb_segments or not contour_segments:
        return None

    # Total local chain length.
    lengths = [max(dist(s.a, s.b), 1e-12) for s in curb_segments]
    total_len = sum(lengths)

    endpoints = []
    # Start tangent points from first segment.
    first = curb_segments[0]
    L0 = max(dist(first.a, first.b), 1e-12)
    u_start = ((first.a[0] - first.b[0]) / L0, (first.a[1] - first.b[1]) / L0)
    endpoints.append((0.0, first.a, u_start, "start"))

    # End tangent points from last segment.
    last = curb_segments[-1]
    L1 = max(dist(last.a, last.b), 1e-12)
    u_end = ((last.b[0] - last.a[0]) / L1, (last.b[1] - last.a[1]) / L1)
    endpoints.append((total_len, last.b, u_end, "end"))

    # Only apply this special rule when the point being solved is near an endpoint.
    p_station, _pq, _pd, _idx = _chain_station_for_point_on_segments(p, curb_segments)
    if p_station is None:
        return None
    endpoint_station, endpoint_pt, tangent, endpoint_name = min(endpoints, key=lambda e: abs(e[0] - p_station))
    if abs(endpoint_station - p_station) > max(2.0, min(8.0, search_radius * 0.20)):
        return None

    max_forward = max(7.0, min(14.0, search_radius * 0.35))
    max_perp = max(3.5, min(6.0, search_radius * 0.18))

    candidates = []
    tx, ty = tangent
    # normal vector
    nx, ny = -ty, tx

    for es in contour_segments:
        for ep in (es.a, es.b):
            dx = ep[0] - endpoint_pt[0]
            dy = ep[1] - endpoint_pt[1]
            along = dx * tx + dy * ty
            perp = abs(dx * nx + dy * ny)
            euclid = math.hypot(dx, dy)

            if 0.0 <= along <= max_forward and perp <= max_perp and euclid <= search_radius:
                z = float(segment_z_at_point(ep, es))
                # Favor points directly ahead of the curb end and close perpendicular.
                score = perp * 2.0 + along * 0.25
                candidates.append((score, endpoint_station, z, endpoint_pt, es.layer, along, perp, euclid, endpoint_name))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    _score, st, z, q, layer, along, perp, euclid, endpoint_name = candidates[0]
    src = (
        f"{z:.3f} endpoint-extension contour control at {endpoint_name} of selected BACK OF CURB on {layer}; "
        f"contour is {along:.2f} ft beyond curb end and {perp:.2f} ft off tangent"
    )
    return (st, z, q, src, "BC_ENDPOINT_EXTENSION_CONTOUR_DIRECT")

def curb_chain_contour_elevation(p: Point2, elev_segments: List[ElevSegment], search_radius: float, default_curb_height: float) -> Tuple[Optional[float], str, str, float]:
    """Correct 2D curb contour interpretation along the selected BC chain.

    Survey grading rule used here:
    * Contour crossing selected BACK OF CURB is a true BC elevation, no add.
    * Contour crossing selected FLOWLINE/GUTTER/EP becomes BC by adding curb height.
    * Points between those controls are prorated/interpolated along the selected BC chain.
    * One-sided/distant hits are not trusted over a surface/TIN unless they are close.
    """
    global CURB_SEGMENTS, FLOWLINE_SEGMENTS
    if not CURB_SEGMENTS or not elev_segments:
        return None, "no selected BC contour chain available", "", 0.0
    # v8.4.9: station on the local curb run/component, not all selected curbs as one chain.
    local_curb_segments = safe_local_curb_chain(p)
    if not local_curb_segments:
        return None, "no local selected BC component found", "", 1e99
    p_station, p_q, p_d, p_idx = _chain_station_for_point_on_segments(p, local_curb_segments)
    if p_station is None or p_q is None or p_d > max(2.0, search_radius * 0.05):
        return None, "point is not on selected BC chain for contour-chain interpolation", "", p_d if p_station is not None else 1e99

    contour_segments = [es for es in elev_segments if is_contour_crossing_source(es)]
    anchors = []  # station, elev, q_on_bc, source, conf

    # BC intersections: exact contour elevation.
    cum = 0.0
    for i, cs in enumerate(local_curb_segments):
        L = max(dist(cs.a, cs.b), 1e-12)
        for es in contour_segments:
            inter = segment_intersection(cs.a, cs.b, es.a, es.b)
            if inter is None:
                continue
            t, u, q = inter
            z = segment_z_at_point(q, es)
            st = cum + max(0.0, min(1.0, t)) * L
            anchors.append((st, float(z), q, f"{z:.3f} exact contour crossing selected BACK OF CURB on {es.layer}", "BC_CONTOUR_EXACT"))
        cum += L

    # Projected near/short-gap contours cannot become approved interpolation anchors.
    # FL/gutter intersections: project crossing to BC and add PDF verified curb height.
    if FLOWLINE_SEGMENTS:
        for fs in FLOWLINE_SEGMENTS:
            for es in contour_segments:
                inter = segment_intersection(fs.a, fs.b, es.a, es.b)
                if inter is None:
                    continue
                t, u, q_fl = inter
                q_bc, d_bc = _nearest_point_on_curb(q_fl, max_dist=max(12.0, search_radius * 0.25))
                if q_bc is None:
                    continue
                st, q_proj, d_proj, idx = _chain_station_for_point_on_segments(q_bc, local_curb_segments)
                if st is None or d_proj > 1e-6:
                    continue
                z_fl = segment_z_at_point(q_fl, es)
                z_bc = float(z_fl) + float(default_curb_height)
                anchors.append((st, z_bc, q_proj if q_proj is not None else q_bc, f"{z_bc:.3f} from contour crossing selected FLOWLINE/GUTTER/EP on {es.layer}: {z_fl:.3f} + curb height {default_curb_height:.2f}", "FL_CONTOUR_PLUS_CURB_EXACT"))

    if not anchors:
        return None, "no BC or FL/gutter contour crossings found on selected curb components", "", p_d

    # Deduplicate anchors at the same station.  If BC and FL are at nearly same station,
    # BC exact is safer for BC staking because it physically touches selected BC.
    anchors.sort(key=lambda a: a[0])
    ded=[]
    def _anchor_priority(a):
        # Direct selected-BC controls must outrank FL/gutter controls at the same station.
        # v8.4.7: this includes short-gap/projected BC contour controls, not just exact CAD intersections.
        conf = (a[4] or "").upper()
        if "BC_CONTOUR_EXACT" in conf:
            return 0
        if "BC_NEAR_POINT" in conf:
            return 0
        if "BC_ENDPOINT_EXTENSION" in conf:
            return 0
        if "BC_STOP_SHORT" in conf or "CONTOUR_DIRECT" in conf:
            return 1
        if "FL_CONTOUR_PLUS_CURB" in conf:
            return 3
        return 2

    for a in anchors:
        if ded and abs(a[0]-ded[-1][0]) <= 0.05:
            if _anchor_priority(a) == _anchor_priority(ded[-1]) and abs(a[1] - ded[-1][1]) > 1e-6:
                return None, 'Conflicting contour grades at the same curb station; review required.', 'CONTROL_CONFLICT_REVIEW', 0.0
            # Prefer an actual BC intersection over the converted FL control.
            if _anchor_priority(a) < _anchor_priority(ded[-1]):
                ded[-1] = a
        else:
            ded.append(a)
    anchors = ded

    direct = min(anchors, key=lambda a: abs(a[0] - p_station))
    direct_dist_station = abs(direct[0] - p_station)
    if direct_dist_station <= max(0.35, search_radius * 0.01):
        z, src, conf = direct[1], direct[3], direct[4]
        issue_src = " direct hold on selected curb component"
        return z, src + issue_src, conf, max(p_d, dist(p_q, direct[2]))

    before = [a for a in anchors if a[0] <= p_station]
    after = [a for a in anchors if a[0] >= p_station]
    if before and after:
        lo = before[-1]
        hi = after[0]
        span = hi[0] - lo[0]
        if span > 1e-9:
            f = (p_station - lo[0]) / span
            z = lo[1] + (hi[1] - lo[1]) * f
            # Large grade jumps are possible but must be flagged.  Do not hide them.
            slope = abs(hi[1] - lo[1]) / max(span, 1e-9)
            src = f"SURVEY INTERPOLATION: prorated/averaged by station along selected BC chain between curb-component contour controls: [{lo[3]}] and [{hi[3]}]"
            conf = "BC_CHAIN_CONTOUR_INTERP"
            if slope > 0.12:
                src += f"; REVIEW large curb profile slope {slope*100:.1f}% between contour controls"
                conf = "BC_CHAIN_CONTOUR_REVIEW"
            return z, src, conf, min(abs(p_station-lo[0]), abs(hi[0]-p_station))
        return lo[1], lo[3], lo[4], abs(lo[0]-p_station)

    # One-sided anchors can explain a value but should not outrank TIN/surface unless close.
    if direct_dist_station <= min(search_radius, 10.0):
        return direct[1], direct[3] + " one-sided curb-component contour fallback, review", "LOW_BC_CHAIN_CONTOUR", direct_dist_station
    return None, "curb-component contour anchors exist but do not bracket this point", "", direct_dist_station

def assign_elevations(points, labels, segments, default_curb_height, label_radius, contour_radius,
                      add_curb_height_to_contours, contour_check_tolerance=0.15, landxml_surface=None,
                      surface_mode='bc', contour_tin=None, source_policy='surface', curb_controls=True):
    """Assign once under an explicit source policy; later passes cannot redesign grades."""
    out = []
    def surface_result(p):
        z, src, conf = landxml_elevation_at(p, landxml_surface, search_pad=max(5, min(contour_radius,25)))
        if z is None:
            return None, src, 'Review required: selected LandXML does not cover this point.', 'SURFACE_REVIEW'
        z, src, conf, issue = contour_bc_rule(p,z,src,conf,0,default_curb_height,surface_mode)
        return z, src, issue, conf
    for p in points:
        z, src, conf = nearest_label_elevation(p, labels, default_curb_height, label_radius)
        if z is not None or conf == 'CONTROL_CONFLICT_REVIEW':
            out.append((z,src,'' if conf == 'HELD_CONTROL' else 'Review required: confirm the control location/value.',conf))
            continue
        override = get_contour_point_override(p) if curb_controls else None
        if override:
            z,src,conf,issue = override
            out.append((z,src,issue,conf)); continue
        if landxml_surface is not None and source_policy == 'surface':
            out.append(surface_result(p)); continue
        if source_policy == 'dxf_surface':
            surface_segments = [es for es in segments if is_surface_layer(es.layer)]
            z,src,conf,d = contour_elevation(p,surface_segments,contour_radius)
            if z is not None and d <= 0.25:
                z,src,conf,issue = contour_bc_rule(p,z,src,'FEATURELINE_HIGH',d,default_curb_height,surface_mode)
                out.append((z,src,issue,conf)); continue
        if curb_controls:
            z,src,conf,_d = curb_chain_contour_elevation(p,segments,contour_radius,default_curb_height)
            if conf == 'CONTROL_CONFLICT_REVIEW':
                out.append((None,src,'Review required: conflicting contour controls.',conf)); continue
            if z is not None and conf in ('BC_CONTOUR_EXACT','FL_CONTOUR_PLUS_CURB_EXACT','BC_CHAIN_CONTOUR_INTERP'):
                out.append((z,src,'',conf)); continue
        if source_policy == 'contours':
            out.append((None,'No bracketing selected curb contour controls','Review required: contours-only mode has no approved control.','CONTOUR_REVIEW')); continue
        if landxml_surface is not None:
            out.append(surface_result(p)); continue
        z,src,conf,d = local_contour_prorated_elevation(p,segments,contour_radius)
        if z is not None:
            # Unconstrained local estimates remain office-review evidence. Actual
            # curb crossings and connected station controls are handled above.
            out.append((z,src,'Review required: local contour estimate is not an approved surface/control.','LOCAL_CONTOUR_REVIEW')); continue
        z,src,conf = contour_tin_elevation(p,contour_tin)
        if z is not None:
            out.append((z,src,'Review required: temporary contour TIN has no approved boundaries/breaklines.','TIN_REVIEW')); continue
        z,src,conf,_d = contour_elevation(p,segments,contour_radius)
        out.append((z,src,'Review required: nearby contour fallback is not a field elevation.','CONTOUR_REVIEW'))
    return out

def add_flow_warnings(points: List[GenPoint], min_slope: float = 0.003, max_gap: float = 55.0):
    # This is a first pass. It flags flat pairs only when consecutive generated points are close enough to be likely on the same run.
    for a, b, d in diagnostic_pairs(points):
        if a.elevation is None or b.elevation is None:
            continue
        if d <= 0.1 or d > max_gap:
            continue
        if CURB_SEGMENTS and not same_curb_run((a.easting,a.northing),(b.easting,b.northing)):
            continue
        slope = abs(b.elevation - a.elevation) / d
        if slope < min_slope:
            msg = f"Possible no flow or very flat curb segment to point {b.point}. Slope {slope*100:.2f} percent."
            if a.issue:
                a.issue += " " + msg
            else:
                a.issue = msg




def closest_point_on_segment(p: Point2, a: Point2, b: Point2) -> Tuple[Point2, float]:
    ax, ay = a
    bx, by = b
    px, py = p
    vx, vy = bx - ax, by - ay
    c2 = vx * vx + vy * vy
    if c2 <= 1e-12:
        return a, dist(p, a)
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / c2))
    q = (ax + t * vx, ay + t * vy)
    return q, dist(p, q)


def nearest_segment_info(p: Point2, segments: List[CurbSegment]) -> Tuple[Optional[CurbSegment], Optional[Point2], float]:
    best_seg = None
    best_q = None
    best_d = 1e99
    for seg in segments:
        q, d = closest_point_on_segment(p, seg.a, seg.b)
        if d < best_d:
            best_seg = seg
            best_q = q
            best_d = d
    return best_seg, best_q, best_d


def unit_vector(vx: float, vy: float) -> Optional[Point2]:
    l = math.hypot(vx, vy)
    if l <= 1e-9:
        return None
    return (vx / l, vy / l)


def offset_direction_for_bc_point(p: Point2, mode: str = "away_from_flowline") -> Tuple[Point2, str]:
    """Return the direction to offset a BC staking point.

    Best case: user picked the flowline/gutter. Then "back from curb" means
    continue from the flowline through the BC point and farther away from gutter.
    Fallback: use the nearest BC segment normal, left or right by user choice.
    """
    mode = (mode or "away_from_flowline").lower()
    if mode in ("away_from_flowline", "toward_flowline") and FLOWLINE_SEGMENTS:
        fl_seg, q, d = nearest_segment_info(p, FLOWLINE_SEGMENTS)
        if q is not None:
            u = unit_vector(p[0] - q[0], p[1] - q[1])
            if u is not None:
                
                if mode == "toward_flowline":
                    return (-u[0], -u[1]), f"toward selected flowline/gutter, nearest FL distance {d:.2f} ft"
                return u, f"away from selected flowline/gutter, nearest FL distance {d:.2f} ft"

    seg, q, d = nearest_segment_info(p, CURB_SEGMENTS)
    if seg is not None:
        tx, ty = seg.b[0] - seg.a[0], seg.b[1] - seg.a[1]
        u = unit_vector(tx, ty) or (1.0, 0.0)
        left = (-u[1], u[0])
        right = (u[1], -u[0])
        if mode == "right":
            return right, "right of nearest BC segment"
        return left, "left of nearest BC segment"

    return (1.0, 0.0), "fallback east offset; no curb segment found"


def make_offset_points(points: List[GenPoint], offset_distance: float, mode: str = "away_from_flowline", reverse: bool = False, start_point: Optional[int] = None) -> Tuple[List[GenPoint], List[Tuple[GenPoint, GenPoint, str]]]:
    """Create contractor offset staking points and line pairs back to true BC.

    The original direct BC file is unchanged. Offset points are exported to a
    separate CSV so jobs that do not want offsets are not affected.
    """
    if not points or abs(offset_distance) <= 1e-9:
        return [], []
    next_pt = int(start_point) if start_point is not None else (max(p.point for p in points) + 1)
    offsets: List[GenPoint] = []
    pairs: List[Tuple[GenPoint, GenPoint, str]] = []
    for bc in points:
        p = (bc.easting, bc.northing)
        direction, note = offset_direction_for_bc_point(p, mode=mode)
        if reverse:
            direction = (-direction[0], -direction[1])
            note = "REVERSED offset direction. " + note
        ox = p[0] + direction[0] * offset_distance
        oy = p[1] + direction[1] * offset_distance
        issue = f"Offset point {offset_distance:.2f} ft from true BC. Draw line back to BC. {note}"
        op = GenPoint(point=next_pt, northing=oy, easting=ox, elevation=bc.elevation, desc=bc.desc, source="Offset from BC", issue=issue, confidence="OFFSET", approved=field_approval(bc)[0])
        offsets.append(op)
        pairs.append((bc, op, note))
        next_pt += 1
    return offsets, pairs


def write_offset_lines_dxf(pairs: List[Tuple[GenPoint, GenPoint, str]], path: str, offset_distance: float, offset_mode: str = "away_from_flowline", reverse: bool = False):
    """Write contractor offset DXF.

    This creates three useful pieces of linework:
    1) tie lines from each offset point back to the true BC point,
    2) true BC reference linework, and
    3) a continuous-looking offset curb line made from the selected BC segments.

    Curved curb returns are already tessellated into small BC segments when loaded,
    so this DXF will show the offset curve instead of only straight point-to-point ties.
    """
    if ezdxf is None:
        return
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    try:
        doc.layers.add("GRADEGUARD-BC-OFFSET-TIE-LINES", color=1)
        doc.layers.add("GRADEGUARD-BC-OFFSET-POINTS", color=3)
        doc.layers.add("GRADEGUARD-TRUE-BC-POINTS", color=2)
        doc.layers.add("GRADEGUARD-TRUE-BC-LINE", color=8)
        doc.layers.add("GRADEGUARD-OFFSET-CURB-LINE", color=5)
    except Exception:
        pass

    # Draw true BC and offset curb linework. Curves show up because selected curved
    # polylines/arcs are stored as many small segments in CURB_SEGMENTS.
    try:
        for cs in CURB_SEGMENTS:
            a = cs.a
            b = cs.b
            msp.add_line(a, b, dxfattribs={"layer": "GRADEGUARD-TRUE-BC-LINE"})
            da, _na = offset_direction_for_bc_point(a, mode=offset_mode)
            db, _nb = offset_direction_for_bc_point(b, mode=offset_mode)
            if reverse:
                da = (-da[0], -da[1])
                db = (-db[0], -db[1])
            ao = (a[0] + da[0] * offset_distance, a[1] + da[1] * offset_distance)
            bo = (b[0] + db[0] * offset_distance, b[1] + db[1] * offset_distance)
            if dist(ao, bo) > 0.001:
                msp.add_line(ao, bo, dxfattribs={"layer": "GRADEGUARD-OFFSET-CURB-LINE"})
    except Exception:
        pass

    for bc, op, note in pairs or []:
        bc_xy = (bc.easting, bc.northing)
        op_xy = (op.easting, op.northing)
        msp.add_line(op_xy, bc_xy, dxfattribs={"layer": "GRADEGUARD-BC-OFFSET-TIE-LINES"})
        msp.add_circle(op_xy, radius=max(0.35, abs(offset_distance) * 0.05), dxfattribs={"layer": "GRADEGUARD-BC-OFFSET-POINTS"})
        msp.add_circle(bc_xy, radius=max(0.25, abs(offset_distance) * 0.035), dxfattribs={"layer": "GRADEGUARD-TRUE-BC-POINTS"})
        txt = f"{op.point} {offset_distance:.2f}' OFF BC"
        try:
            msp.add_text(txt, dxfattribs={"layer": "GRADEGUARD-BC-OFFSET-POINTS", "height": max(1.0, abs(offset_distance) * 0.25)}).set_placement((op.easting, op.northing))
        except Exception:
            pass
    doc.saveas(path)


def normalize_high_low_descriptions(points: List[GenPoint]):
    """Use special descriptions at HP/LP while preserving PC/PT where possible.

    Normal curb points export as BC, curve controls export as BC PC or BC PT, and
    only true high/low points export as BC HP or BC LP.  HP/LP overrides PC/PT.
    """
    for p in points:
        src = (p.source or '').upper()
        conf = (p.confidence or '').upper()
        issue = (p.issue or '').upper()
        if 'LP_TC' in src or ' LP' in src or 'POSSIBLE LP' in src or conf == 'LP_REVIEW' or 'LOW POINT' in issue:
            p.desc = 'BC LP'
        elif 'HP_TC' in src or ' HP' in src or 'POSSIBLE HP' in src or conf == 'HP_REVIEW' or 'HIGH POINT' in issue:
            p.desc = 'BC HP'
        elif _near_any_control(p, CURB_PC_POINTS):
            p.desc = 'BC PC'
        elif _near_any_control(p, CURB_PT_POINTS):
            p.desc = 'BC PT'
        else:
            p.desc = 'BC'






def _is_hp_lp_label_kind(kind: str) -> bool:
    return (kind or '').upper() in {'HP', 'LP', 'HP_TC', 'LP_TC'}

def _is_hp_label_kind(kind: str) -> bool:
    return (kind or '').upper().startswith('HP')

def hard_lock_hp_lp_callout_elevations(points: List[GenPoint], labels: List[GradeLabel], tolerance: float = 2.50) -> int:
    """v8.4.8 hard lock for designed high and low points.

    HP/LP callouts are vertical break points.  They must not be smoothed,
    prorated, overwritten by contour station interpolation, or zeroed by the
    review sanitizer when a trusted HP/LP elevation exists.

    This function runs after initial elevation assignment and again after the
    sanitizer as a belt-and-suspenders guard.  It finds generated BC points that
    match projected/manual/CAD/PDF HP/LP labels and forces the exact callout
    elevation and description.
    """
    if not points or not labels:
        return 0
    hp_lp_labels = [lab for lab in labels if _is_hp_lp_label_kind(getattr(lab, 'kind', ''))]
    if not hp_lp_labels:
        return 0
    locked = 0
    used_point_ids = set()
    for lab in hp_lp_labels:
        try:
            z = float(lab.elev)
        except Exception:
            continue
        if not valid_grade_z(z):
            continue
        lx, ly = float(lab.x), float(lab.y)
        best = None
        best_d = 1e99
        for gp in points:
            d = dist((gp.easting, gp.northing), (lx, ly))
            if d < best_d:
                best = gp
                best_d = d
        if best is None or best_d > tolerance:
            continue
        # Allow the same generated point to receive one lock only.  If duplicates
        # exist, first HP/LP label wins and the source report shows what happened.
        if id(best) in used_point_ids:
            continue
        used_point_ids.add(id(best))
        is_hp = _is_hp_label_kind(lab.kind)
        best.elevation = z
        best.locked = True
        best.desc = 'BC HP' if is_hp else 'BC LP'
        best.source = (best.source + '; ' if best.source else '') + (
            f"v8.4.8 HP/LP HARD HOLD: {lab.kind} callout held exact elevation {z:.3f} "
            f"at leader/tick/projected BC point from {lab.raw}"
        )
        best.confidence = (best.confidence + '; ' if best.confidence else '') + (
            'HP_HARD_HOLD' if is_hp else 'LP_HARD_HOLD'
        )
        best.issue = (best.issue + ' ' if best.issue else '') + (
            'HP/LP hard-lock active. This is a vertical break point and was not averaged or interpolated through.'
        )
        locked += 1
    return locked


def fill_missing_bc_elevations_by_station_interpolation(points: List[GenPoint], max_bracket_span: float = 90.0, max_near_hold_station: float = 8.0) -> int:
    """v8.4.1 safety upgrade: fill missing BC elevations by station between contour controls.

    Why this exists:
    Some runs had real contour crossing/control points in the generated file, but
    nearby PC/PT/HP/LP/25-ft points were still being exported as 0.000 because the
    conservative review sanitizer rejected values with the word REVIEW in their
    source/issue text.  For curb staking, once we have clean BC-equivalent controls
    on the selected curb chain, the correct survey workflow is to prorate by
    station between those controls.

    Rules:
    - Only uses points that already have nonzero elevations in the same generated run.
    - Projects every point to the selected BC chain.
    - If a missing point is bracketed by valid nonzero controls, linearly interpolates.
    - If a missing point is almost on a valid control, holds that control.
    - Does not bridge long gaps beyond max_bracket_span.
    - Keeps the source text explicit so the review CSV proves what happened.
    """
    if not points or not CURB_SEGMENTS:
        return 0

    stationed = []
    for idx, gp in enumerate(points):
        st, q, d, seg_idx = _bc_chain_station_for_point((gp.easting, gp.northing))
        if st is None or q is None or d > 4.0:
            continue
        stationed.append((idx, gp, float(st), q, float(d), seg_idx))

    anchors = []
    for idx, gp, st, q, d, seg_idx in stationed:
        if gp.elevation is None:
            continue
        try:
            z = float(gp.elevation)
        except Exception:
            continue
        if not valid_grade_z(z):
            continue

        txt = " ".join([(gp.confidence or "").upper(), (gp.source or "").upper(), (gp.issue or "").upper()])
        # Do not use already unsafe/zeroed points as controls.
        if "ZEROED" in txt or "UNKNOWN" in txt or "UNSAFE" in txt:
            continue
        anchors.append((st, z, idx, gp, seg_idx))

    if len(anchors) < 1:
        return 0

    anchors.sort(key=lambda a: a[0])
    filled = 0

    for idx, gp, st, q, d, seg_idx in stationed:
        try:
            current_z = None if gp.elevation is None else float(gp.elevation)
        except Exception:
            current_z = None
        if current_z is not None and abs(current_z) > 1e-9:
            # v8.4.2: never re-average or replace an existing HP/LP/TC/BC held value.
            # A plan high point or low point is a hard vertical control, not a smoothing point.
            continue

        hp_lp_text = " ".join([(gp.desc or "").upper(), (gp.confidence or "").upper(), (gp.source or "").upper(), (gp.issue or "").upper()])
        if ("BC HP" in hp_lp_text or "BC LP" in hp_lp_text or "HP_TC" in hp_lp_text or "LP_TC" in hp_lp_text or
                "HIGH POINT" in hp_lp_text or "LOW POINT" in hp_lp_text or "HP/LP CALLOUT" in hp_lp_text):
            # If a point is identified as a plan HP/LP but has no trustworthy elevation,
            # leave it at 0.000 for review instead of interpolating through it.
            # Interpolation can calculate between contours, but it must not erase a
            # designed vertical break point.
            gp.issue = (gp.issue + " " if gp.issue else "") + (
                "v8.4.2 HP/LP protection: point was identified as a high/low point but had no held elevation, "
                "so GradeGuard did not average through it. Verify the PDF tick/callout and enter a manual anchor if needed."
            )
            gp.confidence = (gp.confidence + "; HP_LP_REVIEW_HOLD").strip("; ") if gp.confidence else "HP_LP_REVIEW_HOLD"
            continue

        # Direct/near hold first.  This catches a PC/PT/25-ft point sitting almost
        # exactly on a contour crossing point generated from the same selected BC.
        local_anchors = [a for a in anchors if same_curb_run((gp.easting,gp.northing),(a[3].easting,a[3].northing))]
        if not local_anchors:
            continue
        nearest = min(local_anchors, key=lambda a: abs(a[0] - st))
        near_sta = abs(nearest[0] - st)
        if near_sta <= max_near_hold_station:
            gp.elevation = nearest[1]
            gp.source = (gp.source + "; " if gp.source else "") + (
                f"v8.4.1 NEAR CONTROL HOLD: held {nearest[1]:.3f} from point {nearest[3].point} "
                f"{near_sta:.2f} ft away by station on selected BC chain"
            )
            gp.confidence = "BC_POST_NEAR_HOLD"
            gp.issue = (gp.issue + " " if gp.issue else "") + (
                "Filled former 0.000 from nearby nonzero BC control on the selected curb chain."
            )
            filled += 1
            continue

        before = [a for a in anchors if a[0] < st and same_curb_run((gp.easting,gp.northing),(a[3].easting,a[3].northing))]
        after = [a for a in anchors if a[0] > st and same_curb_run((gp.easting,gp.northing),(a[3].easting,a[3].northing))]
        if not before or not after:
            continue

        lo = before[-1]
        hi = after[0]
        span = hi[0] - lo[0]
        if span <= 1e-9 or span > max_bracket_span:
            continue

        f = (st - lo[0]) / span
        z = lo[1] + (hi[1] - lo[1]) * f
        gp.elevation = float(z)
        gp.source = (gp.source + "; " if gp.source else "") + (
            f"v8.4.1 SURVEY INTERPOLATION: prorated by BC station between point {lo[3].point} "
            f"elev {lo[1]:.3f} and point {hi[3].point} elev {hi[1]:.3f}; span {span:.2f} ft"
        )
        gp.confidence = "BC_POST_CHAIN_INTERP"
        gp.issue = (gp.issue + " " if gp.issue else "") + (
            "Filled former 0.000 by linear station interpolation between valid BC contour/control elevations."
        )
        filled += 1

    return filled



def apply_bc_station_contour_profile_to_normal_points(points: List[GenPoint], elev_segments: List[ElevSegment], search_radius: float, default_curb_height: float) -> int:
    """v8.4.9: use the contour's station on the local selected BC run, not just straight-line closeness.

    This is the correction for places where a contour clearly controls the BC run but the
    drawn contour/curb linework stops short. GradeGuard now finds the contour's projected
    station on the BC chain, then prorates normal BC points by distance along that station.

    Safety rules:
    - Do not overwrite BC HP / BC LP points. Those are vertical break points and must hold.
    - Do not overwrite PDF/manual hard callouts.
    - Direct BC contour and short-gap BC contour controls are used directly, no curb height.
    - FL/gutter controls only get curb height when they truly come from selected FL/gutter.
    """
    if not points or not CURB_SEGMENTS or not elev_segments:
        return 0

    changed = 0
    for gp in points:
        desc_txt = (gp.desc or "").upper()
        all_txt = " ".join([(gp.desc or "").upper(), (gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])

        # HP/LP are hard vertical controls, not smoothing points.
        if "BC HP" in desc_txt or "BC LP" in desc_txt or "HIGH POINT" in all_txt or "LOW POINT" in all_txt or "HP_TC" in all_txt or "LP_TC" in all_txt:
            continue

        # Do not override a manually held/PDF tick elevation. The tick/leader endpoint holds.
        if "MANUAL" in all_txt or "PDF_TICK" in all_txt or "PDF LEADER" in all_txt or "PDF_SPOT" in all_txt or "HP/LP CALLOUT" in all_txt:
            continue

        p = (gp.easting, gp.northing)
        z, src, conf, sta_d = curb_chain_contour_elevation(p, elev_segments, search_radius, default_curb_height)
        if z is None:
            continue
        conf_u = (conf or "").upper()
        src_u = (src or "").upper()

        # Only accept selected-BC chain controls/interpolation. Avoid replacing a good value
        # with a generic TIN/nearest-contour fallback.
        ok = (
            "BC_CONTOUR_EXACT" in conf_u or
            "BC_STOP_SHORT" in conf_u or
            "BC_NEAR_POINT" in conf_u or
            "BC_ENDPOINT_EXTENSION" in conf_u or
            "BC_CHAIN_CONTOUR_INTERP" in conf_u or
            "BC_CHAIN_CONTOUR_REVIEW" in conf_u or
            "FL_CONTOUR_PLUS_CURB_EXACT" in conf_u or
            "SURVEY INTERPOLATION" in src_u
        )
        if not ok:
            continue

        try:
            old_z = None if gp.elevation is None else float(gp.elevation)
        except Exception:
            old_z = None

        # Replace zeros/missing, and also fix values that came from the wrong curb-height side,
        # such as a direct 610 contour being treated as 610.50.
        replace = old_z is None or abs(old_z) < 1e-9 or abs(float(z) - old_z) >= 0.20
        if not replace:
            continue

        gp.elevation = float(z)
        gp.source = (gp.source + "; " if gp.source else "") + (
            "v8.4.7 STATIONED CONTOUR PROFILE: used contour control by distance along the selected BC station. " + src
        )
        gp.confidence = (gp.confidence + "; " if gp.confidence else "") + "BC_STATION_CONTOUR_PROFILE"
        gp.issue = (gp.issue + " " if gp.issue else "") + (
            "v8.4.7 corrected/filled this normal BC point from the selected-BC contour profile. "
            "Distance was measured along the curb station to the contour control, not just by nearest XY distance."
        )
        changed += 1


    return changed


def is_hp_lp_or_manual_locked_point(gp: GenPoint) -> bool:
    """True when a point must not be changed by contour profile cleanup."""
    txt = " ".join([(gp.desc or "").upper(), (gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])
    locked_tokens = [
        "BC HP", "BC LP", "HP_TC", "LP_TC", "HIGH POINT", "LOW POINT",
        "HP/LP CALLOUT", "HP_HARD_HOLD", "LP_HARD_HOLD",
        "MANUAL", "PDF_TICK", "PDF LEADER", "PDF_SPOT", "HELD_CONTROL", "HELD CONTROL"
    ]
    return any(tok in txt for tok in locked_tokens)



def is_strong_curb_component_control_v854(gp: GenPoint) -> bool:
    """True when the point already came from the correct curb-component contour logic.

    v8.5.5 locks the correct survey workflow:
      BC contour hit = direct BC elevation.
      EP/FL/gutter contour hit = contour + curb height.
      Normal point between those controls = prorated by BC station.
    Surface/TIN passes must not overwrite these results.
    """
    txt = " ".join([(gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])
    tokens = [
        "BC_CHAIN_CONTOUR_INTERP", "BC_CHAIN_CONTOUR_REVIEW",
        "BC_CONTOUR_EXACT", "FL_CONTOUR_PLUS_CURB_EXACT",
        "FL_TOUCH_PLUS_CURB", "FL_TOUCH_INTERP_PLUS_CURB",
        "BC_TOUCH", "BC_TOUCH_INTERP",
        "BC_CONTOUR_PROFILE_PRORATE_V850",
        "BC_DIRECT_CONTOUR_HOLD_V850",
        "BC_STATION_CONTOUR_PROFILE",
        "BC_ENDPOINT_EXTENSION_CONTOUR_DIRECT",
        "BC_STOP_SHORT_CONTOUR_DIRECT",
    ]
    return any(tok in txt for tok in tokens)


def is_fl_ep_plus_curb_control_v854(gp: GenPoint) -> bool:
    txt = " ".join([(gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])
    return (("FLOWLINE" in txt or "GUTTER" in txt or " EP" in txt or "FL/EP" in txt or "FL_CONTOUR" in txt or "FL_TOUCH" in txt)
            and ("PLUS CURB" in txt or "+ CURB" in txt or "CURB HEIGHT" in txt))


def protect_close_hp_lp_clusters_v857(points: List[GenPoint], cluster_tol: float = 1.25, max_allowed_delta: float = 0.20) -> int:
    """v8.5.7: protect normal points that fall on top of a high or low point.

    If a generated normal BC point is within drafting tolerance of a BC HP or BC LP,
    it is part of that same vertical break location. It must not be pulled down or up
    by an endpoint extension contour from beyond the curb end.

    This fixes the 6006 type problem:
      6005 was a nearby LP control.
      6006 was only about half a foot away.
      A distant endpoint extension contour pulled 6006 to 609.
      The correct behavior is to hold the nearby LP cluster unless a real PDF callout
      or direct BC contour says otherwise.
    """
    if not points:
        return 0

    controls = []
    for gp in points:
        desc = (gp.desc or "").upper()
        txt = " ".join([(gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])
        if ("BC HP" in desc or "BC LP" in desc or "HP_HARD_HOLD" in txt or "LP_HARD_HOLD" in txt
                or "POSSIBLE HP" in txt or "POSSIBLE LP" in txt):
            try:
                z = float(gp.elevation)
            except Exception:
                continue
            if valid_grade_z(z):
                controls.append(gp)

    if not controls:
        return 0

    changed = 0
    for gp in points:
        desc = (gp.desc or "").upper()
        all_txt = " ".join([(gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper(), desc])

        # True manual or PDF callouts still win.
        if "MANUAL" in all_txt or "PDF_TICK" in all_txt or "PDF LEADER" in all_txt or "PDF_SPOT" in all_txt:
            continue

        # Do not change the control point itself.
        if "BC HP" in desc or "BC LP" in desc:
            continue

        try:
            z = float(gp.elevation)
        except Exception:
            continue

        p = (float(gp.easting), float(gp.northing))
        best = None
        best_d = 1e99
        for hp_lp in controls:
            d = dist(p, (float(hp_lp.easting), float(hp_lp.northing)))
            if d < best_d:
                best = hp_lp
                best_d = d

        if best is None or best_d > cluster_tol:
            continue

        bz = float(best.elevation)
        if abs(z - bz) <= max_allowed_delta:
            continue

        # Only replace suspicious jumps near the HP or LP. This prevents a bad endpoint
        # extension contour from creating a vertical cliff at a duplicate or near duplicate point.
        gp.elevation = bz
        gp.source = (gp.source + "; " if gp.source else "") + (
            f"v8.5.7 HP/LP CLUSTER HOLD: point is {best_d:.2f} ft from nearby {best.desc} "
            f"point {best.point} elev {bz:.3f}; held cluster elevation instead of isolated endpoint/profile value."
        )
        gp.confidence = (gp.confidence + "; " if gp.confidence else "") + "HP_LP_CLUSTER_HOLD_V857"
        gp.issue = (gp.issue + " " if gp.issue else "") + (
            "v8.5.7 removed a local jump at a high/low point cluster. "
            "Nearby normal BC points at the same break location are held to the HP/LP elevation unless a direct PDF/manual callout overrides."
        )
        changed += 1

    return changed

def force_nearby_contour_direct_holds_v850(points: List[GenPoint], elev_segments: List[ElevSegment], direct_tol: float = 0.35, review_tol: float = 1.25) -> int:
    """v8.5.0 direct contour hold pass.

    This fixes the Brayfield issue where a plotted contour physically at the BC
    point was still getting treated like FL/gutter and raised by curb height.

    Survey rule:
    - If a contour line is on the generated BC point, or within drafting tolerance,
      hold that contour elevation directly.
    - Do NOT add curb height to this direct BC contour hold.
    - HP/LP/manual/PDF callouts remain hard locked and are not overwritten.
    """
    if not points or not elev_segments:
        return 0

    contour_segments = [es for es in elev_segments if is_contour_crossing_source(es)]
    if not contour_segments:
        return 0

    changed = 0
    for gp in points:
        if is_hp_lp_or_manual_locked_point(gp):
            continue
        if is_fl_ep_plus_curb_control_v854(gp):
            # v8.5.5: do not undo a correct EP/FL/gutter + curb-height control.
            continue

        p = (gp.easting, gp.northing)
        best = None
        for es in contour_segments:
            try:
                d = point_segment_distance(p, es.a, es.b)
            except Exception:
                continue
            if d > review_tol:
                continue
            try:
                _t, q, _d2 = closest_point_t_on_segment(p, es.a, es.b)
                z = float(segment_z_at_point(q, es))
            except Exception:
                continue
            if not valid_grade_z(z):
                continue
            score = d
            # Prefer major contour direct holds only when distances tie.
            if "MAJOR" in (es.layer or "").upper():
                score -= 0.02
            cand = (score, d, z, es.layer)
            if best is None or cand < best:
                best = cand

        if best is None:
            continue

        _score, d, z, layer = best
        # Direct hold if the contour is effectively at the BC point.
        # Also fix the exact 0.50 curb-height error when the output is one curb
        # height above a direct contour.
        try:
            old_z = None if gp.elevation is None else float(gp.elevation)
        except Exception:
            old_z = None

        is_bad_curb_add = (
            old_z is not None and
            abs((old_z - z) - 0.50) <= 0.08 and
            d <= review_tol
        )

        if d <= direct_tol or is_bad_curb_add:
            if old_z is None or abs(old_z - z) >= 0.005:
                gp.elevation = float(z)
                gp.source = (gp.source + "; " if gp.source else "") + (
                    f"v8.5.0 DIRECT CONTOUR HOLD: held {z:.3f} directly from {layer} "
                    f"because contour is {d:.2f} ft from the generated BC point. No curb height added."
                )
                gp.confidence = (gp.confidence + "; " if gp.confidence else "") + "BC_DIRECT_CONTOUR_HOLD_V850"
                gp.issue = (gp.issue + " " if gp.issue else "") + (
                    "v8.5.0 corrected contour-at-BC handling. A contour on/at BC controls directly; "
                    "curb height is only added to true selected FL/gutter/EP controls."
                )
                changed += 1

    return changed


def rebuild_bc_profile_from_contours_v850(points: List[GenPoint], elev_segments: List[ElevSegment], max_gap: float = 80.0) -> int:
    """v8.5.0 profile smoothing by actual contour controls along each curb run.

    This does not use nearest XY as the final answer.  It uses direct contour
    holds as station controls, then prorates normal BC points by point sequence
    and station distance.  HP/LP/manual/PDF callouts are not overwritten.

    This is intentionally conservative: it only changes a point when both sides
    are bracketed by strong direct contour controls on the same local run.
    """
    if not points or not CURB_SEGMENTS:
        return 0

    # Build local station info for all points.
    rows = []
    for i, gp in enumerate(points):
        p = (gp.easting, gp.northing)
        # v8.4.9 local component station first
        local = _local_curb_segments_for_point(p)
        st, q, d, seg_idx = _chain_station_for_point_on_segments(p, local) if local else (None, None, 1e99, None)
        if st is None or q is None:
            st, q, d, seg_idx = _bc_chain_station_for_point(p)
        if st is None or q is None or d > 5.0:
            continue
        try:
            z = None if gp.elevation is None else float(gp.elevation)
        except Exception:
            z = None
        rows.append((i, gp, float(st), int(seg_idx or 0), z))

    if len(rows) < 3:
        return 0

    # Strong contour anchors are points whose source/confidence says a real curb-component
    # control, not a later generic cleanup.  v8.5.6: EP/FL/gutter + curb controls are
    # valid anchors too.  The old v8.5.0 cleanup treated only BC direct contours as anchors,
    # so a correct FL + 0.50 point like 6019 could be overwritten by a different local run.
    anchors = []
    for i, gp, st, seg_idx, z in rows:
        if z is None or abs(z) < 1e-9:
            continue
        txt = " ".join([(gp.source or "").upper(), (gp.confidence or "").upper(), (gp.issue or "").upper()])
        if (
            "BC_DIRECT_CONTOUR_HOLD_V850" in txt or
            "BC_CONTOUR_EXACT" in txt or
            "BC_STOP_SHORT" in txt or
            "BC_NEAR_POINT_CONTOUR_DIRECT" in txt or
            "BC_ENDPOINT_EXTENSION" in txt or
            "FL_CONTOUR_PLUS_CURB_EXACT" in txt or
            "FL_TOUCH_PLUS_CURB" in txt or
            "FL_TOUCH_INTERP_PLUS_CURB" in txt or
            "SURFACE_MODE_FL_EP_PLUS_CURB_V853" in txt
        ):
            anchors.append((st, z, i, gp, seg_idx))

    if len(anchors) < 2:
        return 0

    anchors.sort(key=lambda a: a[0])
    changed = 0

    for i, gp, st, seg_idx, old_z in rows:
        if is_hp_lp_or_manual_locked_point(gp):
            continue
        # v8.5.6: do not let the generic profile cleanup overwrite a point that
        # already came from the correct curb-component workflow:
        #   BC contour = direct
        #   EP/FL/gutter contour = + curb height
        #   chain interpolation between those controls = prorated
        # This is what caused 6019 to be flattened to 611 even though its source
        # said FL_CONTOUR_PLUS_CURB_EXACT = 616.500.
        if is_strong_curb_component_control_v854(gp):
            continue
        # Do not replace a point that is itself a strong direct contour anchor.
        if any(a[2] == i for a in anchors):
            continue

        before = [a for a in anchors if a[0] < st and same_curb_run((gp.easting,gp.northing),(a[3].easting,a[3].northing))]
        after = [a for a in anchors if a[0] > st and same_curb_run((gp.easting,gp.northing),(a[3].easting,a[3].northing))]
        if not before or not after:
            continue
        lo = before[-1]
        hi = after[0]
        span = hi[0] - lo[0]
        if span <= 1e-9 or span > max_gap:
            continue

        f = (st - lo[0]) / span
        z = lo[1] + (hi[1] - lo[1]) * f

        if old_z is None or abs(old_z - z) >= 0.15:
            gp.elevation = float(z)
            gp.source = (gp.source + "; " if gp.source else "") + (
                f"v8.5.0 CONTOUR PROFILE PRORATE: prorated by BC station between "
                f"point {lo[3].point} elev {lo[1]:.3f} and point {hi[3].point} elev {hi[1]:.3f}; "
                f"span {span:.2f} ft."
            )
            gp.confidence = (gp.confidence + "; " if gp.confidence else "") + "BC_CONTOUR_PROFILE_PRORATE_V850"
            gp.issue = (gp.issue + " " if gp.issue else "") + (
                "v8.5.0 used contour controls along the curb station instead of applying curb height or nearest XY alone."
            )
            changed += 1

    return changed




def is_direct_pdf_or_manual_hold_v851(gp: GenPoint) -> bool:
    """v8.5.1: true plan callouts and manual holds remain above surface mode."""
    txt = " ".join([(gp.desc or "").upper(), (gp.source or "").upper(), (gp.issue or "").upper(), (gp.confidence or "").upper()])
    hard = [
        "BC HP", "BC LP", "HP_TC", "LP_TC", "HIGH POINT", "LOW POINT",
        "HP/LP CALLOUT", "HP_HARD_HOLD", "LP_HARD_HOLD",
        "MANUAL", "PDF_TICK", "PDF LEADER", "PDF_SPOT", "PDF SPOT",
        "HELD EXACT", "CALLOUT HELD", "HELD_CONTROL", "HELD CONTROL", "LANDXML", "TICK", "ARROW ENDPOINT", "LEADER ENDPOINT",
    ]
    return any(tok in txt for tok in hard)


def apply_contour_surface_mode_v851(points: List[GenPoint], contour_tin: Optional[ContourTinSurface], elev_segments: List[ElevSegment], direct_tol: float = 0.75, max_replace_diff: float = 0.12, default_curb_height: float = 0.50, surface_mode: str = "auto") -> int:
    """v8.5.3 Surface Mode for curb staking.

    Survey rule:
    - Build the proposed grading surface from 3D DXF contours.
    - Sample every normal BC point from that surface.
    - PDF/manual TC/BC/HP/LP callouts hold above the surface.
    - HP/LP are break points and are never averaged through.
    - If a contour is directly at the generated BC point, hold that contour exactly.
    - Only add curb height when the selected geometry is truly FL/gutter/EP.

    This fixes the Brayfield condition where the curb stops short but the grading
    surface/contours continue to 610.  GradeGuard should act as if the curb is graded
    into that same surface, not as if the curb end breaks the contour interpolation.
    """
    if not points or contour_tin is None:
        return 0

    contour_segments = [es for es in elev_segments if is_contour_crossing_source(es)] if elev_segments else []
    changed = 0
    for gp in points:
        if is_direct_pdf_or_manual_hold_v851(gp):
            continue
        if is_strong_curb_component_control_v854(gp):
            # v8.5.5: actual BC/EP contour station logic is stronger than generic surface sampling.
            continue
        p = (gp.easting, gp.northing)

        # Strongest surface-mode case: a contour is physically at the BC point.
        direct = None
        for es in contour_segments:
            try:
                d = point_segment_distance(p, es.a, es.b)
            except Exception:
                continue
            if d > direct_tol:
                continue
            try:
                _t, q, _d2 = closest_point_t_on_segment(p, es.a, es.b)
                z = float(segment_z_at_point(q, es))
            except Exception:
                z = float(es.z)
            if not valid_grade_z(z):
                continue
            cand = (d, z, es.layer)
            if direct is None or cand < direct:
                direct = cand

        if direct is not None:
            d, z, layer = direct
            # v8.5.3: A contour that touches picked BC holds directly.
            # A contour that touches picked FL/gutter/EP is a pavement/flowline control,
            # so the BC stake elevation is contour + verified/standard curb height.
            use_plus_curb = False
            q_for_direct = None
            try:
                # Recompute the point on the contour closest to the BC point.
                best_es = None
                best_d = 1e99
                for es2 in contour_segments:
                    dd = point_segment_distance(p, es2.a, es2.b)
                    if dd < best_d:
                        best_d = dd
                        best_es = es2
                if best_es is not None:
                    _tq, q_for_direct, _dq = closest_point_t_on_segment(p, best_es.a, best_es.b)
            except Exception:
                q_for_direct = None
            try:
                if q_for_direct is not None and FLOWLINE_SEGMENTS:
                    _fls, _flq, fl_d = nearest_segment_info(q_for_direct, FLOWLINE_SEGMENTS)
                    _bcs, _bcq, bc_d = nearest_segment_info(q_for_direct, CURB_SEGMENTS)
                    # If the contour point is on/near selected FL/EP and not truly on picked BC,
                    # it controls flowline/pavement grade and must be raised to BC.
                    if fl_d <= max(0.75, direct_tol) and fl_d + 0.15 < bc_d:
                        use_plus_curb = True
            except Exception:
                pass
            mode = (surface_mode or "auto").lower()
            if mode.startswith("flow") or (mode.startswith("auto") and use_plus_curb):
                z_bc = float(z) + float(default_curb_height)
                src = f"v8.5.3 SURFACE MODE FL/EP CONTOUR PLUS CURB: {z:.3f} from {layer}, contour is {d:.2f} ft from generated BC point and matches selected FL/gutter/EP; BC = {z:.3f} + curb height {default_curb_height:.2f} = {z_bc:.3f}."
                z = z_bc
                conf = "SURFACE_MODE_FL_EP_PLUS_CURB_V853"
            else:
                src = f"v8.5.3 SURFACE MODE DIRECT BC CONTOUR HOLD: {z:.3f} from {layer}, contour is {d:.2f} ft from generated BC point. No curb height added because it is not controlling selected FL/gutter/EP."
                conf = "SURFACE_MODE_DIRECT_BC_CONTOUR_V853"
        else:
            z, src0, conf0 = contour_tin_elevation(p, contour_tin)
            if z is None or not valid_grade_z(float(z)):
                continue
            mode = (surface_mode or "auto").lower()
            if mode.startswith("flow") or mode.startswith("auto"):
                z0 = float(z)
                z = z0 + float(default_curb_height)
                src = f"v8.5.3 SURFACE MODE: sampled temporary proposed grading surface as pavement/EP/flowline surface and added curb height. {src0}; BC = {z0:.3f} + {default_curb_height:.2f} = {z:.3f}"
                conf = "SURFACE_MODE_TIN_PLUS_CURB_V853"
            else:
                src = "v8.5.3 SURFACE MODE: sampled temporary proposed grading surface built from 3D DXF contours as top/back curb. " + src0
                conf = "SURFACE_MODE_TIN_DIRECT_V853"

        try:
            old_z = None if gp.elevation is None else float(gp.elevation)
        except Exception:
            old_z = None
        if old_z is not None and abs(old_z - float(z)) < max_replace_diff:
            # Still mark that it checked against the surface, but do not clutter every point.
            continue

        gp.elevation = float(z)
        gp.source = (gp.source + "; " if gp.source else "") + src
        gp.confidence = (gp.confidence + "; " if gp.confidence else "") + conf
        gp.issue = (gp.issue + " " if gp.issue else "") + (
            "v8.5.3 corrected this normal BC point by sampling the proposed contour surface with curb-component meaning. "
            "Contours are interpolated across the grading surface first; BC station/profile logic is only a QA/fallback. "
            "HP/LP and held PDF/manual callouts remain protected."
        )
        changed += 1
    return changed


def sanitize_field_elevations(points: List[GenPoint], unknown_to_zero: bool = False, max_local_jump: float = 8.0) -> int:
    """One export decision for all tools. Keep rejected values only in review data."""
    withheld = 0
    for point in points:
        approved, reason = field_approval(point)
        point.approved = approved
        if not approved:
            if point.elevation is not None:
                point.calculated_elevation = point.elevation
            point.elevation = None
            point.confidence = (point.confidence + '; WITHHELD_REVIEW').strip('; ')
            point.issue = (point.issue + ' ' if point.issue else '') + reason + ' Point withheld from stake-ready export.'
            withheld += 1
    return withheld

def stake_ready_points(points: List[GenPoint]) -> List[GenPoint]:
    return [point for point in points if field_approval(point)[0]]


def write_csv(points: List[GenPoint], out_path: str) -> int:
    """Write field import CSV with NO header row.

    CAD/data collector import expects raw P,N,E,Z,D rows.  The detailed debug
    CSV keeps headers and sources for review.
    """
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        ready = stake_ready_points(points)
        for p in ready:
            elev = f"{float(p.elevation):.3f}"
            description = (p.desc or "BC").replace('\r', ' ').replace('\n', ' ')
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, description])
    return len(ready)


def write_do_not_stake_csv(points: List[GenPoint], out_path: str) -> int:
    """Write withheld points to a labeled office-review file, not a raw field CSV."""
    ready_ids = {id(p) for p in stake_ready_points(points)}
    withheld = [p for p in points if id(p) not in ready_ids]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["DO_NOT_STAKE", "Point", "Northing", "Easting", "Description", "Confidence", "Source", "Blocking_Reason"])
        for p in withheld:
            w.writerow(["DO_NOT_STAKE", p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", p.desc or "BC", p.confidence, p.source, p.issue])
    return len(withheld)


def write_debug_csv(points: List[GenPoint], out_path: str):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Point", "Northing", "Easting", "Elevation", "Description", "Source", "Confidence", "Issue", "Calculated_Elevation_REVIEW_ONLY"])
        for p in points:
            elev = "" if p.elevation is None else f"{p.elevation:.3f}"
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, p.desc or "BC", p.source, p.confidence, p.issue, p.calculated_elevation if p.calculated_elevation is not None else ""])



# ----------------------------
# GradeGuard v9.0 Survey Package QA
# ----------------------------

def gg_v9_status_for_point(p: GenPoint) -> Tuple[str, str]:
    approved, reason = field_approval(p)
    if not approved:
        return 'RED', reason
    if p.issue:
        return 'YELLOW', 'Approved control/calculation with an office QA note: ' + p.issue
    return 'GREEN', 'Approved source and finite coordinates/elevation.'


def write_confidence_summary_csv(points: List[GenPoint], out_path: str) -> Dict[str, int]:
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Point", "Northing", "Easting", "Elevation", "Description", "QA_Status", "QA_Reason", "Confidence", "Source", "Issue"])
        for p in points:
            status, reason = gg_v9_status_for_point(p)
            counts[status] = counts.get(status, 0) + 1
            elev = "" if p.elevation is None else f"{p.elevation:.3f}"
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, p.desc or "BC", status, reason, p.confidence, p.source, p.issue])
    return counts


def write_surface_conflict_report_csv(points: List[GenPoint], out_path: str, max_normal_jump: float = 0.35, min_distance: float = 0.25, max_distance: float = 75.0) -> Dict[str, int]:
    """Office QA report for suspicious profile jumps.

    This is intentionally separate from the staking CSV. It does not change
    elevations. It flags profile problems like the 6006/6019 type jumps.
    """
    rows = []
    good = [p for p in points if p.elevation is not None]
    for a, b, d in diagnostic_pairs(points):
        if a.elevation is None or b.elevation is None:
            continue
        pa = (float(a.easting), float(a.northing))
        pb = (float(b.easting), float(b.northing))
        if d < min_distance or d > max_distance:
            continue
        if CURB_SEGMENTS and not same_curb_run(pa,pb):
            continue
        dz = float(b.elevation) - float(a.elevation)
        desc = ((a.desc or "") + " " + (b.desc or "")).upper()
        txt = " ".join([(a.confidence or ""), (b.confidence or ""), (a.source or ""), (b.source or ""), (a.issue or ""), (b.issue or "")]).upper()
        hp_lp = ("HP" in desc or "LP" in desc or "HP_LP" in txt or "HP/LP" in txt)
        if abs(dz) > max_normal_jump and not hp_lp:
            rows.append([
                a.point, b.point,
                f"{d:.3f}", f"{float(a.elevation):.3f}", f"{float(b.elevation):.3f}", f"{dz:.3f}",
                "RED",
                f"Adjacent normal BC points change {abs(dz):.3f} ft over {d:.3f} ft without HP/LP protection. Review contours, EP/FL conversion, and selected curb run."
            ])
        elif abs(dz) > (max_normal_jump * 0.65) and not hp_lp:
            rows.append([
                a.point, b.point,
                f"{d:.3f}", f"{float(a.elevation):.3f}", f"{float(b.elevation):.3f}", f"{dz:.3f}",
                "YELLOW",
                f"Moderate grade jump. Review against grading surface and water flow."
            ])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["From_Point", "To_Point", "Distance_Ft", "From_Elev", "To_Elev", "Delta_Elev", "QA_Status", "Review_Note"])
        w.writerows(rows)

    return {"surface_conflict_rows": len(rows), "surface_conflict_red": sum(1 for r in rows if r[6] == "RED"), "surface_conflict_yellow": sum(1 for r in rows if r[6] == "YELLOW")}


def write_v9_package_manifest(out_dir: str, csv_path: str, debug_csv_path: str, report_path: str, confidence_path: str, conflict_path: str, flow_pdf_path: str, dxf_stats: Dict[str, Any], do_not_stake_path: str = "") -> str:
    manifest_path = os.path.join(out_dir, "GradeGuard_v9_Survey_Package_Manifest.txt")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write(f"Schoel GradeGuard v{APP_VERSION} Survey Package Mode\n")
        f.write("=========================================\n\n")
        f.write("Core staking rules locked in this build:\n")
        f.write("1. User-reviewed PDF controls govern; the software does not certify plan signatures or local curb detail applicability.\n")
        f.write("2. CAD/DXF gives geometry.\n")
        f.write("3. Selected LandXML faces and connected contour brackets supply approved grades; temporary contour TIN is review-only.\n")
        f.write("4. Actual contour on selected BC holds exact elevation.\n")
        f.write("5. Actual contour on selected EP/FL/gutter adds standard/PDF curb height.\n")
        f.write("6. Normal BC points between controls prorate by station along the connected curb run.\n")
        f.write("7. Explicitly located HP/LP controls hold; ambiguous or unregistered callouts require review.\n")
        f.write("8. Bad jumps, surface conflicts, drainage flat spots, and unsupported elevations are flagged for office QA.\n")
        f.write("9. No separate crew-view report is created.\n\n")
        f.write("Outputs:\n")
        for label, path in [
            ("Clean staking CSV, no header", csv_path),
            ("Source/debug CSV", debug_csv_path),
            ("HTML office QA report", report_path),
            ("Confidence summary CSV", confidence_path),
            ("DO NOT STAKE office-review CSV", do_not_stake_path),
            ("Surface conflict report CSV", conflict_path),
            ("Water flow PDF", flow_pdf_path or ""),
        ]:
            f.write(f"- {label}: {path}\n")
        f.write("\nStatistics:\n")
        for k in sorted(dxf_stats.keys()):
            if k.lower().endswith("image_base64"):
                continue
            try:
                f.write(f"- {k}: {dxf_stats[k]}\n")
            except Exception:
                pass
    return manifest_path


def _flow_pdf_arrow(c, x1, y1, x2, y2, color, width=1.0):
    """Draw a line with a small arrow head on a ReportLab canvas."""
    c.setStrokeColor(color)
    c.setFillColor(color)
    c.setLineWidth(width)
    c.line(x1, y1, x2, y2)
    ang = math.atan2(y2 - y1, x2 - x1)
    ah = 7.0
    aw = 4.0
    p1 = (x2, y2)
    p2 = (x2 - ah * math.cos(ang) + aw * math.sin(ang), y2 - ah * math.sin(ang) - aw * math.cos(ang))
    p3 = (x2 - ah * math.cos(ang) - aw * math.sin(ang), y2 - ah * math.sin(ang) + aw * math.cos(ang))
    c._code.append("n %.3f %.3f m %.3f %.3f l %.3f %.3f l h f" % (p1[0], p1[1], p2[0], p2[1], p3[0], p3[1]))


def write_water_flow_pdf(points: List[GenPoint], out_path: str, spacing: float = 25.0, min_slope: float = 0.003, title: str = "Schoel GradeGuard - BC Water Flow Map"):
    """Create a PDF map showing downhill water-flow direction along generated points."""
    try:
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import landscape, letter
        from reportlab.lib.colors import HexColor, black
    except Exception:
        return None

    good = [p for p in points if p.elevation is not None]
    if not good:
        return None

    coords = [(p.easting, p.northing) for p in good]
    for seg in CURB_SEGMENTS:
        coords.extend([seg.a, seg.b])
    if not coords:
        return None

    minx = min(x for x, y in coords); maxx = max(x for x, y in coords)
    miny = min(y for x, y in coords); maxy = max(y for x, y in coords)
    dx = max(maxx - minx, 1.0); dy = max(maxy - miny, 1.0)

    page_w, page_h = landscape(letter)
    margin = 42
    title_h = 70
    draw_w = page_w - 2 * margin
    draw_h = page_h - 2 * margin - title_h
    scale = min(draw_w / dx, draw_h / dy)
    if scale <= 0:
        scale = 1.0

    def sx(x): return margin + (x - minx) * scale
    def sy(y): return margin + (y - miny) * scale

    c = canvas.Canvas(out_path, pagesize=landscape(letter))
    logo_path = resource_path("gradeguard_logo.png")
    if os.path.exists(logo_path):
        try:
            c.drawImage(logo_path, margin, page_h - 62, width=130, height=45, preserveAspectRatio=True, mask='auto')
        except Exception:
            pass
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin + 150, page_h - 45, title)
    c.setFont("Helvetica", 8)
    c.drawString(margin + 150, page_h - 60, "Arrows point downhill from higher generated top/back-of-curb elevations toward lower elevations. Review flat and disconnected areas before staking.")

    c.setStrokeColor(HexColor("#777777")); c.setLineWidth(0.5)
    for seg in CURB_SEGMENTS:
        c.line(sx(seg.a[0]), sy(seg.a[1]), sx(seg.b[0]), sy(seg.b[1]))

    max_gap = max(float(spacing or 25.0) * 1.75, 45.0)
    arrow_count = 0
    flat_count = 0
    for a, b, d in diagnostic_pairs(points):
        if a.elevation is None or b.elevation is None:
            continue
        pa = (a.easting, a.northing); pb = (b.easting, b.northing)
        if d < 0.25 or d > max_gap:
            continue
        if CURB_SEGMENTS and not same_curb_run(pa,pb):
            continue
        dz = float(b.elevation) - float(a.elevation)
        slope = abs(dz) / d if d > 0 else 0
        ax, ay = sx(pa[0]), sy(pa[1])
        bx, by = sx(pb[0]), sy(pb[1])
        mx1 = ax * 0.65 + bx * 0.35; my1 = ay * 0.65 + by * 0.35
        mx2 = ax * 0.35 + bx * 0.65; my2 = ay * 0.35 + by * 0.65
        if slope < min_slope:
            c.setStrokeColor(HexColor("#ff9f1c")); c.setLineWidth(1.0)
            c.line(mx1, my1, mx2, my2)
            flat_count += 1
            continue
        if a.elevation > b.elevation:
            _flow_pdf_arrow(c, mx1, my1, mx2, my2, HexColor("#0077cc"), 1.4)
        else:
            _flow_pdf_arrow(c, mx2, my2, mx1, my1, HexColor("#0077cc"), 1.4)
        arrow_count += 1

    for p in good:
        x, y = sx(p.easting), sy(p.northing)
        desc = (p.desc or "BC").upper()
        if "HP" in desc:
            color = HexColor("#d62728"); rad = 2.7
        elif "LP" in desc:
            color = HexColor("#2ca02c"); rad = 2.7
        else:
            color = black; rad = 1.6
        c.setFillColor(color); c.setStrokeColor(color)
        c.circle(x, y, rad, stroke=1, fill=1)
        if "HP" in desc or "LP" in desc or p.point % 5 == 0:
            c.setFillColor(black); c.setFont("Helvetica", 6)
            c.drawString(x + 3, y + 3, f"{p.point} {p.elevation:.2f} {desc}")

    lx = margin; ly = margin - 22
    c.setFont("Helvetica", 8); c.setFillColor(black)
    c.drawString(lx, ly, "Legend: blue arrows = downhill flow, orange = flat/no-flow review, red = HP/high side, green = LP/low side.")
    c.drawString(lx, ly - 12, f"Flow arrows: {arrow_count}   Flat/no-flow review segments: {flat_count}   Generated points: {len(points)}")
    c.showPage()
    c.save()
    return out_path

def write_report(points: List[GenPoint], layers: List[str], pdf_scan: Dict[str, Any], dxf_stats: Dict[str, Any], out_path: str):
    issues = [p for p in points if p.issue]
    assigned = [p for p in points if p.elevation is not None]
    missing = [p for p in points if p.elevation is None]
    high_conf = [p for p in points if p.confidence == "HIGH"]
    med_conf = [p for p in points if p.confidence == "MEDIUM"]
    logo_html = ""
    logo_path = resource_path("gradeguard_logo.png")
    if os.path.exists(logo_path):
        try:
            with open(logo_path, "rb") as lf:
                logo_b64 = base64.b64encode(lf.read()).decode("ascii")
            logo_html = f"<img src='data:image/png;base64,{logo_b64}' style='max-width:360px;height:auto;display:block;margin-bottom:10px;'>"
        except Exception:
            logo_html = ""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("<html><head><title>Schoel GradeGuard Generate Report</title>")
        f.write("<style>body{font-family:Arial;margin:30px} table{border-collapse:collapse;width:100%} td,th{border:1px solid #ccc;padding:6px} .bad{color:#b00020;font-weight:bold} .warn{color:#b36b00;font-weight:bold} .ok{color:#006b2e;font-weight:bold} .small{font-size:12px;color:#555}</style></head><body>")
        f.write(logo_html)
        f.write(f"<h1>Schoel GradeGuard v{APP_VERSION} Generate Report</h1>")
        f.write("<h2>Generated BC Points</h2>")
        f.write(f"<p>Total generated points: <b>{len(points)}</b></p>")
        f.write(f"<p>Points with elevations: <span class='ok'>{len(assigned)}</span></p>")
        f.write(f"<p>Missing elevations: <span class='bad'>{len(missing)}</span></p>")
        f.write(f"<p>High confidence elevations: <b>{len(high_conf)}</b></p>")
        f.write(f"<p>Contour calculated elevations needing review: <span class='warn'>{len(med_conf)}</span></p>")
        f.write(f"<p>Layers used for curb geometry: {', '.join(layers) if layers else 'None'}</p>")
        f.write("<h2>Curb Layer Detection</h2>")
        f.write("<p>High confidence BC/back-of-curb layers are used automatically. Generic curb layers are listed for review and are not used unless selected by keyword.</p>")
        f.write("<table><tr><th>Use</th><th>Layer</th><th>Confidence</th><th>Assumed Meaning</th></tr>")
        for c in dxf_stats.get("curb_layer_candidates", []):
            f.write(f"<tr><td>{c.get('use','')}</td><td>{c.get('layer','')}</td><td>{c.get('confidence','')}</td><td>{c.get('meaning','')}</td></tr>")
        f.write("</table>")
        f.write("<h2>Elevation Sources</h2>")
        f.write(f"<p>PDF C-5.0 spot labels found: <b>{dxf_stats.get('pdf_spot_labels',0)}</b></p>")
        f.write(f"<p>CAD grade labels found: {dxf_stats.get('cad_grade_labels',0)}</p>")
        f.write(f"<p>DXF contour/elevation segments found: {dxf_stats.get('elevation_segments',0)}</p>")
        f.write(f"<p>Extra BC points added at PC/PT/geometry controls: <b>{dxf_stats.get('pc_pt_geometry_points_added',0)}</b></p>")
        f.write(f"<p>Extra BC points added where contours cross selected BC: <b>{dxf_stats.get('contour_crossing_bc_points_added',0)}</b></p>")
        f.write(f"<p>Extra BC points added where contours cross selected FL/gutter and project to BC: <b>{dxf_stats.get('flowline_contour_crossing_bc_points_added',0)}</b></p>")
        f.write(f"<p>Extra BC HP points added from HP/TC callouts: <b>{dxf_stats.get('hp_callout_points_added',0)}</b></p>")
        f.write(f"<p>Extra BC LP points added from LP/TC callouts: <b>{dxf_stats.get('lp_callout_points_added',0)}</b></p>")
        if dxf_stats.get("water_flow_pdf"):
            f.write(f"<p><b>Water flow PDF created:</b> {dxf_stats.get('water_flow_pdf','')}</p>")
        if dxf_stats.get("offset_enabled"):
            f.write(f"<p><b>Offset package created:</b> {dxf_stats.get('offset_points_created',0)} offset points at {dxf_stats.get('offset_distance_ft',0):.2f} ft. Offset CSV: {dxf_stats.get('offset_csv','')}. Offset line DXF: {dxf_stats.get('offset_lines_dxf','')}.</p>")
        f.write("<p><b>Calculation order:</b> v9.0 Survey Package Mode: PDF callouts and HP/LP controls hold first. CAD/DXF supplies geometry. Proposed 3D contours build the temporary grading surface. Actual contour crossing selected BC holds exact elevation. Actual contour crossing selected EP/FL/gutter is converted to BC by adding the standard/PDF verified curb height. Normal BC points between clean controls are prorated by station along the connected curb run. HP and LP are hard vertical controls and are never averaged through. Surface conflicts, bad jumps, flat/no-flow areas, and unsupported values are flagged for office QA. No separate crew-view report is created.</p>")
        f.write("<pre>")
        f.write(json.dumps(dxf_stats.get("elevation_layers", {}), indent=2))
        f.write("</pre>")
        f.write("<h2>Review Items</h2>")
        f.write(f"<p class='bad'>Points needing review: {len(issues)}</p>")
        f.write("<table><tr><th>Point</th><th>N</th><th>E</th><th>Elev</th><th>Confidence</th><th>Issue</th><th>Source</th></tr>")
        for p in issues[:800]:
            elev = "WITHHELD" if p.elevation is None else f"{p.elevation:.3f}"
            f.write(f"<tr><td>{p.point}</td><td>{p.northing:.3f}</td><td>{p.easting:.3f}</td><td>{elev}</td><td>{p.confidence}</td><td>{p.issue}</td><td>{p.source}</td></tr>")
        f.write("</table>")
        f.write("<h2>First 100 Generated Points</h2>")
        f.write("<table><tr><th>Point</th><th>N</th><th>E</th><th>Elev</th><th>Source</th></tr>")
        for p in points[:100]:
            elev = "WITHHELD" if p.elevation is None else f"{p.elevation:.3f}"
            f.write(f"<tr><td>{p.point}</td><td>{p.northing:.3f}</td><td>{p.easting:.3f}</td><td>{elev}</td><td>{p.source}</td></tr>")
        f.write("</table>")
        f.write("<h2>PDF Scan</h2>")
        f.write("<p><b>PDF rule:</b> The certified civil PDF plan set is required for BC generation. PDF callouts and details govern over CAD/DXF. TC/BC/HP/LP callouts are held at the leader or arrow endpoint where they intersect the curb, not at the text box. PDF curb details are scanned before any FL/gutter contour is raised to BC. Standard curb is +0.50 unless the certified PDF detail specifically supports catch +0.42, spill +0.58, or a custom height.</p>")
        f.write("<pre>")
        f.write(json.dumps(pdf_scan, indent=2))
        f.write("</pre>")
        f.write("</body></html>")



# -----------------------------
# v1.1 manual PDF alignment and spot anchor helpers
# -----------------------------

def similarity_transform_from_two_points(p1, p2, q1, q2):
    """Return coefficients that map PDF page x,y to CAD easting,northing with scale plus rotation.
    p coordinates are PDF page points, q coordinates are CAD feet.
    """
    x1, y1 = p1; x2, y2 = p2
    u1, v1 = q1; u2, v2 = q2
    dx, dy = x2 - x1, y2 - y1
    du, dv = u2 - u1, v2 - v1
    den = dx*dx + dy*dy
    if den <= 1e-9:
        raise ValueError("Alignment points are too close together.")
    a = (du*dx + dv*dy) / den
    b = (dv*dx - du*dy) / den
    # u = a*x - b*y + tx ; v = b*x + a*y + ty
    tx = u1 - a*x1 + b*y1
    ty = v1 - b*x1 - a*y1
    ce = [a, -b, tx]
    cn = [b, a, ty]
    return (ce, cn)


def affine_transform_from_points(alignment_points):
    """Fit similarity/reflected-similarity models without altering CAD coordinates."""
    import numpy as np
    clean = []
    for point in alignment_points or []:
        try:
            values = (*map(float,point['pdf']), *map(float,point['cad']))
            if len(values) != 4 or not all(math.isfinite(v) for v in values):
                raise ValueError()
            clean.append(values)
        except (ValueError,KeyError,TypeError):
            return None, {'warning':'Invalid/nonfinite alignment control.', 'points':len(clean)}
    if len(clean) < 2 or len({v[:2] for v in clean}) < 2:
        return None, {'warning':'Need distinct alignment controls.', 'points':len(clean)}
    # Center before solving to avoid loss of significance in State Plane coordinates.
    X = np.array([v[:2] for v in clean]); Y = np.array([v[2:] for v in clean])
    xm = X.mean(axis=0); ym = Y.mean(axis=0)
    centered = X-xm; target = Y-ym
    candidates = []
    for reflect in (False,True):
        rows=[]; values=[]
        for (x,y),(e,n) in zip(centered,target):
            rows.extend([[x,y],[-y,x]] if reflect else [[x,-y],[y,x]])
            values.extend([e,n])
        sol,_res,rank,_singular = np.linalg.lstsq(np.array(rows),np.array(values),rcond=None)
        if rank < 2:
            continue
        a,b=map(float,sol)
        if math.hypot(a,b) <= 1e-12:
            continue
        ce=[a,b if reflect else -b,0.0]
        cn=[b,-a if reflect else a,0.0]
        ce[2]=float(ym[0]-ce[0]*xm[0]-ce[1]*xm[1])
        cn[2]=float(ym[1]-cn[0]*xm[0]-cn[1]*xm[1])
        errs=[dist(transform_pdf_to_cad((ce,cn),(x,y)),(e,n)) for x,y,e,n in clean]
        rmse=math.sqrt(sum(v*v for v in errs)/len(errs))
        candidates.append((rmse,(ce,cn),reflect,math.hypot(a,b)))
    if not candidates:
        return None, {'warning':'Alignment is degenerate.', 'points':len(clean)}
    rmse,model,reflected,scale=min(candidates,key=lambda v:v[0])
    warning = 'Alignment RMSE exceeds 1 foot; verify controls.' if rmse > 1 else ''
    if np.linalg.matrix_rank(centered) < 2:
        warning = 'Collinear/two-point alignment cannot establish reflection; add a third noncollinear control.'
    return model, {'points':len(clean),'method':'Centered similarity transform','rmse_ft':rmse,
                   'pdf_scale_ft_per_point':scale,'reflected_pdf_y':reflected,'warning':warning}


def transform_pdf_to_cad(model, pdf_xy):
    if model is None:
        return None
    ce, cn = model
    x, y = pdf_xy
    return (float(ce[0]*x + ce[1]*y + ce[2]), float(cn[0]*x + cn[1]*y + cn[2]))


def load_manual_anchor_file(path: str, cad_path: str = '', pdf_path: str = '') -> Tuple[List[GradeLabel], Dict[str, Any]]:
    info = {"manual_anchor_file": path or "", "alignment_points": 0, "manual_spot_anchors": 0, "alignment": {}}
    labels: List[GradeLabel] = []
    if not path or not os.path.exists(path):
        return labels, info
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception as e:
        raise ValueError(f"Could not read manual anchor file: {e}") from e
    validate_saved_drawing(data, cad_path)
    if data.get('schema') and data.get('schema') != 'gradeguard.pdf-controls.v1':
        raise ValueError('Unsupported PDF control schema. Review and save the controls again.')
    reviewed_pdf = data.get('schema') == 'gradeguard.pdf-controls.v1'
    if reviewed_pdf:
        from gradeguard_pdf import validate_reviewed_data
        validate_reviewed_data(data, pdf_path, cad_path)
    align_pts = data.get("alignment_points", []) or []
    spot_pts = data.get("manual_spots", []) or []
    info["alignment_points"] = len(align_pts)
    info["manual_spot_anchors"] = len(spot_pts)
    model, minfo = affine_transform_from_points(align_pts)
    info["alignment"] = minfo
    for s in spot_pts:
        try:
            kind = str(s.get("kind", "TC")).upper().replace(" ", "_")
            elev = float(s.get("elev"))
            # Multi-job support: do not reject valid project elevations outside one job's range.
            if not valid_grade_z(elev):
                continue
            if "cad" in s:
                e, n = float(s["cad"][0]), float(s["cad"][1])
            elif "pdf" in s and model is not None:
                if minfo.get('warning'):
                    raise ValueError("Manual PDF anchor alignment requires review: " + minfo['warning'])
                e, n = transform_pdf_to_cad(model, tuple(s["pdf"]))
            else:
                continue
            raw = s.get("raw") or f"Manual {kind} {elev:.3f}"
            page = s.get("page", "")
            if not (math.isfinite(e) and math.isfinite(n)):
                raise ValueError('CAD coordinates must be finite.')
            source = f"MANUAL PDF tick anchor page {page}"
            if reviewed_pdf:
                source = (f"MANUAL REVIEWED PDF page {page}; {s['meaning']}; printed {s['raw_elevation']}; "
                          f"local rise {s['curb_rise']}; {s['role']}; {s['reference']}")
            labels.append(GradeLabel(e, n, kind, elev, raw, source))
        except Exception as ex:
            raise ValueError(f"Invalid manual tick anchor: {ex}") from ex
    return labels, info


def validate_reviewed_controls_on_curb(labels, segments):
    """The keyed position is the actual BC tick; never pull a remote label to BC."""
    for label in labels:
        if not label.source.startswith('MANUAL REVIEWED PDF'):
            continue
        distance = min((closest_point_t_on_segment((label.x, label.y), seg.a, seg.b)[2]
                        for seg in segments), default=float('inf'))
        if distance > 0.35:
            raise ValueError(f'Reviewed PDF control {label.raw!r} is {distance:.3f} ft from selected BC. '
                             'Verify the actual CAD curb tick and select its curb geometry; no automatic relocation is allowed.')


def save_manual_anchor_file(path: str, data: Dict[str, Any], cad_path: str = ''):
    data = dict(data)
    if cad_path:
        data.update(cad_path=cad_path, dxf_sha256=drawing_fingerprint(cad_path))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def run_generation(cad_path: str, pdf_path: str, out_dir: str, start_pt: int, spacing: float, default_curb_height: float, curb_layer_keywords: str, elev_layer_keywords: str, label_radius: float, contour_radius: float, min_slope: float, add_curb_height_to_contours: bool=False, contour_check_tolerance: float=0.15, manual_anchor_path: str="", require_manual_pdf_spots: bool=True, surface_path: str="", surface_mode: str="bc", selected_curb_path: str="", selected_flowline_path: str="", create_offset: bool=False, offset_distance: float=3.0, offset_mode: str="away_from_flowline", offset_reverse: bool=False, source_policy: str="surface", surface_name: str="", units_confirmed: bool=False):
    reset_generation_state()
    validate_generation_inputs(cad_path, spacing, units_confirmed)
    validate_numeric_settings(curb_height=default_curb_height, label_radius=label_radius, contour_radius=contour_radius,
                              minimum_slope=min_slope, contour_tolerance=contour_check_tolerance, offset_distance=offset_distance)
    if source_policy not in ('surface', 'contours', 'dxf_surface') or surface_mode not in ('auto', 'bc', 'flowline'):
        raise ValueError('Choose a supported elevation source and surface interpretation.')
    out_dir = prepare_output_directory(out_dir)
    ext = os.path.splitext(cad_path)[1].lower()
    if ext == ".dwg":
        raise RuntimeError("DWG files need to be converted to DXF first. Export from Civil 3D or use ODA File Converter, then load the DXF.")
    if ext != ".dxf":
        raise RuntimeError("Generate Mode currently reads DXF. Save the civil CAD as DXF first.")

    curb_keywords = [x.strip() for x in re.split(r"[,;]", curb_layer_keywords) if x.strip()]
    elev_keywords = [x.strip() for x in re.split(r"[,;]", elev_layer_keywords) if x.strip()]
    if not pdf_path or not os.path.exists(pdf_path) or not str(pdf_path).lower().endswith(".pdf"):
        raise RuntimeError("Civil PDF plan set is required. GradeGuard will not calculate BC elevations without the certified PDF plans because PDF callouts/details hold over CAD/DXF.")
    selected_handles = load_selected_curb_handles(selected_curb_path, cad_path)
    selected_fl_handles = load_selected_curb_handles(selected_flowline_path, cad_path)
    global FLOWLINE_SEGMENTS, CONTOUR_POINT_OVERRIDES
    FLOWLINE_SEGMENTS = selected_handles_to_segments(cad_path, selected_fl_handles, "Selected Flowline/Gutter")
    CONTOUR_POINT_OVERRIDES = {}
    raw_points, layers, cad_labels, segments, dxf_stats = load_dxf_geometry_and_elevations(cad_path, curb_keywords, elev_keywords, spacing, selected_handles)
    # v8.3: clean the DXF elevation evidence for curb staking.  This removes
    # zero-Z placeholder contour fragments, far-away contour geometry, and extreme
    # project outliers before contour crossings, TIN, or interpolation can use them.
    segments, elev_clean_stats = sanitize_dxf_elevation_segments_for_bc(segments, max_dist_to_curb=max(175.0, spacing * 8.0))
    dxf_stats["bc_elevation_segment_safety_filter"] = elev_clean_stats
    dxf_stats["selected_curb_file"] = selected_curb_path
    dxf_stats["selected_curb_handles_loaded"] = len(selected_handles)
    dxf_stats["selected_flowline_file"] = selected_flowline_path
    dxf_stats["selected_flowline_handles_loaded"] = len(selected_fl_handles)
    dxf_stats["selected_flowline_segments"] = len(FLOWLINE_SEGMENTS)
    pdf_scan = scan_pdf_for_curb_terms(pdf_path)
    global CURRENT_CURB_HEIGHT, CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF
    # v8.4: the GUI forces the user to confirm catch/spill/custom curb detail from the certified PDF.
    # The PDF scan is still recorded for QA, but the confirmed checklist selection controls the conversion.
    verified_height, height_verified, height_note = determine_verified_curb_height_from_pdf(pdf_scan, float(default_curb_height))
    if abs(float(default_curb_height) - 0.50) < 1e-6:
        verified_height, height_verified, height_note = 0.50, True, "v8.4.5 standard curb selected. EP/FL/gutter controls use +0.50 ft unless the certified PDF detail specifically says otherwise."
    elif abs(float(default_curb_height) - 0.42) < 1e-6:
        verified_height, height_verified, height_note = 0.42, True, "v8.4.5 catch curb selected from PDF detail. EP/FL/gutter controls use +0.42 ft."
    elif abs(float(default_curb_height) - 0.58) < 1e-6:
        verified_height, height_verified, height_note = 0.58, True, "v8.4.5 spill curb selected from PDF detail. EP/FL/gutter controls use +0.58 ft."
    elif 0.10 <= float(default_curb_height) <= 1.50:
        verified_height, height_verified, height_note = float(default_curb_height), True, f"v8.4.5 custom curb height {float(default_curb_height):.3f} ft selected from the certified PDF detail."
    CURRENT_CURB_HEIGHT = float(verified_height)
    CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF = bool(height_verified)
    dxf_stats["pdf_required"] = True
    dxf_stats["pdf_plan_set"] = pdf_path
    dxf_stats["pdf_verified_curb_height_ft"] = float(verified_height)
    dxf_stats["pdf_curb_height_verified"] = bool(height_verified)
    dxf_stats["pdf_curb_height_note"] = height_note
    dxf_stats["v8_4_4_auto_qa_rule"] = "Automatic QA/QC active: final signed civil PDF is required; PDF controls over CAD/DXF; PDF callouts use leader/arrow/tick endpoint when readable, not the text box; standard curb +0.50 unless the certified PDF detail specifically supports catch +0.42, spill +0.58, or custom height; HP/LP points are protected; unsupported elevations are withheld for review."
    raw_points, pcpt_added = add_curb_geometry_control_points(raw_points, tolerance=0.05)
    dxf_stats["pc_pt_geometry_points_added"] = pcpt_added
    raw_points, contour_crossings_added, fl_crossings_added = add_contour_crossing_bc_points(raw_points, segments, tolerance=0.35)
    dxf_stats["contour_crossing_bc_points_added"] = contour_crossings_added
    dxf_stats["flowline_contour_crossing_bc_points_added"] = fl_crossings_added
    landxml_surface = safe_load_landxml_surface(surface_path, surface_name)
    dxf_stats["landxml_surface"] = {"path": surface_path if landxml_surface else "", "triangles": len(landxml_surface.triangles) if landxml_surface else 0, "feature_segments": len(getattr(landxml_surface, "segments", []) or []) if landxml_surface else 0}
    # For 2D curb projects with no LandXML, build a temporary TIN from 3D contours so
    # curb elevations can be prorated between contours instead of reading the nearest contour.
    contour_tin_surface = build_contour_tin_surface(segments)
    dxf_stats["temporary_contour_tin"] = {"created": bool(contour_tin_surface), "points": int(len(contour_tin_surface.points)) if contour_tin_surface is not None else 0}
    manual_labels, manual_info = load_manual_anchor_file(manual_anchor_path, cad_path, pdf_path)
    validate_reviewed_controls_on_curb(manual_labels, CURB_SEGMENTS)
    pdf_scan["manual_pdf_tick_anchors"] = manual_info

    pdf_labels = []
    pdf_auto_labels = []
    pdf_hp_lp_labels = []
    pdf_spot_info = {"disabled": bool(require_manual_pdf_spots), "reason": "PDF plan sheets are read for curb terms, detail pages, and HP/LP/TC/FL values. PDF text boxes are not blindly used for general staking elevations unless manually held, because the tick/leader point controls. HP/LP labels may be projected to selected BC as review/hard hold points when they are close enough to the selected curb geometry."}
    if pdf_path:
        try:
            cad_ref_pts = list(raw_points)
            try:
                for _seg in segments:
                    cad_ref_pts.append(_seg.a); cad_ref_pts.append(_seg.b)
                for _seg in CURB_SEGMENTS:
                    cad_ref_pts.append(_seg.a); cad_ref_pts.append(_seg.b)
            except Exception:
                pass
            pdf_auto_labels, pdf_spot_info_auto = extract_pdf_spot_labels(pdf_path, default_curb_height, cad_ref_pts)
            pdf_scan["pdf_grade_labels_found"] = pdf_spot_info_auto.get("pdf_spot_labels", 0)
            pdf_scan["pdf_grade_observations"] = pdf_spot_info_auto
            pdf_spot_info = pdf_spot_info_auto
            hp_lp_candidates = [l for l in pdf_auto_labels if l.kind in ("HP", "LP", "HP_TC", "LP_TC")]
            ambiguous_boc_labels = [l for l in pdf_auto_labels if l.kind in ("HP_BOC_AMBIG", "LP_BOC_AMBIG")]
            bottom_curb_labels = [l for l in pdf_auto_labels if l.kind in ("HP_BOTTOM_CURB", "LP_BOTTOM_CURB")]
            alignment = (pdf_spot_info_auto or {}).get("alignment", {}) or {}
            # Exact automatic HP/LP holds require both a controlled PDF-to-CAD
            # alignment and a vector leader/tick endpoint. Approximate extents
            # matching and text-box centers are detection/review evidence only.
            pdf_hp_lp_labels = [
                l for l in hp_lp_candidates
                if pdf_hp_lp_hard_hold_is_trusted(l, alignment)
            ]
            pdf_scan["pdf_hp_lp_labels_found"] = pdf_spot_info_auto.get("hp_lp_observations", 0)
            pdf_scan["pdf_hp_lp_labels_accepted_for_hard_hold"] = len(pdf_hp_lp_labels)
            pdf_scan["pdf_hp_lp_labels_review_only"] = pdf_spot_info_auto.get("hp_lp_observations", 0)
            pdf_scan["pdf_boc_ambiguous_labels_review_only"] = len(ambiguous_boc_labels)
            pdf_scan["pdf_bottom_of_curb_labels_review_only"] = len(bottom_curb_labels)
            pdf_scan["pdf_boc_rule"] = (
                "BOC is ambiguous between BACK OF CURB and BOTTOM OF CURB. Bare BOC callouts are never used automatically; "
                "confirm the engineer's legend/standards and create a manual tick anchor with the correct elevation meaning."
            )
            pdf_scan["pdf_bottom_of_curb_rule"] = (
                "Explicit BOTTOM OF CURB values are detected but remain review-only. Verify the applicable curb detail/height and manually anchor the intended staking elevation."
            )
            pdf_scan["pdf_hp_lp_hard_hold_gate"] = (
                "All PDF detections are review-only. Read PDF grades requires a verified legend/detail, "
                "CAD curb tick, units/reference and local conversion before saving a held control."
            )
            if not require_manual_pdf_spots:
                pdf_labels = [l for l in pdf_auto_labels if l.kind in ('TC','BC','FL','FG','HP','LP','HP_TC','LP_TC') and pdf_hp_lp_hard_hold_is_trusted(l, alignment)]
                pdf_spot_info = pdf_spot_info_auto
        except Exception as _e:
            pdf_scan["pdf_grade_label_error"] = str(_e)
    pdf_scan["pdf_spot_alignment"] = pdf_spot_info

    # v4.5 primary order: manual tick anchors first, HP/LP callout anchors, optional PDF text, CAD spot text, then contours/surface.
    primary_labels = manual_labels + pdf_labels + cad_labels
    # Add missing HP/LP staking points from CAD and PDF callouts by projecting them to the selected BC geometry.
    hp_lp_source_labels = cad_labels + manual_labels + pdf_hp_lp_labels
    raw_points, projected_hp_lp_labels, hp_added, lp_added = add_high_low_callout_points(raw_points, hp_lp_source_labels, tolerance=0.35, max_label_to_curb_dist=max(label_radius * 3.0, 140.0))
    # v7.4: final horizontal QA before elevations.  If the selected BC is made of
    # exploded parallel hardscape arcs, force generated station points back to the
    # saved BC chain so curve MID points cannot plot beside the curb.
    raw_points, snap_count, snap_max = snap_points_to_selected_curb_geometry(raw_points, max_snap=3.0)
    dxf_stats["bc_points_snapped_to_selected_geometry"] = snap_count
    dxf_stats["max_bc_snap_distance_ft"] = round(float(snap_max), 4)
    primary_labels = projected_hp_lp_labels + primary_labels
    dxf_stats["hp_callout_points_added"] = hp_added
    dxf_stats["lp_callout_points_added"] = lp_added
    dxf_stats["projected_hp_lp_callout_labels"] = len(projected_hp_lp_labels)
    dxf_stats["manual_pdf_tick_anchors"] = len(manual_labels)
    dxf_stats["pdf_spot_labels"] = len(pdf_labels)
    dxf_stats["cad_grade_labels"] = len(cad_labels)
    dxf_stats["calculation_order"] = [
        "User-reviewed PDF tick controls hold first; automatic text-center/extents alignment is review-only.",
        "Exact selected BC crossings hold directly; selected FL crossings add the entered curb height.",
        "Preferred LandXML surface is sampled inside its selected faces, using explicit BC/FL meaning.",
        "Connected BC/FL contour brackets supply station interpolation; disconnected runs do not share anchors.",
        "Unconstrained contour TIN, unbracketed contours, ambiguous BOC and outside-surface estimates require review.",
        "Held grades are protected. One shared export gate withholds unresolved points from every field CSV."
    ]
    dxf_stats["source_policy"] = source_policy
    dxf_stats["surface_name"] = surface_name
    dxf_stats["coordinate_units"] = "US survey feet; no coordinate/datum transformation"
    dxf_stats["input_dxf_sha256"] = drawing_fingerprint(cad_path)
    elevs = assign_elevations(raw_points, primary_labels, segments, verified_height, label_radius, contour_radius, False, contour_check_tolerance, landxml_surface=landxml_surface, surface_mode=surface_mode, contour_tin=contour_tin_surface, source_policy=source_policy)

    gen: List[GenPoint] = []
    pt = int(start_pt)
    for (x, y), (elev, src, issue, conf) in zip(raw_points, elevs):
        gen.append(GenPoint(point=pt, northing=y, easting=x, elevation=elev, source=src, issue=issue, confidence=conf))
        pt += 1
    apply_pc_pt_descriptions(gen, base_desc='BC')
    for point in gen:
        point.locked = held_result(point.confidence, point.source)
    dxf_stats["hp_lp_hard_locked"] = hard_lock_hp_lp_callout_elevations(gen, primary_labels, tolerance=0.35)
    add_flow_warnings(gen, min_slope=min_slope)
    tag_high_low_points(gen, max_gap=max(spacing*2.25,55.0), min_change=0.02)
    normalize_high_low_descriptions(gen)
    dxf_stats["field_points_withheld_for_review"] = sanitize_field_elevations(gen)
    dxf_stats["calculation_policy"] = "9.3.1 single assignment; typed confidence and shared export approval; no automatic contour smoothing or grade replacement."

    csv_path = os.path.join(out_dir, "Generated_BC_Points.csv")
    debug_csv_path = os.path.join(out_dir, "Generated_BC_Points_with_sources.csv")
    report_path = os.path.join(out_dir, "GradeGuard_Generate_Report.html")
    confidence_path = os.path.join(out_dir, "GradeGuard_Confidence_Summary.csv")
    conflict_path = os.path.join(out_dir, "GradeGuard_Surface_Conflict_Report.csv")
    do_not_stake_path = os.path.join(out_dir, "GradeGuard_DO_NOT_STAKE.csv")
    stake_ready_count = write_csv(gen, csv_path)
    do_not_stake_count = write_do_not_stake_csv(gen, do_not_stake_path)
    dxf_stats["stake_ready_points_exported"] = stake_ready_count
    dxf_stats["do_not_stake_points_withheld"] = do_not_stake_count
    write_debug_csv(gen, debug_csv_path)
    confidence_counts = write_confidence_summary_csv(gen, confidence_path)
    conflict_counts = write_surface_conflict_report_csv(gen, conflict_path, max_normal_jump=0.35)
    dxf_stats["v9_0_confidence_green"] = confidence_counts.get("GREEN", 0)
    dxf_stats["v9_0_confidence_yellow"] = confidence_counts.get("YELLOW", 0)
    dxf_stats["v9_0_confidence_red"] = confidence_counts.get("RED", 0)
    dxf_stats["v9_0_surface_conflict_rows"] = conflict_counts.get("surface_conflict_rows", 0)
    dxf_stats["v9_0_surface_conflict_red"] = conflict_counts.get("surface_conflict_red", 0)
    dxf_stats["v9_0_surface_conflict_yellow"] = conflict_counts.get("surface_conflict_yellow", 0)
    dxf_stats["v9_0_confidence_summary"] = confidence_path
    dxf_stats["v9_0_surface_conflict_report"] = conflict_path
    dxf_stats["v9_0_rule"] = "Survey Package Mode: PDF controls; DXF gives geometry; contours build surface; BC contour holds; EP/FL/gutter contour adds curb height; normal BC points prorate by station; HP/LP always hold; bad jumps are flagged, not hidden."
    flow_pdf_path = os.path.join(out_dir, "GradeGuard_BC_Water_Flow_Map.pdf")
    created_flow_pdf = write_water_flow_pdf(gen, flow_pdf_path, spacing=spacing, min_slope=min_slope)
    dxf_stats["water_flow_pdf"] = created_flow_pdf or ""

    dxf_stats["offset_enabled"] = bool(create_offset)
    dxf_stats["offset_distance_ft"] = float(offset_distance or 0.0)
    dxf_stats["offset_mode"] = offset_mode
    if create_offset:
        offset_pts, offset_pairs = make_offset_points(gen, float(offset_distance), offset_mode, offset_reverse, start_point=(max(p.point for p in gen) + 1 if gen else start_pt))
        offset_csv_path = os.path.join(out_dir, "Generated_BC_Offset_Points.csv")
        offset_debug_path = os.path.join(out_dir, "Generated_BC_Offset_Points_with_sources.csv")
        offset_dxf_path = os.path.join(out_dir, "GradeGuard_BC_Offset_Lines.dxf")
        write_csv(offset_pts, offset_csv_path)
        write_debug_csv(offset_pts, offset_debug_path)
        write_offset_lines_dxf(offset_pairs, offset_dxf_path, float(offset_distance), offset_mode, offset_reverse)
        dxf_stats["offset_points_created"] = len(offset_pts)
        dxf_stats["offset_csv"] = offset_csv_path
        dxf_stats["offset_lines_dxf"] = offset_dxf_path
    else:
        dxf_stats["offset_points_created"] = 0

    manifest_path = write_v9_package_manifest(out_dir, csv_path, debug_csv_path, report_path, confidence_path, conflict_path, created_flow_pdf or "", dxf_stats, do_not_stake_path)
    dxf_stats["v9_0_package_manifest"] = manifest_path
    write_report(gen, layers, pdf_scan, dxf_stats, report_path)
    from gradeguard_review import write_cad_review
    write_cad_review(gen, out_dir, 'CURB', CURB_SEGMENTS, cad_path, pdf_path, surface_path,
                     landxml_surface, anchor_path=manual_anchor_path,
                     settings={'curb_rise': default_curb_height, 'surface_interpretation': surface_mode},
                     extra_points=offset_pts if create_offset else [])
    return csv_path, debug_csv_path, report_path, len(gen), len([p for p in gen if p.issue]), stake_ready_count





STRUCTURE_RE = re.compile(r"\b(?:STMH|STM|SSMH|SMH|SANMH|AI|CB|CI|DI|YI|GI|JB|JBOX|MH|CO|INLET|AREA\s+INLET|CURB\s+INLET|SAN\s*MH|STORM\s*MH|MANHOLE)[\s\-#]*\d+[A-Z]?\b", re.IGNORECASE)
PIPE_RE = re.compile(r"\b(\d{1,2})\s*(?:\"|IN|IN\.|INCH|INCHES)\s*(?:RCP|PVC|HDPE|DIP|DI|CMP|SDR|PIPE|SAN|STM|STORM|SS)?\b", re.IGNORECASE)
INV_RE = re.compile(r"\b(?:I\.?\s*E\.?|IE|INV(?:ERT)?|F\.?\s*L\.?|FL|FLOW\s*LINE|FLOWLINE)\s*(?:IN|OUT|O\/S|O\.S\.|O\/?|OUTLET)?\s*(?:=|:|-)??\s*([0-9]{2,4}\.[0-9]{1,3})\b", re.IGNORECASE)
OUT_INV_RE = re.compile(r"\b(?:I\.?\s*E\.?|IE|INV(?:ERT)?|F\.?\s*L\.?|FL|FLOW\s*LINE|FLOWLINE)\s*(?:OUT|O\/S|O\.S\.|O\/|OUTLET)\s*(?:=|:|-)??\s*([0-9]{2,4}\.[0-9]{1,3})\b|\b(?:OUT|OUTLET)\s*(?:INV(?:ERT)?|I\.?\s*E\.?|IE|F\.?\s*L\.?|FL)\s*(?:=|:|-)??\s*([0-9]{2,4}\.[0-9]{1,3})\b", re.IGNORECASE)
RIM_RE = re.compile(r"\b(?:RIM|GRATE|TG|TOP)\s*(?:=|:)?\s*([0-9]{2,4}\.[0-9]{1,3})\b", re.IGNORECASE)

def normalize_structure_label(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"\s+", " ", raw.upper().replace("#", "")).strip()
    # Normalize common spaced labels such as AREA INLET 1.
    s = s.replace("AREA INLET", "AI").replace("CURB INLET", "CI")
    s = re.sub(r"\s*[- ]\s*", "-", s)
    return s

def parse_structure_label(raw: str) -> str:
    if not raw:
        return ""
    m = STRUCTURE_RE.search(raw)
    return normalize_structure_label(m.group(0)) if m else ""

def parse_pipe_size(raw: str) -> str:
    if not raw:
        return ""
    m = PIPE_RE.search(raw)
    if not m:
        return ""
    return f'{int(m.group(1))}\"'

def parse_inverts(raw: str) -> Tuple[List[float], List[float]]:
    outs, allv = [], []
    for m in OUT_INV_RE.finditer(raw or ""):
        try:
            val = m.group(1) or m.group(2)
            z = float(val); outs.append(z); allv.append(z)
        except Exception:
            pass
    for m in INV_RE.finditer(raw or ""):
        try:
            z = float(m.group(1))
            if z not in allv:
                allv.append(z)
        except Exception:
            pass
    return outs, allv

def _layer_tokens(text: str) -> List[str]:
    """Return uppercase CAD layer/name tokens without letting CO match CONTOUR, etc."""
    raw = (text or "").upper()
    return [t for t in re.split(r"[^A-Z0-9]+", raw) if t]

def is_bad_structure_layer(text: str) -> bool:
    """Layers that should never create structure centers.

    The previous detector used broad substring matching, so layers like
    P-Topo Minor Contours and P-Erosion Control could become false
    structure centers because they contain tokens like CO/CONTROL/CONTOUR.
    """
    raw = (text or "").upper()
    toks = set(_layer_tokens(raw))
    bad_tokens = {
        "TOPO", "CONTOUR", "CONTOURS", "MAJOR", "MINOR",
        "EROSION", "CONTROL", "SILT", "FENCE", "WATTLE",
        "CENTERLINE", "CENTER", "CL", "PIPE", "PIPES",
        "FEATURE", "FEATURELINE", "FEATURELINES",
        "GRADE", "GRADING", "SURFACE", "TIN",
        "TEXT", "NOTE", "NOTES", "LABEL", "LABELS",
        "CURB", "PAVEMENT", "EOP", "BC", "FL"
    }
    if toks & bad_tokens:
        return True
    # Also catch common glued layer strings.
    glued = normalize_layer_name(raw)
    for bad in ["TOPO", "CONTOUR", "EROSION", "CENTERLINE", "FEATURELINE", "GRADINGCONTOUR"]:
        if bad in glued:
            return True
    return False

def is_structure_layer_or_name(text: str, find_mode: str = "Both", *, for_geometry: bool = False) -> bool:
    raw = (text or "").upper()
    n = normalize_layer_name(raw)
    toks = set(_layer_tokens(raw))
    mode = (find_mode or "Both").upper()

    # For actual structure center geometry, be strict.  Pipe centerlines,
    # contours, erosion control, topo, and generic CO/CONTROL matches are not structures.
    if for_geometry and is_bad_structure_layer(raw):
        return False

    storm_tokens = {"STORM", "STM", "DRAIN", "DRAINAGE", "INLET", "CB", "CI", "DI", "AI", "YI", "GI", "JB", "JBOX", "JUNCTION", "CATCH", "BASIN", "STRUCTURE", "STRUCTURES", "MH", "MANHOLE"}
    san_tokens = {"SAN", "SANITARY", "SS", "SSMH", "SMH", "SEWER", "MH", "MANHOLE", "CLEANOUT", "CLEAN", "OUT"}
    # glued names for common block/layer names like STMH1, SSMH2, AREA_INLET, etc.
    storm_glued = ["STMH", "STM", "STORM", "AREAINLET", "CURBINLET", "CATCHBASIN", "JUNCTIONBOX", "DROPINLET"]
    san_glued = ["SSMH", "SMH", "SANMH", "SANITARY", "SANSEWER", "CLEANOUT"]

    storm_hit = bool(toks & storm_tokens) or any(w in n for w in storm_glued)
    san_hit = bool(toks & san_tokens) or any(w in n for w in san_glued)

    # Do not let a standalone CO token count as cleanout unless the layer/name
    # also clearly mentions sanitary/sewer/cleanout.  This prevents CONTROL/CONTOUR issues.
    if "CO" in toks and not ({"CLEANOUT", "CLEAN", "SAN", "SANITARY", "SEWER", "SS"} & toks or "CLEANOUT" in n):
        san_hit = False

    if mode == "STORM":
        return storm_hit
    if mode == "SANITARY":
        return san_hit
    return storm_hit or san_hit

def _structure_candidate_score(st: Dict[str, Any]) -> int:
    """Higher is better when duplicate candidate centers are near each other."""
    src = (st.get("source", "") or "").upper()
    layer = (st.get("layer", "") or "").upper()
    score = 0
    if "BLOCK" in src: score += 100
    if "CIRCLE" in src: score += 80
    if "POLYLINE" in src: score += 50
    if st.get("label"): score += 30
    if "STRUCT" in layer or "INLET" in layer or "MANHOLE" in layer or "MH" in layer: score += 30
    if "CENTERLINE" in layer: score -= 100
    if is_bad_structure_layer(layer): score -= 200
    return score

def dedupe_structure_candidates(structures: List[Dict[str, Any]], tol: float = 2.0) -> List[Dict[str, Any]]:
    """Merge duplicate centers within tol feet and keep the best candidate."""
    keep: List[Dict[str, Any]] = []
    for st in sorted(structures, key=_structure_candidate_score, reverse=True):
        merged = False
        for k in keep:
            if dist((st["x"], st["y"]), (k["x"], k["y"])) <= tol:
                # Keep best coordinates, but borrow missing label/source info.
                if not k.get("label") and st.get("label"):
                    k["label"] = st["label"]
                k["source"] = (k.get("source","") + " / duplicate suppressed: " + st.get("source",""))[:300]
                merged = True
                break
        if not merged:
            keep.append(st)
    return keep

def load_dxf_structures_and_text(cad_path: str, find_mode: str = "Both") -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if ezdxf is None:
        raise RuntimeError("ezdxf is required. Run install_requirements.bat first.")
    doc = ezdxf.readfile(cad_path)
    msp = doc.modelspace()
    structures: List[Dict[str, Any]] = []
    texts: List[Dict[str, Any]] = []
    seen = set()
    def add_struct(x, y, label, source, layer, handle):
        key=(round(x,2), round(y,2), handle)
        if key in seen:
            return
        seen.add(key)
        structures.append({"x":float(x), "y":float(y), "label":normalize_structure_label(label), "source":source, "layer":layer, "handle":handle})
    for ent in msp:
        typ = ent.dxftype()
        layer = entity_layer(ent)
        handle = str(getattr(ent.dxf, "handle", ""))
        if typ in ("TEXT", "MTEXT"):
            raw = text_value(ent).replace("\\P", " ").replace("\n", " ")
            try:
                ins = ent.dxf.insert
                x, y = float(ins.x), float(ins.y)
            except Exception:
                x, y = 0.0, 0.0
            texts.append({"x":x, "y":y, "raw":raw, "label":parse_structure_label(raw), "pipe":parse_pipe_size(raw), "outs":parse_inverts(raw)[0], "inverts":parse_inverts(raw)[1], "layer":layer})
            # Some plans use text labels only near a block/circle. Do not add standalone text structures yet unless label is strong.
        elif typ == "INSERT":
            name = ""
            try: name = ent.dxf.name or ""
            except Exception: pass
            label = parse_structure_label(name)
            try:
                ins = ent.dxf.insert; x, y = float(ins.x), float(ins.y)
            except Exception:
                continue
            if label or is_structure_layer_or_name(layer + " " + name, find_mode, for_geometry=True):
                add_struct(x, y, label, f"DXF block {name}", layer, handle)
        elif typ == "CIRCLE":
            try:
                c = ent.dxf.center; x, y = float(c.x), float(c.y)
            except Exception:
                continue
            if is_structure_layer_or_name(layer, find_mode, for_geometry=True):
                add_struct(x, y, "", "DXF circle center", layer, handle)
        elif typ in ("LWPOLYLINE", "POLYLINE"):
            # Closed square/rectangle/circular-looking structures on storm/san layers.
            if not is_structure_layer_or_name(layer, find_mode, for_geometry=True):
                continue
            try:
                pts = polyline_points(ent, arc_spacing=8.0)
            except Exception:
                pts = []
            if len(pts) >= 3:
                xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
                w=max(xs)-min(xs); h=max(ys)-min(ys)
                # Limit to likely structures, not long pipe/curb lines.
                if 1.0 <= w <= 25.0 and 1.0 <= h <= 25.0:
                    closed = bool(getattr(ent, 'closed', False) if typ == 'LWPOLYLINE' else ent.is_closed)
                    if not closed:
                        continue
                    polygon = pts[:-1] if dist(pts[0], pts[-1]) < 1e-8 else pts
                    pairs = list(zip(polygon, polygon[1:] + polygon[:1]))
                    cross = [a[0]*b[1]-b[0]*a[1] for a,b in pairs]
                    area2 = sum(cross)
                    if abs(area2) <= 1e-12:
                        continue
                    cx = sum((a[0]+b[0])*c for (a,b),c in zip(pairs,cross))/(3*area2)
                    cy = sum((a[1]+b[1])*c for (a,b),c in zip(pairs,cross))/(3*area2)
                    add_struct(cx, cy, "", "DXF closed structure centroid", layer, handle)
    # Remove duplicate/false nearby structure centers and keep the best source.
    structures = dedupe_structure_candidates(structures, tol=2.0)

    # Attach nearby structure labels to unlabeled structures.
    for st in structures:
        if not st["label"]:
            best=None; bd=999999
            for t in texts:
                if t.get("label"):
                    d=dist((st["x"],st["y"]),(t["x"],t["y"]))
                    if d<bd and d<=80:
                        best=t; bd=d
            if best:
                st["label"]=best["label"]
                st["source"] += f" + nearby label {best['raw']}"
    return structures, texts

def extract_pdf_flout_text(pdf_path: str) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    if not pdf_path or not os.path.exists(pdf_path) or fitz is None:
        return out
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return out
    for pi, page in enumerate(doc):
        text = page.get_text("text") or ""
        # Break into windows around structure labels.
        labels = [(m.start(), normalize_structure_label(m.group(0))) for m in STRUCTURE_RE.finditer(text)]
        for number, (idx, label) in enumerate(labels):
            end = labels[number+1][0] if number+1 < len(labels) else min(len(text),idx+700)
            win = text[idx:end].replace("\n", " ")
            outs, invs = parse_inverts(win)
            pipe = parse_pipe_size(win)
            if outs or invs or pipe:
                out.setdefault(label, []).append({"page":pi+1, "raw":win[:500], "pipe":pipe, "outs":outs, "inverts":invs})
    return out

def build_flout_description(label: str, pipe: str, fmt: str, review: bool=False) -> str:
    label = normalize_structure_label(label)
    pipe = pipe.strip()
    fmt = (fmt or "Structure + Pipe Size + FL-OUT").upper()
    if review:
        if label and pipe: return f'{label} {pipe} FL-OUT REVIEW'
        if label: return f'{label} FL-OUT REVIEW'
        if pipe: return f'{pipe} FL-OUT REVIEW'
        return 'FL-OUT REVIEW'
    if "STRUCTURE" in fmt and "PIPE" in fmt:
        if label and pipe: return f'{label} {pipe} FL-OUT'
        if label: return f'{label} FL-OUT'
        if pipe: return f'{pipe} FL-OUT'
    elif "STRUCTURE" in fmt:
        if label: return f'{label} FL-OUT'
        if pipe: return f'{pipe} FL-OUT'
    elif "PIPE" in fmt:
        if pipe: return f'{pipe} FL-OUT'
        if label: return f'{label} FL-OUT'
    return 'FL-OUT'

def load_manual_flout_points(path: str, cad_path: str = '') -> List[Dict[str, Any]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError('Invalid manual FL OUT control file.')
    validate_saved_drawing(data, cad_path)
    return data.get('points', [])

def save_manual_flout_points(path: str, points: List[Dict[str, Any]], cad_path: str):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump({'cad_path': cad_path, 'dxf_sha256': drawing_fingerprint(cad_path), 'points': points}, stream, indent=2)

def run_flout_generation(cad_path: str, pdf_path: str, out_dir: str, start_pt: int, find_mode: str, desc_format: str, search_radius: float, manual_path: str = "", units_confirmed: bool = False) -> Tuple[str, str, int, int, int]:
    validate_numeric_settings(search_radius=search_radius)
    out_dir = prepare_output_directory(out_dir)
    reset_generation_state()
    validate_generation_inputs(cad_path, units_confirmed=units_confirmed)
    structures, texts = load_dxf_structures_and_text(cad_path, find_mode)
    pdf_by_label = extract_pdf_flout_text(pdf_path)
    points: List[GenPoint] = []
    assigned = 0
    review = 0
    for i, st in enumerate(structures):
        sp=(st["x"], st["y"])
        label = st.get("label", "")
        near=[]
        for t in texts:
            d=dist(sp,(t["x"],t["y"]))
            if d <= search_radius:
                near.append((d,t))
        near.sort(key=lambda x:x[0])
        pipe=""; outs=[]; invs=[]; source_parts=[]
        for d,t in near[:12]:
            if not label and t.get("label"):
                label=t["label"]
            if not pipe and t.get("pipe"):
                pipe=t["pipe"]
            if not label or not t.get("label") or normalize_structure_label(t["label"]) != normalize_structure_label(label):
                continue
            if t.get("outs"):
                outs.extend(t["outs"]); source_parts.append(f"CAD OUT text {t['raw'][:80]}")
            if t.get("inverts"):
                invs.extend(t["inverts"])
        # PDF by structure number strengthens labels when CAD text is not enough.
        if label and label in pdf_by_label:
            for item in pdf_by_label[label]:
                if not pipe and item.get("pipe"):
                    pipe=item["pipe"]
                if item.get("outs"):
                    outs.extend(item["outs"]); source_parts.append(f"PDF page {item['page']} OUT callout")
                if item.get("inverts"):
                    invs.extend(item["inverts"])
        elev=None; conf="REVIEW"; issue=""
        unique_outs = sorted({float(z) for z in outs if valid_grade_z(z)})
        if len(unique_outs) == 1:
            elev=unique_outs[0]; conf="STRUCTURE_OUT"; issue=""
            assigned += 1
        elif len(unique_outs) > 1:
            issue="Conflicting OUT elevations for this structure. Review required."
            review += 1
        elif invs:
            issue="No explicit OUT found. IN/unspecified inverts cannot define FL OUT."
            review += 1
        else:
            issue="No FL OUT / invert elevation found nearby. Review plans."
            review += 1
        desc=build_flout_description(label, pipe, desc_format, review=(elev is None))
        src="; ".join(source_parts[:3]) if source_parts else f"Structure center from {st['source']} layer {st['layer']}"
        points.append(GenPoint(start_pt+i, st["y"], st["x"], elev, desc, src, issue, conf))
    # Add manually picked structure center / FL OUT records last. These are high confidence
    # because the user clicked the structure center and keyed the flowline out data.
    manual = load_manual_flout_points(manual_path, cad_path)
    for item in manual:
        try:
            x = float(item.get("x")); y = float(item.get("y"))
        except Exception:
            continue
        label = normalize_structure_label(item.get("label", ""))
        pipe = (item.get("pipe", "") or "").strip()
        elev = None
        try:
            elev = float(item.get("elev"))
        except Exception:
            pass
        desc = build_flout_description(label, pipe, desc_format, review=(elev is None))
        issue = "" if elev is not None else "Manual structure center has no FL OUT elevation. Review."
        if elev is not None:
            assigned += 1
        else:
            review += 1
        pick_source = (item.get("pick_source") or "Manual picked structure center")
        points.append(GenPoint(start_pt+len(points), y, x, elev, desc, f"{pick_source} / keyed FL OUT", issue, "MANUAL_FL_OUT" if elev is not None else "REVIEW"))

    csv_path=os.path.join(out_dir,"Generated_FL_OUT_Points.csv")
    debug_path=os.path.join(out_dir,"Generated_FL_OUT_QA_Report.csv")
    sanitize_field_elevations(points)
    write_csv(points, csv_path)
    write_do_not_stake_csv(points, os.path.join(out_dir, "FL_OUT_DO_NOT_STAKE.csv"))
    write_debug_csv(points, debug_path)
    assigned = len(stake_ready_points(points))
    return csv_path, debug_path, len(points), assigned, len(points)-assigned



def _ray_segment_intersection_distance(origin: Point2, direction: Point2, a: Point2, b: Point2) -> Optional[float]:
    """Return ray distance t where origin + t*direction intersects segment AB.

    Used to auto-detect sidewalk width by shooting a perpendicular ray from the
    picked sidewalk edge to the opposite sidewalk edge.  DXF/CAD coordinates stay fixed.
    """
    ox, oy = origin
    dx, dy = direction
    ax, ay = a
    bx, by = b
    sx, sy = bx - ax, by - ay
    den = dx * sy - dy * sx
    if abs(den) < 1e-10:
        return None
    qx, qy = ax - ox, ay - oy
    t = (qx * sy - qy * sx) / den
    u = (qx * dy - qy * dx) / den
    if t > 0.05 and -1e-6 <= u <= 1.0 + 1e-6:
        return float(t)
    return None


def _sidewalk_candidate_segments_from_dxf(dxf_path: str, selected_handles: List[str]) -> List[CurbSegment]:
    """Return sidewalk/walk segments from the DXF, excluding the picked edge handles.

    This supports 2D sidewalk jobs where the user picks one edge and GradeGuard
    needs to find the opposite edge automatically for cross-slope checks.
    """
    if not dxf_path or ezdxf is None:
        return []
    hset = set(str(h) for h in selected_handles or [])
    out: List[CurbSegment] = []
    try:
        doc = ezdxf.readfile(dxf_path)
        for ent in doc.modelspace():
            h = str(getattr(ent.dxf, "handle", ""))
            if h in hset:
                continue
            layer = entity_layer(ent) or ""
            n = normalize_layer_name(layer)
            # Main rule requested: anything in the layer that says SIDEWALK.
            # Keep WALK variants too for other engineers.
            if not any(t in n for t in ["SIDEWALK", "WALK", "SWLK"]):
                continue
            typ = ent.dxftype()
            try:
                if typ == "LINE":
                    pts = line_points(ent)
                elif typ in ("LWPOLYLINE", "POLYLINE"):
                    pts = polyline_points(ent, arc_spacing=2.0)
                elif typ == "ARC":
                    pts = arc_points_from_entity(ent, spacing=2.0)
                else:
                    continue
                for a, b in zip(pts[:-1], pts[1:]):
                    if dist(a, b) > 0.05:
                        out.append(CurbSegment(a, b, layer))
            except Exception:
                continue
    except Exception:
        return []
    return out


def detect_sidewalk_width_from_opposite_edge(dxf_path: str, selected_handles: List[str], min_width: float = 3.0, max_width: float = 20.0) -> Tuple[Optional[float], str]:
    """Estimate sidewalk width from the opposite sidewalk edge.

    Process:
    1. Build the selected edge from the manually picked sidewalk handles.
    2. Build candidate sidewalk/walk segments from the same DXF, excluding picked handles.
    3. Shoot perpendicular rays left and right from selected-edge sample points.
    4. Use the median hit distance as the detected width.

    If the opposite edge is not found with confidence, return None and a note so
    the manual width box is used.
    """
    picked = selected_handles_to_segments(dxf_path, selected_handles, layer_fallback="Selected Sidewalk Edge")
    candidates = _sidewalk_candidate_segments_from_dxf(dxf_path, selected_handles)
    hits: List[float] = []
    for seg in picked:
        ax, ay = seg.a
        bx, by = seg.b
        vx, vy = bx - ax, by - ay
        L = math.hypot(vx, vy)
        if L < 1.0:
            continue
        ux, uy = vx / L, vy / L
        normals = [(-uy, ux), (uy, -ux)]
        samples = [(ax + vx * 0.25, ay + vy * 0.25), (ax + vx * 0.50, ay + vy * 0.50), (ax + vx * 0.75, ay + vy * 0.75)]
        for sp in samples:
            best = None
            for nvec in normals:
                for cand in candidates:
                    t = _ray_segment_intersection_distance(sp, nvec, cand.a, cand.b)
                    if t is None:
                        continue
                    if min_width <= t <= max_width and (best is None or t < best):
                        best = t
            if best is not None:
                hits.append(best)
    if len(hits) < 3:
        return None, "Sidewalk width not confidently detected. Enter width manually."
    hits.sort()
    mid = hits[len(hits)//2]
    # Basic consistency check. Reject if the hit spread is too wild.
    q1 = hits[len(hits)//4]
    q3 = hits[(len(hits)*3)//4]
    spread = q3 - q1
    if spread > max(1.0, mid * 0.25):
        return None, f"Sidewalk width not confidently detected. Hits ranged too much ({q1:.2f} to {q3:.2f} ft). Enter width manually."
    return float(mid), f"Detected sidewalk width from opposite edge: {mid:.2f} ft using {len(hits)} perpendicular hits."


def enforce_sidewalk_local_reasonableness(points, elevs, max_local_jump=0.75, search_radius=35.0):
    """Flag unsupported local discontinuities; never redesign a grade by median."""
    out = [list(e) for e in elevs]
    for i, p in enumerate(points):
        z, src, issue, conf = out[i]
        if z is None or held_result(conf, src) or 'SURFACE_HIGH' in conf:
            continue
        neighbors = [float(elevs[j][0]) for j,q in enumerate(points) if i != j and elevs[j][0] is not None and 0.1 < dist(p,q) <= search_radius]
        if len(neighbors) >= 4:
            med = sorted(neighbors)[len(neighbors)//2]
            if abs(z-med) > max_local_jump:
                out[i][2] = (issue+'; ' if issue else '')+'LOCAL_REVIEW: discontinuity requires a design check; elevation preserved.'
                out[i][3] = 'LOCAL_REVIEW'
    return [tuple(e) for e in out]


def run_edge_sidewalk_generation(cad_path: str, pdf_path: str, surface_path: str, out_dir: str, start_pt: int, spacing: float, description: str, elev_layer_keywords: str, contour_radius: float, selected_sidewalk_path: str = "", cross_slope_percent: float = 0.0, sidewalk_width: float = 0.0, cross_slope_adjust: str = "direct", auto_detect_width: bool = True, surface_name: str = "", units_confirmed: bool = False):
    """Generate edge-of-sidewalk staking points from selected sidewalk edge geometry.

    This is intentionally separate from BC staking. It uses the same surface/contour
    elevation engine, but descriptions default to SIDEWALK and it does not apply curb height.
    """
    reset_generation_state()
    validate_generation_inputs(cad_path, spacing, units_confirmed)
    validate_numeric_settings(cross_slope_percent=cross_slope_percent, sidewalk_width=sidewalk_width, contour_radius=contour_radius)
    out_dir = prepare_output_directory(out_dir)
    if not cad_path or not os.path.exists(cad_path):
        raise RuntimeError("Pick a Civil CAD DXF first.")
    if cad_path.lower().endswith(".dwg"):
        raise RuntimeError("DWG files need to be converted to DXF first.")
    selected_handles = load_selected_curb_handles(selected_sidewalk_path, cad_path)
    width_note = "Manual sidewalk width used."
    detected_width = None
    if auto_detect_width and selected_handles:
        detected_width, width_note = detect_sidewalk_width_from_opposite_edge(cad_path, selected_handles)
        if detected_width is not None:
            sidewalk_width = detected_width
    # Edge of sidewalk detection intentionally catches ANY layer name containing SIDEWALK.
    # It also supports common abbreviations, but the plain SIDEWALK keyword is the main rule.
    sidewalk_keywords = ["SIDEWALK", "P-SIDEWALK", "EDGE OF SIDEWALK", "CONCRETE SIDEWALK", "SIDEWALK EDGE", "WALK", "P-WALK", "ESW", "E-SW", "SWLK", "S-WALK", "SW"]
    elev_keywords = [x.strip() for x in re.split(r"[,;]", elev_layer_keywords) if x.strip()]
    raw_points, layers, cad_labels, segments, dxf_stats = load_dxf_geometry_and_elevations(cad_path, sidewalk_keywords, elev_keywords, spacing, selected_handles)
    raw_points, pcpt_added = add_curb_geometry_control_points(raw_points, tolerance=0.05)
    raw_points, edge_contour_added = add_edge_contour_crossing_points(raw_points, segments, tolerance=0.25)
    raw_points, projected_edge_labels, edge_callouts_added = add_grade_callout_points_to_selected_edge(raw_points, cad_labels, tolerance=0.35, max_label_to_edge_dist=140.0)
    landxml_surface = safe_load_landxml_surface(surface_path, surface_name)
    pdf_scan = scan_pdf_for_curb_terms(pdf_path)
    # Projected CAD/PDF leader tick callouts hold first. When there is no LandXML surface,
    # build a TIN from the 3D contour polylines so sidewalk grades are prorated between
    # contours instead of snapping to a nearby contour.
    primary_labels = projected_edge_labels + cad_labels
    contour_tin = build_contour_tin_surface(segments)
    elevs = assign_sidewalk_elevations(raw_points, primary_labels, segments, contour_radius, landxml_surface=landxml_surface, contour_tin=contour_tin)
    dxf_stats["sidewalk_contour_tin"] = "created from 3D contour polylines" if contour_tin else "not available"
    # Optional sidewalk cross-slope adjustment. Direct is the normal workflow when contours/surface
    # touch the selected edge. Auto samples left/right of the selected edge to help with 1% cross slope.
    adj_mode = (cross_slope_adjust or "direct").strip().lower()
    adjusted_elevs = []
    for pnt, (elev, src, issue, conf) in zip(raw_points, elevs):
        new_elev, new_src = sidewalk_cross_slope_auto_adjust(pnt, elev, src or "", segments, float(sidewalk_width or 0.0), float(cross_slope_percent or 0.0), adj_mode, contour_radius)
        adjusted_elevs.append((new_elev, new_src, issue, conf))
    elevs = adjusted_elevs
    # Remove obvious bad contour/TIN outliers before cross-slope QA.  This protects
    # 2D sidewalk jobs where one distant 3D contour can create a bad temporary TIN triangle.
    elevs = enforce_sidewalk_local_reasonableness(raw_points, elevs, max_local_jump=max(0.50, float(sidewalk_width or 10.0)*0.05), search_radius=max(25.0, float(sidewalk_width or 10.0)*4.0))
    # Edge-to-edge cross-slope QA/enforcement.  This pairs points approximately one
    # sidewalk width apart and perpendicular to the selected walk run.  In auto mode
    # it clamps the high side so cross slope does not exceed the entered percent.
    elevs = enforce_sidewalk_cross_slope_pairs(raw_points, elevs, float(sidewalk_width or 0.0), float(cross_slope_percent or 0.0), adj_mode)
    gen=[]; pt=int(start_pt); desc=(description or "SIDEWALK").strip() or "SIDEWALK"
    for (x,y),(elev,src,issue,conf) in zip(raw_points, elevs):
        gen.append(GenPoint(point=pt, northing=y, easting=x, elevation=elev, desc=desc, source=src, issue=issue, confidence=conf))
        pt += 1
    if desc.upper() == "SIDEWALK":
        apply_pc_pt_descriptions(gen, base_desc="SIDEWALK")
    csv_path=os.path.join(out_dir, "Generated_Edge_Sidewalk_Points.csv")
    debug_path=os.path.join(out_dir, "Generated_Edge_Sidewalk_Points_with_sources.csv")
    report_path=os.path.join(out_dir, "GradeGuard_Edge_Sidewalk_Report.html")
    sanitize_field_elevations(gen)
    write_csv(gen, csv_path)
    write_do_not_stake_csv(gen, os.path.join(out_dir, "Sidewalk_DO_NOT_STAKE.csv"))
    write_debug_csv(gen, debug_path)
    sidewalk_flow_pdf = os.path.join(out_dir, "GradeGuard_Sidewalk_Water_Flow_Map.pdf")
    dxf_stats["sidewalk_water_flow_pdf"] = write_water_flow_pdf(gen, sidewalk_flow_pdf, spacing=spacing, min_slope=0.001, title="Schoel GradeGuard - Sidewalk Water Flow Map") or ""
    dxf_stats["edge_sidewalk_layers"] = layers
    dxf_stats["selected_edge_sidewalk_handles"] = len(selected_handles)
    dxf_stats["pc_pt_geometry_points_added"] = pcpt_added
    dxf_stats["sidewalk_contour_crossing_points_added"] = edge_contour_added
    dxf_stats["sidewalk_leader_callout_points_added"] = edge_callouts_added
    dxf_stats["landxml_surface"] = {"path": surface_path if landxml_surface else "", "triangles": len(landxml_surface.triangles) if landxml_surface else 0, "feature_segments": len(getattr(landxml_surface, "segments", []) or []) if landxml_surface else 0}
    dxf_stats["sidewalk_cross_slope"] = {"percent": cross_slope_percent, "width_ft": sidewalk_width, "adjustment": cross_slope_adjust, "auto_detect_width": auto_detect_width, "detected_width_ft": detected_width, "width_note": width_note}
    write_report(gen, layers, pdf_scan, dxf_stats, report_path)
    from gradeguard_review import write_cad_review
    write_cad_review(gen, out_dir, 'SIDEWALK', CURB_SEGMENTS, cad_path, pdf_path, surface_path,
                     landxml_surface, settings={'cross_slope_percent':cross_slope_percent,'width':sidewalk_width,'adjustment':cross_slope_adjust})
    return csv_path, debug_path, report_path, len(gen), len([p for p in gen if p.elevation is not None]), len([p for p in gen if p.elevation is None])


def run_grade_grid_generation(cad_path: str, surface_path: str, out_dir: str, start_pt: int, grid_spacing: float, subtract_amount: float, description: str, elev_layer_keywords: str, contour_radius: float, selected_curb_path: str = "", surface_name: str = "", units_confirmed: bool = False):
    """Generate a simple finished grade or subgrade grid across the selected curb/site extents.

    This is intentionally separate from BC staking.  CAD/DXF supplies the horizontal limits,
    while LandXML/Topo-P-Surface/contours supply finished grade.  For SUBGRADE, the user-entered
    amount is subtracted from finished grade.  For FINISHED GRADE, subtract should be 0.
    """
    reset_generation_state()
    validate_generation_inputs(cad_path, grid_spacing, units_confirmed)
    if not math.isfinite(float(subtract_amount)) or subtract_amount < 0:
        raise ValueError("Subgrade deduction must be finite and nonnegative.")
    out_dir = prepare_output_directory(out_dir)
    ext = os.path.splitext(cad_path)[1].lower()
    if ext == ".dwg":
        raise RuntimeError("DWG files need to be converted to DXF first. Export from Civil 3D or use ODA File Converter, then load the DXF.")
    if ext != ".dxf":
        raise RuntimeError("Grade grid currently reads DXF. Save the civil CAD as DXF first.")
    if grid_spacing <= 0:
        raise RuntimeError("Grid spacing must be greater than zero.")

    elev_keywords = [x.strip() for x in re.split(r"[,;]", elev_layer_keywords) if x.strip()]
    # Use the selected BC objects if present for the grid limits; otherwise use auto curb detection.
    selected_handles = load_selected_curb_handles(selected_curb_path, cad_path)
    curb_keywords = ["AUTO", "SITE-P-BC", "SITE-BC", "P-BC", "BOC", "BACK OF CURB", "BACK-CURB", "BACK_CURB", "BC", "TBC", "TOP BACK OF CURB", "P-CURB", "CURB"]
    raw_points, layers, cad_labels, segments, dxf_stats = load_dxf_geometry_and_elevations(cad_path, curb_keywords, elev_keywords, max(25.0, grid_spacing), selected_handles)

    bbox_pts = []
    for pnt in raw_points:
        bbox_pts.append(pnt)
    for cs in CURB_SEGMENTS:
        bbox_pts.append(cs.a); bbox_pts.append(cs.b)
    if not bbox_pts:
        for seg in segments:
            bbox_pts.append(seg.a); bbox_pts.append(seg.b)
    if not bbox_pts:
        raise RuntimeError("Could not determine grid limits from the DXF. Pick True Back of Curb or provide CAD linework/contours.")

    minx = min(p[0] for p in bbox_pts); maxx = max(p[0] for p in bbox_pts)
    miny = min(p[1] for p in bbox_pts); maxy = max(p[1] for p in bbox_pts)
    # Small buffer so edge points are included.  The user can delete extra grid points in CAD if needed.
    buf = max(0.0, grid_spacing * 0.25)
    minx -= buf; maxx += buf; miny -= buf; maxy += buf

    grid = []
    y = miny
    # prevent accidental giant grid
    max_points = 25000
    while y <= maxy + 1e-6:
        x = minx
        while x <= maxx + 1e-6:
            grid.append((x, y))
            if len(grid) > max_points:
                raise RuntimeError("Grid would create more than 25,000 points. Increase the grid spacing or use a smaller selected boundary.")
            x += grid_spacing
        y += grid_spacing

    landxml_surface = safe_load_landxml_surface(surface_path, surface_name)
    # For site grade grids, contours/surfaces represent finished grade directly.  Never apply curb height here.
    elevs = assign_elevations(grid, [], segments, 0.0, 0.0, contour_radius, False, 0.15, landxml_surface=landxml_surface, surface_mode="bc", curb_controls=False)

    desc = (description or "SUBGRADE").upper().strip()
    pts=[]
    ptno=int(start_pt)
    for (x,y),(fg,src,issue,conf) in zip(grid,elevs):
        z = None if fg is None else float(fg) - float(subtract_amount or 0.0)
        note = issue or ""
        if desc == "SUBGRADE":
            src = (src or "") + f"; SUBGRADE = finished grade - {float(subtract_amount or 0.0):.3f} ft"
        pts.append(GenPoint(point=ptno, northing=y, easting=x, elevation=z, desc=desc, source=src, issue=note, confidence=conf))
        ptno += 1

    safe = "SUBGRADE" if desc == "SUBGRADE" else "FINISHED_GRADE"
    csv_path = os.path.join(out_dir, f"Generated_{safe}_Points.csv")
    debug_path = os.path.join(out_dir, f"Generated_{safe}_Points_with_sources.csv")
    sanitize_field_elevations(pts)
    write_csv(pts, csv_path)
    write_do_not_stake_csv(pts, os.path.join(out_dir, "Grade_Grid_DO_NOT_STAKE.csv"))
    write_debug_csv(pts, debug_path)
    from gradeguard_review import write_cad_review
    write_cad_review(pts, out_dir, safe, [], cad_path, surface_path=surface_path,
                     surface=landxml_surface, deduction=float(subtract_amount or 0.0))
    return csv_path, debug_path, len(pts), len([p for p in pts if p.elevation is not None]), len([p for p in pts if p.elevation is None])



def safe_load_landxml_surface(path: str, surface_name: str = "") -> Optional[LandXMLSurface]:
    """An omitted surface is optional; an explicitly selected invalid surface is an error."""
    if not path or not str(path).strip():
        return None
    if os.path.splitext(str(path))[1].lower() not in ('.xml', '.landxml'):
        raise ValueError("Selected surface must be LandXML/XML. Clear the surface box to use DXF contours.")
    return load_landxml_surface(path, surface_name)


def resource_path(filename: str) -> str:
    """Find bundled resources in source mode or PyInstaller onefile mode."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, filename)

def set_gradeguard_app_id():
    """Force Windows taskbar grouping/icon to use the GradeGuard app identity."""
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"SchoelEngineering.GradeGuard.v{APP_VERSION}")
        except Exception:
            pass

def apply_gradeguard_icon(win):
    """Apply GradeGuard icon to root and child windows."""
    try:
        ico_path = resource_path("gradeguard_icon.ico")
        png_path = resource_path("gradeguard_icon.png")
        if os.path.exists(ico_path):
            win.iconbitmap(ico_path)
        if os.path.exists(png_path):
            # Keep a reference on each window so Tk does not garbage collect the image.
            img = tk.PhotoImage(file=png_path)
            win._gradeguard_icon_img = img
            win.iconphoto(True, img)
    except Exception:
        pass

class GradeGuardApp(tk.Tk):
    def __init__(self, show_splash=False):
        set_gradeguard_app_id()
        super().__init__()
        self._splash = None
        if show_splash:
            self.withdraw()
        self.title(f"Schoel GradeGuard v{APP_VERSION} Multi-Job")
        apply_gradeguard_icon(self)
        self.geometry("1180x820")
        self.minsize(1000, 700)

        self.cad_var = tk.StringVar()
        self.surface_var = tk.StringVar()
        self.surface_name_var = tk.StringVar()
        self.unitless_confirm_var = tk.BooleanVar(value=False)
        self.pdf_var = tk.StringVar()
        self.project_var = tk.StringVar(value="Unsaved GradeGuard Project")
        self.out_var = tk.StringVar(value=os.path.join(os.getcwd(), "GradeGuard_Output"))
        self.start_var = tk.StringVar(value="6000")
        self.spacing_var = tk.StringVar(value="25")
        self.height_var = tk.StringVar(value="0.50")
        # v8.4 boss-checklist workflow: force the user to identify the curb detail before FL/gutter grades are raised to BC.
        self.curb_type_var = tk.StringVar(value="Standard curb (+0.50)")
        self.qa_latest_cad_var = tk.BooleanVar(value=True)
        self.qa_latest_pdf_var = tk.BooleanVar(value=True)
        self.qa_cad_units_var = tk.BooleanVar(value=True)
        self.qa_control_var = tk.BooleanVar(value=True)
        self.qa_benchmark_var = tk.BooleanVar(value=True)
        self.qa_pdf_trumps_cad_var = tk.BooleanVar(value=True)
        self.qa_curb_detail_var = tk.BooleanVar(value=True)
        self.qa_property_var = tk.BooleanVar(value=True)
        self.create_offset_var = tk.BooleanVar(value=False)
        self.offset_distance_var = tk.StringVar(value="3.00")
        self.offset_mode_var = tk.StringVar(value="away_from_flowline")
        self.offset_reverse_var = tk.BooleanVar(value=False)
        self.source_mode = tk.StringVar(value="surface")
        self.surface_interpret_var = tk.StringVar(value="auto")

        # Subgrade / finished grade grid settings.  These are separate from curb staking.
        self.grade_grid_type_var = tk.StringVar(value="SUBGRADE")
        self.grade_grid_start_var = tk.StringVar(value="8000")
        self.grade_grid_spacing_var = tk.StringVar(value="25")
        self.grade_grid_subtract_var = tk.StringVar(value="0.50")

        # Edge of sidewalk staking settings. Separate from curb staking.
        self.esw_start_var = tk.StringVar(value="9000")
        self.esw_spacing_var = tk.StringVar(value="25")
        self.esw_desc_var = tk.StringVar(value="SIDEWALK")
        # Optional sidewalk elevation adjustment.  Use Direct for the selected edge.
        # If elevations come from the opposite edge and the walk has cross slope,
        # add/subtract width*slope to get the selected edge.
        self.esw_cross_slope_var = tk.StringVar(value="1.00")
        self.esw_width_var = tk.StringVar(value="5.00")
        self.esw_auto_width_var = tk.BooleanVar(value=True)
        self.esw_adjust_var = tk.StringVar(value="direct")

        # Storm and sanitary FL-OUT point settings. Separate from BC and grade grids.
        self.flout_start_var = tk.StringVar(value="7000")
        self.flout_find_var = tk.StringVar(value="Both")
        self.flout_desc_format_var = tk.StringVar(value="Structure + Pipe Size + FL-OUT")
        self.flout_search_radius_var = tk.StringVar(value="120")

        # Hidden/default engine settings. Keep the interface simple.
        self.curb_layer_var = tk.StringVar(value="AUTO, SITE-P-BC, SITE-BC, P-BC, BOC, BACK OF CURB, BACK-CURB, BACK_CURB, BC, TBC, TOP BACK OF CURB")
        self.elev_layer_var = tk.StringVar(value="Topo-P-Surface, SURFACE, TIN, 3D, CONTOUR, CONTOURS, GRADING CONTOURS, P-Topo Minor Contours, P-Topo Major Contours, P-Topo Grading Contours, Topo-P-GRADING CONTOURS, Topo-P-INTER CONT, Topo-P-INDEX CONT")
        self.label_radius_var = tk.StringVar(value="5")
        self.contour_radius_var = tk.StringVar(value="50")
        self.min_slope_var = tk.StringVar(value="0.003")
        self.contour_tolerance_var = tk.StringVar(value="0.15")
        self.anchor_path = os.path.join(os.getcwd(), "GradeGuard_Tick_Anchors.json")
        self.selected_curb_path = os.path.join(os.getcwd(), "GradeGuard_Selected_BC_Entities.json")
        self.selected_flowline_path = os.path.join(os.getcwd(), "GradeGuard_Selected_Flowline_Entities.json")
        self.selected_sidewalk_path = os.path.join(os.getcwd(), "GradeGuard_Selected_Edge_Sidewalk_Entities.json")
        self.manual_flout_path = os.path.join(os.getcwd(), "GradeGuard_Manual_FL_OUT_Points.json")
        self.current_project_file = None
        self._generation_running = False
        self._build()
        if show_splash:
            self.show_opening()

    def show_opening(self):
        if self._splash is not None and self._splash.winfo_exists():
            self._splash.lift()
            return

        def open_workspace():
            self._splash = None
            self.deiconify()
            self.lift()

        try:
            from gradeguard_splash import GradeGuardSplash
            self._splash = GradeGuardSplash(self, APP_VERSION, resource_path, open_workspace)
        except Exception as error:
            # Decorative startup must never prevent access to a user's project.
            self.deiconify()
            self.log_msg(f"Opening animation unavailable: {error}")

    def _build(self):
        from gradeguard_ui import WorkflowView
        self._workflow = WorkflowView(self, APP_VERSION, resource_path)

    def _file_row(self, parent, row, label, var, cmd):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=var, width=80).grid(row=row, column=1, sticky="we", padx=8, pady=5)
        ttk.Button(parent, text="Browse", command=cmd).grid(row=row, column=2, sticky="e", pady=5)
        parent.columnconfigure(1, weight=1)

    def _qa_missing_items(self) -> List[str]:
        """v8.4.3: QA/QC is automatic.

        The program still requires the civil PDF and still writes QA/source reports,
        but it no longer asks the user to manually check a checklist before running.
        """
        return []

    def _confirm_preflight(self, cad: str, pdf: str, surface: str, out: str, curb_height: float, curb_note: str) -> bool:
        selected_bc = os.path.exists(self.selected_curb_path)
        selected_fl = os.path.exists(self.selected_flowline_path)
        summary = [
            f"GradeGuard version: {APP_VERSION}",
            f"CAD/DXF: {cad}",
            f"Certified civil PDF: {pdf}",
            f"LandXML/XML surface: {surface or 'NONE - DXF contours/surface fallback'}",
            f"Output folder: {out}",
            "Units: U.S. survey feet workflow (confirm drawing and source files)",
            f"Curb conversion: {curb_height:.3f} ft",
            f"Picked true BC geometry: {'YES' if selected_bc else 'NO - automatic layer detection only'}",
            f"Picked flowline/gutter geometry: {'YES' if selected_fl else 'NO'}",
            "BOC rule: bare BOC is ambiguous and remains review-only.",
            "Unsafe points will be omitted from the stake-ready CSV and listed in GradeGuard_DO_NOT_STAKE.csv.",
            "",
            curb_note,
            "",
            "Continue only after confirming these inputs against the certified plans.",
        ]
        return messagebox.askyesno("GradeGuard Preflight Review", "\n".join(summary), icon="warning")

    def _selected_curb_height_and_note(self) -> Tuple[float, bool, str]:
        v = self.curb_type_var.get()
        if v.startswith("Standard"):
            return 0.50, True, "Standard curb selected. EP/FL/gutter controls use +0.50 ft unless the certified PDF detail specifically says catch, spill, or another height."
        if v.startswith("Catch"):
            return 0.42, True, "Catch curb selected from PDF detail. EP/FL/gutter controls use +0.42 ft."
        if v.startswith("Spill"):
            return 0.58, True, "Spill curb selected from PDF detail. EP/FL/gutter controls use +0.58 ft."
        try:
            h = float(self.height_var.get())
        except Exception:
            h = 0.0
        if not (0.10 <= h <= 1.50):
            raise RuntimeError("Custom curb height must be a plausible value in feet and must come from the civil PDF detail.")
        return h, True, f"Custom curb height {h:.3f} ft selected. Use only when it matches the civil PDF detail."

    def _project_data(self):
        return {
            "version": APP_VERSION,
            "cad_path": self.cad_var.get().strip(),
            "surface_path": self.surface_var.get().strip(),
            "surface_name": self.surface_name_var.get().strip(),
            "unitless_us_survey_feet_confirmed": bool(self.unitless_confirm_var.get()),
            "pdf_path": self.pdf_var.get().strip(),
            "out_dir": self.out_var.get().strip(),
            "start_point": self.start_var.get().strip(),
            "spacing": self.spacing_var.get().strip(),
            "curb_height": self.height_var.get().strip(),
            "curb_type": self.curb_type_var.get(),
            "qa_latest_cad": bool(self.qa_latest_cad_var.get()),
            "qa_latest_pdf": bool(self.qa_latest_pdf_var.get()),
            "qa_cad_units": bool(self.qa_cad_units_var.get()),
            "qa_control": bool(self.qa_control_var.get()),
            "qa_benchmark": bool(self.qa_benchmark_var.get()),
            "qa_pdf_trumps_cad": bool(self.qa_pdf_trumps_cad_var.get()),
            "qa_curb_detail": bool(self.qa_curb_detail_var.get()),
            "qa_property": bool(self.qa_property_var.get()),
            "create_offset": bool(self.create_offset_var.get()),
            "offset_distance": self.offset_distance_var.get().strip(),
            "offset_mode": self.offset_mode_var.get(),
            "offset_reverse": bool(self.offset_reverse_var.get()),
            "source_mode": self.source_mode.get(),
            "surface_interpretation": self.surface_interpret_var.get(),
            "curb_layer_keywords": self.curb_layer_var.get(),
            "elevation_layer_keywords": self.elev_layer_var.get(),
            "label_radius": self.label_radius_var.get(),
            "contour_radius": self.contour_radius_var.get(),
            "min_slope": self.min_slope_var.get(),
            "contour_warning_tolerance": self.contour_tolerance_var.get(),
            "grade_grid_type": self.grade_grid_type_var.get(),
            "grade_grid_start": self.grade_grid_start_var.get(),
            "grade_grid_spacing": self.grade_grid_spacing_var.get(),
            "grade_grid_subtract": self.grade_grid_subtract_var.get(),
            "flout_start": self.flout_start_var.get(),
            "flout_find": self.flout_find_var.get(),
            "flout_desc_format": self.flout_desc_format_var.get(),
            "flout_search_radius": self.flout_search_radius_var.get(),
            "anchor_path": self.anchor_path,
            "selected_curb_path": self.selected_curb_path,
            "selected_flowline_path": self.selected_flowline_path,
            "selected_sidewalk_path": self.selected_sidewalk_path,
            "manual_flout_path": self.manual_flout_path,
            "sidewalk_start": self.esw_start_var.get(),
            "sidewalk_spacing": self.esw_spacing_var.get(),
            "sidewalk_description": self.esw_desc_var.get(),
            "sidewalk_cross_slope": self.esw_cross_slope_var.get(),
            "sidewalk_width": self.esw_width_var.get(),
            "sidewalk_auto_width": bool(self.esw_auto_width_var.get()),
            "sidewalk_adjustment": self.esw_adjust_var.get(),

        }

    def _load_project_data(self, data):
        self.last_calculation_review = ''
        self.cad_var.set(data.get("cad_path", ""))
        reset_generation_state()
        self.surface_var.set(data.get("surface_path", ""))
        self.surface_name_var.set(data.get("surface_name", ""))
        self.unitless_confirm_var.set(bool(data.get("unitless_us_survey_feet_confirmed", False)))
        self.pdf_var.set(data.get("pdf_path", ""))
        self.out_var.set(data.get("out_dir", os.path.join(os.getcwd(), "GradeGuard_Output")))
        self.start_var.set(str(data.get("start_point", "6000")))
        self.spacing_var.set(str(data.get("spacing", "25")))
        self.height_var.set(str(data.get("curb_height", "0.50")))
        self.curb_type_var.set(data.get("curb_type", "Standard curb (+0.50)"))
        self.qa_latest_cad_var.set(bool(data.get("qa_latest_cad", False)))
        self.qa_latest_pdf_var.set(bool(data.get("qa_latest_pdf", False)))
        self.qa_cad_units_var.set(bool(data.get("qa_cad_units", False)))
        self.qa_control_var.set(bool(data.get("qa_control", False)))
        self.qa_benchmark_var.set(bool(data.get("qa_benchmark", False)))
        self.qa_pdf_trumps_cad_var.set(bool(data.get("qa_pdf_trumps_cad", False)))
        self.qa_curb_detail_var.set(bool(data.get("qa_curb_detail", False)))
        self.qa_property_var.set(bool(data.get("qa_property", False)))
        self.create_offset_var.set(bool(data.get("create_offset", False)))
        self.offset_distance_var.set(str(data.get("offset_distance", "3.00")))
        self.offset_mode_var.set(data.get("offset_mode", "away_from_flowline"))
        self.offset_reverse_var.set(bool(data.get("offset_reverse", False)))
        self.source_mode.set(data.get("source_mode", "surface"))
        self.surface_interpret_var.set(data.get("surface_interpretation", "auto"))
        self.curb_layer_var.set(data.get("curb_layer_keywords", self.curb_layer_var.get()))
        self.elev_layer_var.set(data.get("elevation_layer_keywords", self.elev_layer_var.get()))
        self.label_radius_var.set(str(data.get("label_radius", "5")))
        self.contour_radius_var.set(str(data.get("contour_radius", "50")))
        self.min_slope_var.set(str(data.get("min_slope", "0.003")))
        self.contour_tolerance_var.set(str(data.get("contour_warning_tolerance", "0.15")))
        self.grade_grid_type_var.set(data.get("grade_grid_type", "SUBGRADE"))
        self.grade_grid_start_var.set(str(data.get("grade_grid_start", "8000")))
        self.grade_grid_spacing_var.set(str(data.get("grade_grid_spacing", "25")))
        self.grade_grid_subtract_var.set(str(data.get("grade_grid_subtract", "0.50")))
        self.anchor_path = data.get("anchor_path", os.path.join(os.getcwd(), "GradeGuard_Tick_Anchors.json"))
        self.selected_curb_path = data.get("selected_curb_path", os.path.join(os.getcwd(), "GradeGuard_Selected_BC_Entities.json"))
        self.selected_flowline_path = data.get("selected_flowline_path", os.path.join(os.getcwd(), "GradeGuard_Selected_Flowline_Entities.json"))
        self.esw_start_var.set(data.get('sidewalk_start', '9000'))
        self.esw_spacing_var.set(data.get('sidewalk_spacing', '25'))
        self.esw_desc_var.set(data.get('sidewalk_description', 'SIDEWALK'))
        self.esw_cross_slope_var.set(data.get('sidewalk_cross_slope', '1.00'))
        self.esw_width_var.set(data.get('sidewalk_width', '5.00'))
        self.esw_auto_width_var.set(data.get('sidewalk_auto_width', True))
        self.esw_adjust_var.set(data.get('sidewalk_adjustment', 'direct'))
        self.flout_start_var.set(data.get('flout_start', '7000'))
        self.flout_find_var.set(data.get('flout_find', 'Both'))
        self.flout_desc_format_var.set(data.get('flout_desc_format', 'Structure + Pipe Size + FL-OUT'))
        self.flout_search_radius_var.set(data.get('flout_search_radius', '120'))
        self.selected_sidewalk_path = data.get('selected_sidewalk_path', os.path.join(os.getcwd(), 'GradeGuard_Selected_Edge_Sidewalk_Entities.json'))
        self.manual_flout_path = data.get('manual_flout_path', os.path.join(os.getcwd(), 'GradeGuard_Manual_FL_OUT_Points.json'))

    def new_job(self):
        self.last_calculation_review = ''
        reset_generation_state()
        self.surface_name_var.set("")
        self.unitless_confirm_var.set(False)
        self.selected_flowline_path = os.path.join(os.getcwd(), "GradeGuard_Selected_Flowline_Entities.json")
        self.selected_sidewalk_path = os.path.join(os.getcwd(), "GradeGuard_Selected_Edge_Sidewalk_Entities.json")
        self.manual_flout_path = os.path.join(os.getcwd(), "GradeGuard_Manual_FL_OUT_Points.json")
        self.current_project_file = None
        self.project_var.set("Unsaved GradeGuard Project")
        self.cad_var.set("")
        self.surface_var.set("")
        self.pdf_var.set("")
        self.out_var.set(os.path.join(os.getcwd(), "GradeGuard_Output"))
        self.start_var.set("6000")
        self.spacing_var.set("25")
        self.height_var.set("0.50")
        self.curb_type_var.set("Standard curb (+0.50)")
        self.qa_latest_cad_var.set(True)
        self.qa_latest_pdf_var.set(True)
        self.qa_cad_units_var.set(True)
        self.qa_control_var.set(True)
        self.qa_benchmark_var.set(True)
        self.qa_pdf_trumps_cad_var.set(True)
        self.qa_curb_detail_var.set(True)
        self.qa_property_var.set(True)
        self.create_offset_var.set(False)
        self.offset_distance_var.set("3.00")
        self.offset_mode_var.set("away_from_flowline")
        self.offset_reverse_var.set(False)
        self.source_mode.set("surface")
        self.surface_interpret_var.set("auto")
        self.grade_grid_type_var.set("SUBGRADE")
        self.grade_grid_start_var.set("8000")
        self.grade_grid_spacing_var.set("25")
        self.grade_grid_subtract_var.set("0.50")
        self.anchor_path = os.path.join(os.getcwd(), "GradeGuard_Tick_Anchors.json")
        self.selected_curb_path = os.path.join(os.getcwd(), "GradeGuard_Selected_BC_Entities.json")
        self.layer_box.delete("1.0", "end")
        self.layer_box.insert("1.0", "New job started. Pick DXF, LandXML/XML, and output folder. If the curb layer is generic like P-CURB, use Pick True Back of Curb in DXF.")
        self.log_msg("New GradeGuard job started.")
        self.esw_start_var.set('9000')
        self.esw_spacing_var.set('25')
        self.esw_desc_var.set('SIDEWALK')
        self.esw_cross_slope_var.set('1.00')
        self.esw_width_var.set('5.00')
        self.esw_auto_width_var.set(True)
        self.esw_adjust_var.set('direct')
        self.flout_start_var.set('7000')
        self.flout_find_var.set('Both')
        self.flout_desc_format_var.set('Structure + Pipe Size + FL-OUT')
        self.flout_search_radius_var.set('120')

    def save_job(self):
        path = self.current_project_file
        if not path:
            path = filedialog.asksaveasfilename(title="Save GradeGuard Job", defaultextension=".ggjob.json", filetypes=[("GradeGuard Job", "*.ggjob.json"), ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = self._project_data()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.current_project_file = path
            self.project_var.set("GradeGuard Project: " + os.path.basename(path))
            self.log_msg("Saved job settings: " + path)
        except Exception as e:
            messagebox.showerror("Save Job Error", str(e))

    def open_job(self):
        path = filedialog.askopenfilename(title="Open GradeGuard Job", filetypes=[("GradeGuard Job", "*.ggjob.json"), ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._load_project_data(data)
            self.current_project_file = path
            self.project_var.set("GradeGuard Project: " + os.path.basename(path))
            self.log_msg("Opened job settings: " + path)
        except Exception as e:
            messagebox.showerror("Open Job Error", str(e))

    def pick_cad(self):
        p = filedialog.askopenfilename(title="Pick Civil CAD DXF", filetypes=[("DXF files", "*.dxf"), ("DWG files", "*.dwg"), ("All files", "*.*")])
        if p:
            self.cad_var.set(p)
            self.unitless_confirm_var.set(False)
            reset_generation_state()

    def pick_surface(self):
        p = filedialog.askopenfilename(title="Pick LandXML Surface", filetypes=[("LandXML/XML", "*.xml *.landxml"), ("All files", "*.*")])
        if p:
            self.surface_var.set(p)
            self.surface_name_var.set("")
            try:
                names = [el.attrib.get('name','') for el in ET.parse(p).getroot().iter() if _xml_local(el.tag) == 'Surface']
                if len(names) == 1:
                    self.surface_name_var.set(names[0])
                else:
                    self.log_msg("LandXML surface names: " + ", ".join(names))
                if hasattr(self, 'surface_name_combo'):
                    self.surface_name_combo.configure(values=names)
            except Exception as ex:
                self.log_msg("Surface selection could not be read: " + str(ex))

    def pick_pdf(self):
        p = filedialog.askopenfilename(title="Pick PDF plan set", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if p:
            self.pdf_var.set(p)

    def review_pdf_grades(self):
        if not os.path.isfile(self.pdf_var.get().strip()):
            messagebox.showerror('Read PDF grades', 'Choose a local PDF plan set first.')
            return
        try:
            from gradeguard_pdf_ui import PdfReviewWindow
            PdfReviewWindow(self)
        except Exception as ex:
            messagebox.showerror('Read PDF grades', str(ex))

    def pick_out(self):
        p = filedialog.askdirectory(title="Pick output folder")
        if p:
            self.out_var.set(p)

    def log_msg(self, msg):
        try:
            self.log.insert("end", str(msg) + "\n")
            self.log.see("end")
            self.update_idletasks()
        except Exception:
            pass

    def detect_layers(self):
        try:
            self.layer_box.delete("1.0", "end")
            cad = self.cad_var.get().strip()
            if not cad:
                self.layer_box.insert("end", "Pick a DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                self.layer_box.insert("end", "DWG selected. Export to DXF first so the program can read layers.\n")
                return
            if ezdxf is None:
                self.layer_box.insert("end", "ezdxf is not installed. Run install_requirements.bat first.\n")
                return
            doc = ezdxf.readfile(cad)
            layers = sorted([l.dxf.name for l in doc.layers])
            found = []
            for lay in layers:
                conf, meaning, auto = classify_curb_layer(lay)
                if conf in ("HIGH", "MEDIUM", "EXCLUDED") or auto:
                    found.append((lay, conf, meaning, auto))
            if not found:
                self.layer_box.insert("end", "No obvious curb layers found. You can still use Pick True Back of Curb in DXF. v7.6 shows all linework so generic layers like HARDSCAPE can be manually selected.\n")
            else:
                for lay, conf, meaning, auto in found:
                    marker = "USE" if auto else "CHECK"
                    self.layer_box.insert("end", f"{marker:5} | {conf:8} | {lay} | {meaning}\n")
        except Exception as e:
            self.layer_box.insert("end", "Layer detection error:\n" + str(e))


    def pick_back_of_curb_entities(self):
        """Simple DXF viewer for manually selecting the actual back-of-curb entities.
        This is intentionally basic and self-explanatory: click a line/polyline/arc to toggle it as BC.
        """
        try:
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing DXF", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first, then pick the BC objects.")
                return
            if ezdxf is None:
                messagebox.showerror("Missing ezdxf", "Run install_requirements.bat first.")
                return
            doc = ezdxf.readfile(cad)
            msp = doc.modelspace()
            keyword_text = getattr(self, "_picker_keywords_override", self.curb_layer_var.get())
            picker_save_path = getattr(self, "_picker_save_path_override", self.selected_curb_path)
            picker_title = getattr(self, "_picker_title_override", "Pick True Back of Curb Objects")
            picker_instruction = getattr(self, "_picker_instruction_override", "Click the actual BACK OF CURB linework. v7.6 shows all linework, including HARDSCAPE, SPLINE, ELLIPSE, CIRCLE, and block INSERT linework. Yellow = selected BC. Gray = other linework. Mouse wheel zooms. Right drag pans.")
            picker_saved_label = getattr(self, "_picker_saved_label_override", "Manual BC selection saved")
            keywords = [x.strip() for x in re.split(r"[,;]", keyword_text) if x.strip()]
            entities = []
            all_pts = []
            for ent in msp:
                typ = ent.dxftype()
                if typ not in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE", "ELLIPSE", "CIRCLE", "INSERT", "HATCH"):
                    continue
                layer = entity_layer(ent)
                # v6.5: show all selectable CAD linework in the manual picker.
                # Different engineers may put curb on generic layers like HARDSCAPE, SITE, PAVEMENT, etc.
                # Manual picking is the user's override, so do not hide valid linework just because the layer name is not a known curb name.
                n = normalize_layer_name(layer)
                show = True
                if not show:
                    continue
                try:
                    pts = picker_entity_points(ent, spacing=0.75)
                    if len(pts) < 2:
                        continue
                    h = str(getattr(ent.dxf, "handle", ""))
                    entities.append({"handle": h, "layer": layer, "type": typ, "pts": pts})
                    all_pts.extend(pts)
                except Exception:
                    continue
            if not entities:
                messagebox.showwarning("No curb graphics", "No LINE/POLYLINE/ARC/SPLINE/INSERT graphics found in this DXF. Check that the curb/hardscape linework was exported to DXF model space.")
                return
            all_layers = sorted({str(e.get("layer") or "0") for e in entities}, key=lambda x: x.lower())
            active_layers = set(all_layers)
            selected = set(load_selected_curb_handles(picker_save_path))
            win = tk.Toplevel(self)
            apply_gradeguard_icon(win)
            win.title(picker_title)
            win.geometry("1100x760")
            frame = ttk.Frame(win, padding=8)
            frame.pack(fill="both", expand=True)
            info = tk.StringVar(value=picker_instruction)
            ttk.Label(frame, textvariable=info).pack(fill="x")
            canvas = tk.Canvas(frame, bg="#111a24", highlightthickness=0)
            canvas.pack(fill="both", expand=True, pady=8)
            btns = ttk.Frame(frame)
            btns.pack(fill="x")
            def open_layer_filter():
                dlg = tk.Toplevel(win)
                apply_gradeguard_icon(dlg)
                dlg.title("DXF Layers On / Off")
                dlg.geometry("420x560")
                dlg.transient(win)
                outer = ttk.Frame(dlg, padding=10)
                outer.pack(fill="both", expand=True)
                ttk.Label(outer, text="Turn DXF layers on/off for this picker", font=("Arial", 11, "bold")).pack(anchor="w")
                ttk.Label(outer, text="GradeGuard read these layers directly from the DXF.", font=("Arial", 9, "italic")).pack(anchor="w", pady=(0,8))
                canvas_layers = tk.Canvas(outer, highlightthickness=0)
                scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas_layers.yview)
                layer_frame = ttk.Frame(canvas_layers)
                layer_frame.bind("<Configure>", lambda e: canvas_layers.configure(scrollregion=canvas_layers.bbox("all")))
                canvas_layers.create_window((0,0), window=layer_frame, anchor="nw")
                canvas_layers.configure(yscrollcommand=scroll.set)
                canvas_layers.pack(side="left", fill="both", expand=True)
                scroll.pack(side="right", fill="y")
                vars_by_layer = {}
                def sync_layer_visibility(refit=True):
                    active_layers.clear()
                    for lay, v in vars_by_layer.items():
                        if v.get():
                            active_layers.add(lay)
                    info.set(f"Visible DXF layers: {len(active_layers)} of {len(all_layers)}. Selected objects: {len(selected)}")
                    try:
                        if refit:
                            fit()
                        else:
                            draw()
                    except Exception:
                        try:
                            draw()
                        except Exception:
                            pass
                for lay in all_layers:
                    v = tk.BooleanVar(value=(lay in active_layers))
                    vars_by_layer[lay] = v
                    ttk.Checkbutton(layer_frame, text=lay, variable=v, command=lambda: sync_layer_visibility(refit=True)).pack(anchor="w")
                bottom = ttk.Frame(outer)
                bottom.pack(fill="x", pady=(8,0))
                def all_on():
                    for v in vars_by_layer.values(): v.set(True)
                    sync_layer_visibility(refit=True)
                def all_off():
                    for v in vars_by_layer.values(): v.set(False)
                    sync_layer_visibility(refit=True)
                def apply_layers():
                    sync_layer_visibility(refit=True)
                    dlg.destroy()
                ttk.Button(bottom, text="All On", command=all_on).pack(side="left")
                ttk.Button(bottom, text="All Off", command=all_off).pack(side="left", padx=6)
                ttk.Button(bottom, text="Close", style="Accent.TButton", command=apply_layers).pack(side="right")
            ttk.Button(btns, text="DXF Layers On/Off", command=open_layer_filter).pack(side="left", padx=(0,8))
            select_mode = tk.StringVar(value="smart")
            erase_mode = tk.BooleanVar(value=False)
            ttk.Label(btns, text="Pick mode:").pack(side="left", padx=(0,4))
            ttk.Radiobutton(btns, text="Smart chain", variable=select_mode, value="smart").pack(side="left")
            ttk.Radiobutton(btns, text="Single object", variable=select_mode, value="single").pack(side="left")
            ttk.Radiobutton(btns, text="All touching", variable=select_mode, value="all").pack(side="left")
            ttk.Checkbutton(btns, text="Unselect mode", variable=erase_mode).pack(side="left", padx=(12,8))
            minx = min(p[0] for p in all_pts); maxx = max(p[0] for p in all_pts)
            miny = min(p[1] for p in all_pts); maxy = max(p[1] for p in all_pts)
            state = {"scale": 1.0, "ox": 0.0, "oy": 0.0, "last": None}
            def fit():
                w = max(100, canvas.winfo_width()); h = max(100, canvas.winfo_height())
                visible_pts = []
                try:
                    for ee in entities:
                        if str(ee.get("layer") or "0") in active_layers:
                            visible_pts.extend(ee.get("pts") or [])
                except Exception:
                    visible_pts = []
                if not visible_pts:
                    visible_pts = all_pts
                vminx = min(p[0] for p in visible_pts); vmaxx = max(p[0] for p in visible_pts)
                vminy = min(p[1] for p in visible_pts); vmaxy = max(p[1] for p in visible_pts)
                dx = max(vmaxx-vminx, 1.0); dy = max(vmaxy-vminy, 1.0)
                state["scale"] = min((w-40)/dx, (h-40)/dy)
                state["ox"] = 20 - vminx*state["scale"]
                state["oy"] = 20 + vmaxy*state["scale"]
                draw()
            def to_screen(pt):
                x,y = pt
                return (x*state["scale"] + state["ox"], -y*state["scale"] + state["oy"])
            def to_world(sx, sy):
                return ((sx-state["ox"])/state["scale"], -(sy-state["oy"])/state["scale"])
            def draw():
                canvas.delete("all")
                for e in entities:
                    if str(e.get("layer") or "0") not in active_layers:
                        continue
                    pts = e["pts"]
                    color = "#ffd400" if e["handle"] in selected else "#8a96a3"
                    width = 3 if e["handle"] in selected else 1
                    scr=[]
                    for pnt in pts:
                        scr.extend(to_screen(pnt))
                    if len(scr)>=4:
                        canvas.create_line(*scr, fill=color, width=width)
                canvas.create_text(12, 12, anchor="nw", text=f"Selected BC objects: {len(selected)}", fill="#ffffff", font=("Arial", 12, "bold"))
            def point_seg_dist(pt, a, b):
                ax,ay=a; bx,by=b; px,py=pt
                vx,vy=bx-ax,by-ay; wx,wy=px-ax,py-ay
                c1=vx*wx+vy*wy; c2=vx*vx+vy*vy
                if c2<=1e-12: return dist(pt,a)
                t=max(0,min(1,c1/c2)); proj=(ax+t*vx, ay+t*vy)
                return dist(pt, proj)
            def nearest_entity(wx, wy):
                best=None; bestd=1e99
                pt=(wx,wy)
                for e in entities:
                    if str(e.get("layer") or "0") not in active_layers:
                        continue
                    pts=e["pts"]
                    for a,b in zip(pts[:-1], pts[1:]):
                        d=point_seg_dist(pt,a,b)
                        if d<bestd:
                            bestd=d; best=e
                return best, bestd
            def connected_group_all(seed, tol=0.75):
                """Select every entity touching the clicked object. Use only when the curb is not branched."""
                if seed is None:
                    return set()
                group = {seed["handle"]}
                queue = [seed]
                while queue and len(group) < 1000:
                    cur = queue.pop(0)
                    cur_ends = [cur["pts"][0], cur["pts"][-1]]
                    for other in entities:
                        oh = other["handle"]
                        if oh in group:
                            continue
                        if other.get("layer") != cur.get("layer"):
                            continue
                        other_ends = [other["pts"][0], other["pts"][-1]]
                        if any(dist(a,b) <= tol for a in cur_ends for b in other_ends):
                            group.add(oh)
                            queue.append(other)
                return group
            def _angle_deg(v1, v2):
                import math
                x1,y1=v1; x2,y2=v2
                n1=math.hypot(x1,y1); n2=math.hypot(x2,y2)
                if n1 <= 1e-9 or n2 <= 1e-9:
                    return 999.0
                c=max(-1.0,min(1.0,(x1*x2+y1*y2)/(n1*n2)))
                return math.degrees(math.acos(c))
            def connected_group_smart(seed, tol=0.75, max_angle=70.0):
                """Follow the smooth curb path from the clicked piece without jumping branches.

                This is for P-CURB / P-CURBS layers where face of curb and back of curb
                are on the same layer. It follows the best endpoint continuation, instead of
                selecting every object that touches the network.
                """
                if seed is None:
                    return set()
                group = {seed["handle"]}
                def extend(cur, side):
                    # side 0 extends from first point; side -1 extends from last point
                    while len(group) < 1000:
                        pts = cur["pts"]
                        if len(pts) < 2:
                            break
                        if side == 0:
                            shared = pts[0]; inside = pts[1]
                        else:
                            shared = pts[-1]; inside = pts[-2]
                        incoming = (shared[0]-inside[0], shared[1]-inside[1])
                        best = None; best_ang = 999.0; best_shared_idx = None
                        for other in entities:
                            oh = other["handle"]
                            if oh in group or other.get("layer") != cur.get("layer"):
                                continue
                            opts = other["pts"]
                            if len(opts) < 2:
                                continue
                            cand = []
                            if dist(shared, opts[0]) <= tol:
                                cand.append((0, opts[1]))
                            if dist(shared, opts[-1]) <= tol:
                                cand.append((-1, opts[-2]))
                            for idx, inner in cand:
                                outgoing = (inner[0]-shared[0], inner[1]-shared[1])
                                ang = _angle_deg(incoming, outgoing)
                                if ang < best_ang:
                                    best_ang = ang; best = other; best_shared_idx = idx
                        if best is None or best_ang > max_angle:
                            break
                        group.add(best["handle"])
                        # Continue from the opposite end of the added entity.
                        cur = best
                        side = -1 if best_shared_idx == 0 else 0
                extend(seed, 0)
                extend(seed, -1)
                return group
            def click(ev):
                wx,wy=to_world(ev.x, ev.y)
                e,d=nearest_entity(wx,wy)
                if e and d <= max(2.0, 12.0/state["scale"]):
                    h=e["handle"]
                    tol=max(0.25, min(1.0, 8.0/state["scale"]))
                    mode = select_mode.get()
                    if mode == "single":
                        group = {h}
                    elif mode == "all":
                        group = connected_group_all(e, tol=tol)
                    else:
                        group = connected_group_smart(e, tol=tol)
                    if erase_mode.get() or h in selected:
                        selected.difference_update(group)
                        action="Removed"
                    else:
                        selected.update(group)
                        action="Added"
                    info.set(f"{action}: {len(group)} object(s) from {e['layer']} using {mode} mode. Selected BC objects: {len(selected)}")
                    draw()
            def wheel(ev):
                factor = 1.15 if ev.delta > 0 else 1/1.15
                wx,wy=to_world(ev.x,ev.y)
                state["scale"]*=factor
                sx,sy=ev.x,ev.y
                state["ox"] = sx - wx*state["scale"]
                state["oy"] = sy + wy*state["scale"]
                draw()
            def start_pan(ev): state.update(last=(ev.x,ev.y))
            def pan(ev):
                if state["last"]:
                    lx,ly=state["last"]
                    state["ox"] += ev.x-lx; state["oy"] += ev.y-ly
                    state["last"]=(ev.x,ev.y); draw()
            def save_and_close():
                save_selected_curb_handles(picker_save_path, list(selected), cad)
                self.layer_box.insert("end", f"\n{picker_saved_label}: {len(selected)} entity objects. File: {picker_save_path}\n")
                win.destroy()
            def clear_selection():
                selected.clear(); draw(); info.set("Selection cleared. Click true back-of-curb linework.")
            canvas.bind("<Button-1>", click)
            canvas.bind("<MouseWheel>", wheel)
            canvas.bind("<ButtonPress-3>", start_pan)
            canvas.bind("<B3-Motion>", pan)
            ttk.Button(btns, text="Fit", command=fit).pack(side="left")
            ttk.Button(btns, text="Clear", command=clear_selection).pack(side="left", padx=8)
            ttk.Button(btns, text="Save Selected Back of Curb", style="Accent.TButton", command=save_and_close).pack(side="right")
            win.after(300, fit)
        except Exception as e:
            messagebox.showerror("Pick BC error", str(e))

    def pick_flowline_entities(self):
        """Simple DXF viewer for manually selecting the actual flowline/gutter entities.
        This is intentionally basic and self-explanatory: click a line/polyline/arc to toggle it as BC.
        """
        try:
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing DXF", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first, then pick the BC objects.")
                return
            if ezdxf is None:
                messagebox.showerror("Missing ezdxf", "Run install_requirements.bat first.")
                return
            doc = ezdxf.readfile(cad)
            msp = doc.modelspace()
            keywords = [x.strip() for x in re.split(r"[,;]", self.curb_layer_var.get()) if x.strip()]
            entities = []
            all_pts = []
            for ent in msp:
                typ = ent.dxftype()
                if typ not in ("LINE", "LWPOLYLINE", "POLYLINE", "ARC", "SPLINE", "ELLIPSE", "CIRCLE", "INSERT", "HATCH"):
                    continue
                layer = entity_layer(ent)
                # v6.5: show all selectable CAD linework in the manual picker.
                # Different engineers may put curb on generic layers like HARDSCAPE, SITE, PAVEMENT, etc.
                # Manual picking is the user's override, so do not hide valid linework just because the layer name is not a known curb name.
                n = normalize_layer_name(layer)
                show = True
                if not show:
                    continue
                try:
                    pts = picker_entity_points(ent, spacing=0.75)
                    if len(pts) < 2:
                        continue
                    h = str(getattr(ent.dxf, "handle", ""))
                    entities.append({"handle": h, "layer": layer, "type": typ, "pts": pts})
                    all_pts.extend(pts)
                except Exception:
                    continue
            if not entities:
                messagebox.showwarning("No curb graphics", "No LINE/POLYLINE/ARC/SPLINE/INSERT graphics found in this DXF. Check that the curb/hardscape linework was exported to DXF model space.")
                return
            all_layers = sorted({str(e.get("layer") or "0") for e in entities}, key=lambda x: x.lower())
            active_layers = set(all_layers)
            selected = set(load_selected_curb_handles(self.selected_flowline_path))
            win = tk.Toplevel(self)
            apply_gradeguard_icon(win)
            win.title("Pick Flowline / Gutter Objects")
            win.geometry("1100x760")
            frame = ttk.Frame(win, padding=8)
            frame.pack(fill="both", expand=True)
            info = tk.StringVar(value="Click the actual FLOWLINE / GUTTER linework. v7.6 shows all linework, including HARDSCAPE, SPLINE, ELLIPSE, CIRCLE, and block INSERT linework. Yellow = selected FL/gutter. Gray = other linework. Mouse wheel zooms. Right drag pans.")
            ttk.Label(frame, textvariable=info).pack(fill="x")
            canvas = tk.Canvas(frame, bg="#111a24", highlightthickness=0)
            canvas.pack(fill="both", expand=True, pady=8)
            btns = ttk.Frame(frame)
            btns.pack(fill="x")
            def open_layer_filter():
                dlg = tk.Toplevel(win)
                apply_gradeguard_icon(dlg)
                dlg.title("DXF Layers On / Off")
                dlg.geometry("420x560")
                dlg.transient(win)
                outer = ttk.Frame(dlg, padding=10)
                outer.pack(fill="both", expand=True)
                ttk.Label(outer, text="Turn DXF layers on/off for this picker", font=("Arial", 11, "bold")).pack(anchor="w")
                ttk.Label(outer, text="GradeGuard read these layers directly from the DXF.", font=("Arial", 9, "italic")).pack(anchor="w", pady=(0,8))
                canvas_layers = tk.Canvas(outer, highlightthickness=0)
                scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas_layers.yview)
                layer_frame = ttk.Frame(canvas_layers)
                layer_frame.bind("<Configure>", lambda e: canvas_layers.configure(scrollregion=canvas_layers.bbox("all")))
                canvas_layers.create_window((0,0), window=layer_frame, anchor="nw")
                canvas_layers.configure(yscrollcommand=scroll.set)
                canvas_layers.pack(side="left", fill="both", expand=True)
                scroll.pack(side="right", fill="y")
                vars_by_layer = {}
                def sync_layer_visibility(refit=True):
                    active_layers.clear()
                    for lay, v in vars_by_layer.items():
                        if v.get():
                            active_layers.add(lay)
                    info.set(f"Visible DXF layers: {len(active_layers)} of {len(all_layers)}. Selected objects: {len(selected)}")
                    try:
                        if refit:
                            fit()
                        else:
                            draw()
                    except Exception:
                        try:
                            draw()
                        except Exception:
                            pass
                for lay in all_layers:
                    v = tk.BooleanVar(value=(lay in active_layers))
                    vars_by_layer[lay] = v
                    ttk.Checkbutton(layer_frame, text=lay, variable=v, command=lambda: sync_layer_visibility(refit=True)).pack(anchor="w")
                bottom = ttk.Frame(outer)
                bottom.pack(fill="x", pady=(8,0))
                def all_on():
                    for v in vars_by_layer.values(): v.set(True)
                    sync_layer_visibility(refit=True)
                def all_off():
                    for v in vars_by_layer.values(): v.set(False)
                    sync_layer_visibility(refit=True)
                def apply_layers():
                    sync_layer_visibility(refit=True)
                    dlg.destroy()
                ttk.Button(bottom, text="All On", command=all_on).pack(side="left")
                ttk.Button(bottom, text="All Off", command=all_off).pack(side="left", padx=6)
                ttk.Button(bottom, text="Close", style="Accent.TButton", command=apply_layers).pack(side="right")
            ttk.Button(btns, text="DXF Layers On/Off", command=open_layer_filter).pack(side="left", padx=(0,8))
            select_mode = tk.StringVar(value="smart")
            erase_mode = tk.BooleanVar(value=False)
            ttk.Label(btns, text="Pick mode:").pack(side="left", padx=(0,4))
            ttk.Radiobutton(btns, text="Smart chain", variable=select_mode, value="smart").pack(side="left")
            ttk.Radiobutton(btns, text="Single object", variable=select_mode, value="single").pack(side="left")
            ttk.Radiobutton(btns, text="All touching", variable=select_mode, value="all").pack(side="left")
            ttk.Checkbutton(btns, text="Unselect mode", variable=erase_mode).pack(side="left", padx=(12,8))
            minx = min(p[0] for p in all_pts); maxx = max(p[0] for p in all_pts)
            miny = min(p[1] for p in all_pts); maxy = max(p[1] for p in all_pts)
            state = {"scale": 1.0, "ox": 0.0, "oy": 0.0, "last": None}
            def fit():
                w = max(100, canvas.winfo_width()); h = max(100, canvas.winfo_height())
                visible_pts = []
                try:
                    for ee in entities:
                        if str(ee.get("layer") or "0") in active_layers:
                            visible_pts.extend(ee.get("pts") or [])
                except Exception:
                    visible_pts = []
                if not visible_pts:
                    visible_pts = all_pts
                vminx = min(p[0] for p in visible_pts); vmaxx = max(p[0] for p in visible_pts)
                vminy = min(p[1] for p in visible_pts); vmaxy = max(p[1] for p in visible_pts)
                dx = max(vmaxx-vminx, 1.0); dy = max(vmaxy-vminy, 1.0)
                state["scale"] = min((w-40)/dx, (h-40)/dy)
                state["ox"] = 20 - vminx*state["scale"]
                state["oy"] = 20 + vmaxy*state["scale"]
                draw()
            def to_screen(pt):
                x,y = pt
                return (x*state["scale"] + state["ox"], -y*state["scale"] + state["oy"])
            def to_world(sx, sy):
                return ((sx-state["ox"])/state["scale"], -(sy-state["oy"])/state["scale"])
            def draw():
                canvas.delete("all")
                for e in entities:
                    if str(e.get("layer") or "0") not in active_layers:
                        continue
                    pts = e["pts"]
                    color = "#ffd400" if e["handle"] in selected else "#8a96a3"
                    width = 3 if e["handle"] in selected else 1
                    scr=[]
                    for pnt in pts:
                        scr.extend(to_screen(pnt))
                    if len(scr)>=4:
                        canvas.create_line(*scr, fill=color, width=width)
                canvas.create_text(12, 12, anchor="nw", text=f"Selected FL/gutter objects: {len(selected)}", fill="#ffffff", font=("Arial", 12, "bold"))
            def point_seg_dist(pt, a, b):
                ax,ay=a; bx,by=b; px,py=pt
                vx,vy=bx-ax,by-ay; wx,wy=px-ax,py-ay
                c1=vx*wx+vy*wy; c2=vx*vx+vy*vy
                if c2<=1e-12: return dist(pt,a)
                t=max(0,min(1,c1/c2)); proj=(ax+t*vx, ay+t*vy)
                return dist(pt, proj)
            def nearest_entity(wx, wy):
                best=None; bestd=1e99
                pt=(wx,wy)
                for e in entities:
                    if str(e.get("layer") or "0") not in active_layers:
                        continue
                    pts=e["pts"]
                    for a,b in zip(pts[:-1], pts[1:]):
                        d=point_seg_dist(pt,a,b)
                        if d<bestd:
                            bestd=d; best=e
                return best, bestd
            def connected_group_all(seed, tol=0.75):
                """Select every entity touching the clicked object. Use only when the curb is not branched."""
                if seed is None:
                    return set()
                group = {seed["handle"]}
                queue = [seed]
                while queue and len(group) < 1000:
                    cur = queue.pop(0)
                    cur_ends = [cur["pts"][0], cur["pts"][-1]]
                    for other in entities:
                        oh = other["handle"]
                        if oh in group:
                            continue
                        if other.get("layer") != cur.get("layer"):
                            continue
                        other_ends = [other["pts"][0], other["pts"][-1]]
                        if any(dist(a,b) <= tol for a in cur_ends for b in other_ends):
                            group.add(oh)
                            queue.append(other)
                return group
            def _angle_deg(v1, v2):
                import math
                x1,y1=v1; x2,y2=v2
                n1=math.hypot(x1,y1); n2=math.hypot(x2,y2)
                if n1 <= 1e-9 or n2 <= 1e-9:
                    return 999.0
                c=max(-1.0,min(1.0,(x1*x2+y1*y2)/(n1*n2)))
                return math.degrees(math.acos(c))
            def connected_group_smart(seed, tol=0.75, max_angle=70.0):
                """Follow the smooth curb path from the clicked piece without jumping branches.

                This is for P-CURB / P-CURBS layers where face of curb and back of curb
                are on the same layer. It follows the best endpoint continuation, instead of
                selecting every object that touches the network.
                """
                if seed is None:
                    return set()
                group = {seed["handle"]}
                def extend(cur, side):
                    # side 0 extends from first point; side -1 extends from last point
                    while len(group) < 1000:
                        pts = cur["pts"]
                        if len(pts) < 2:
                            break
                        if side == 0:
                            shared = pts[0]; inside = pts[1]
                        else:
                            shared = pts[-1]; inside = pts[-2]
                        incoming = (shared[0]-inside[0], shared[1]-inside[1])
                        best = None; best_ang = 999.0; best_shared_idx = None
                        for other in entities:
                            oh = other["handle"]
                            if oh in group or other.get("layer") != cur.get("layer"):
                                continue
                            opts = other["pts"]
                            if len(opts) < 2:
                                continue
                            cand = []
                            if dist(shared, opts[0]) <= tol:
                                cand.append((0, opts[1]))
                            if dist(shared, opts[-1]) <= tol:
                                cand.append((-1, opts[-2]))
                            for idx, inner in cand:
                                outgoing = (inner[0]-shared[0], inner[1]-shared[1])
                                ang = _angle_deg(incoming, outgoing)
                                if ang < best_ang:
                                    best_ang = ang; best = other; best_shared_idx = idx
                        if best is None or best_ang > max_angle:
                            break
                        group.add(best["handle"])
                        # Continue from the opposite end of the added entity.
                        cur = best
                        side = -1 if best_shared_idx == 0 else 0
                extend(seed, 0)
                extend(seed, -1)
                return group
            def click(ev):
                wx,wy=to_world(ev.x, ev.y)
                e,d=nearest_entity(wx,wy)
                if e and d <= max(2.0, 12.0/state["scale"]):
                    h=e["handle"]
                    tol=max(0.25, min(1.0, 8.0/state["scale"]))
                    mode = select_mode.get()
                    if mode == "single":
                        group = {h}
                    elif mode == "all":
                        group = connected_group_all(e, tol=tol)
                    else:
                        group = connected_group_smart(e, tol=tol)
                    if erase_mode.get() or h in selected:
                        selected.difference_update(group)
                        action="Removed"
                    else:
                        selected.update(group)
                        action="Added"
                    info.set(f"{action}: {len(group)} object(s) from {e['layer']} using {mode} mode. Selected FL/gutter objects: {len(selected)}")
                    draw()
            def wheel(ev):
                factor = 1.15 if ev.delta > 0 else 1/1.15
                wx,wy=to_world(ev.x,ev.y)
                state["scale"]*=factor
                sx,sy=ev.x,ev.y
                state["ox"] = sx - wx*state["scale"]
                state["oy"] = sy + wy*state["scale"]
                draw()
            def start_pan(ev): state.update(last=(ev.x,ev.y))
            def pan(ev):
                if state["last"]:
                    lx,ly=state["last"]
                    state["ox"] += ev.x-lx; state["oy"] += ev.y-ly
                    state["last"]=(ev.x,ev.y); draw()
            def save_and_close():
                save_selected_curb_handles(self.selected_flowline_path, list(selected), cad)
                self.layer_box.insert("end", f"\nManual Flowline/gutter selection saved: {len(selected)} entity objects. File: {self.selected_flowline_path}\n")
                win.destroy()
            def clear_selection():
                selected.clear(); draw(); info.set("Selection cleared. Click true flowline/gutter linework.")
            canvas.bind("<Button-1>", click)
            canvas.bind("<MouseWheel>", wheel)
            canvas.bind("<ButtonPress-3>", start_pan)
            canvas.bind("<B3-Motion>", pan)
            ttk.Button(btns, text="Fit", command=fit).pack(side="left")
            ttk.Button(btns, text="Clear", command=clear_selection).pack(side="left", padx=8)
            ttk.Button(btns, text="Save Selected Flowline/Gutter", style="Accent.TButton", command=save_and_close).pack(side="right")
            win.after(300, fit)
        except Exception as e:
            messagebox.showerror("Pick BC error", str(e))

    def pick_flout_centers(self):
        """Manual FL OUT picker. Use when Civil 3D structures do not export as readable blocks/circles.
        Click the structure center, then enter structure number, pipe size, and FL OUT elevation.
        """
        try:
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing DXF", "Pick the Site/Structure DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first, then pick structure centers.")
                return
            if ezdxf is None:
                messagebox.showerror("Missing ezdxf", "Run install_requirements.bat first.")
                return
            doc = ezdxf.readfile(cad)
            msp = doc.modelspace()
            entities=[]; all_pts=[]; segments=[]; snap_candidates=[]
            def add_snap_candidate(x, y, kind, layer=""):
                """Store a CAD-space snap candidate. De-dupe lightly so the picker is not crowded."""
                try:
                    q=(float(x), float(y), kind, layer)
                except Exception:
                    return
                for ex in snap_candidates:
                    if dist((q[0], q[1]), (ex[0], ex[1])) < 0.03 and ex[2] == kind:
                        return
                snap_candidates.append(q)
            for ent in msp:
                typ=ent.dxftype()
                layer=entity_layer(ent)
                pts=[]
                try:
                    if typ=="LINE":
                        pts=line_points(ent)
                        # Endpoints are useful when the structure is drawn as pipe walls meeting at a kink.
                        for x,y in pts:
                            add_snap_candidate(x, y, "kink/end", layer)
                    elif typ in ("LWPOLYLINE","POLYLINE"):
                        # Dense points draw curves, but original vertices become snap/kink candidates.
                        pts=polyline_points(ent, arc_spacing=4.0)
                        try:
                            if typ == "LWPOLYLINE":
                                for v in ent.get_points():
                                    add_snap_candidate(float(v[0]), float(v[1]), "kink/vertex", layer)
                            else:
                                for v in ent.vertices:
                                    loc=v.dxf.location
                                    add_snap_candidate(float(loc.x), float(loc.y), "kink/vertex", layer)
                        except Exception:
                            for x,y in pts[::max(1, len(pts)//20)]:
                                add_snap_candidate(x, y, "kink/vertex", layer)
                    elif typ=="ARC":
                        pts=arc_points_from_entity(ent, spacing=4.0)
                        # Start, mid, and end are handy for snapped field picks.
                        if pts:
                            add_snap_candidate(pts[0][0], pts[0][1], "arc end", layer)
                            add_snap_candidate(pts[len(pts)//2][0], pts[len(pts)//2][1], "arc mid", layer)
                            add_snap_candidate(pts[-1][0], pts[-1][1], "arc end", layer)
                    elif typ=="CIRCLE":
                        c=ent.dxf.center; r=float(ent.dxf.radius)
                        cx,cy=float(c.x),float(c.y)
                        pts=[(cx+math.cos(a)*r, cy+math.sin(a)*r) for a in [i*math.tau/36 for i in range(37)]]
                        # This is the big fix: a structure circle's staking point is its center, not the perimeter.
                        add_snap_candidate(cx, cy, "structure center", layer)
                    elif typ=="INSERT":
                        ins=ent.dxf.insert; x,y=float(ins.x),float(ins.y); pts=[(x-1,y),(x+1,y),(x,y-1),(x,y+1),(x-1,y)]
                        add_snap_candidate(x, y, "block insertion/center", layer)
                    elif typ in ("TEXT","MTEXT"):
                        ins=ent.dxf.insert; x,y=float(ins.x),float(ins.y); pts=[(x,y)]
                    else:
                        continue
                except Exception:
                    pts=[]
                if pts:
                    entities.append({"type":typ,"layer":layer,"pts":pts})
                    all_pts.extend(pts)
                    # Keep real geometry segments so the picker can snap to pipe/structure intersections.
                    if len(pts) >= 2 and typ not in ("TEXT", "MTEXT"):
                        for a,b in zip(pts[:-1], pts[1:]):
                            if dist(a,b) > 0.001:
                                segments.append((a,b,layer,typ))
            if not all_pts:
                messagebox.showwarning("No graphics", "No readable DXF graphics found.")
                return
            try:
                manual=load_manual_flout_points(self.manual_flout_path, cad)
            except ValueError as ex:
                manual=[]
                self.log_msg(str(ex) + ' Starting a fresh manual structure selection.')
            all_layers = sorted({str(e.get("layer") or "0") for e in entities}, key=lambda x: x.lower())
            active_layers = set(all_layers)
            win=tk.Toplevel(self)
            apply_gradeguard_icon(win)
            win.title("Pick Storm / Sanitary Structure Centers / Intersections")
            win.geometry("1150x780")
            frame=ttk.Frame(win, padding=8); frame.pack(fill="both", expand=True)
            info=tk.StringVar(value="Pick mode: EXACT CLICK stores the exact CAD coordinate you click. SNAP mode snaps to the nearest cyan structure center, intersection, vertex, or kink point.")
            ttk.Label(frame, textvariable=info).pack(fill="x")
            topbar=ttk.Frame(frame); topbar.pack(fill="x", pady=(4,0))
            pick_mode=tk.StringVar(value="exact")
            ttk.Label(topbar, text="Pick mode:", font=("Arial", 10, "bold")).pack(side="left")
            ttk.Radiobutton(topbar, text="Exact click / center", variable=pick_mode, value="exact").pack(side="left", padx=(8,0))
            ttk.Radiobutton(topbar, text="Snap to nearest center / intersection / kink", variable=pick_mode, value="intersection").pack(side="left", padx=(12,0))
            ttk.Label(topbar, text="Right-drag to pan. Mouse wheel to zoom.", font=("Arial", 9, "italic")).pack(side="left", padx=(18,0))
            def open_layer_filter():
                dlg = tk.Toplevel(win)
                apply_gradeguard_icon(dlg)
                dlg.title("DXF Layers On / Off")
                dlg.geometry("420x560")
                dlg.transient(win)
                outer = ttk.Frame(dlg, padding=10)
                outer.pack(fill="both", expand=True)
                ttk.Label(outer, text="Turn DXF layers on/off for storm/san picker", font=("Arial", 11, "bold")).pack(anchor="w")
                ttk.Label(outer, text="GradeGuard read these layers directly from the DXF.", font=("Arial", 9, "italic")).pack(anchor="w", pady=(0,8))
                canvas_layers = tk.Canvas(outer, highlightthickness=0)
                scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas_layers.yview)
                layer_frame = ttk.Frame(canvas_layers)
                layer_frame.bind("<Configure>", lambda e: canvas_layers.configure(scrollregion=canvas_layers.bbox("all")))
                canvas_layers.create_window((0,0), window=layer_frame, anchor="nw")
                canvas_layers.configure(yscrollcommand=scroll.set)
                canvas_layers.pack(side="left", fill="both", expand=True)
                scroll.pack(side="right", fill="y")
                vars_by_layer = {}
                def sync_layer_visibility(refit=True):
                    active_layers.clear()
                    for lay, v in vars_by_layer.items():
                        if v.get():
                            active_layers.add(lay)
                    info.set(f"Visible DXF layers: {len(active_layers)} of {len(all_layers)}. Manual FL OUT points: {len(manual)}")
                    try:
                        if refit:
                            fit()
                        else:
                            draw()
                    except Exception:
                        try:
                            draw()
                        except Exception:
                            pass
                for lay in all_layers:
                    v = tk.BooleanVar(value=(lay in active_layers))
                    vars_by_layer[lay] = v
                    ttk.Checkbutton(layer_frame, text=lay, variable=v, command=lambda: sync_layer_visibility(refit=True)).pack(anchor="w")
                bottom = ttk.Frame(outer)
                bottom.pack(fill="x", pady=(8,0))
                def all_on():
                    for v in vars_by_layer.values(): v.set(True)
                    sync_layer_visibility(refit=True)
                def all_off():
                    for v in vars_by_layer.values(): v.set(False)
                    sync_layer_visibility(refit=True)
                def apply_layers():
                    sync_layer_visibility(refit=True)
                    dlg.destroy()
                ttk.Button(bottom, text="All On", command=all_on).pack(side="left")
                ttk.Button(bottom, text="All Off", command=all_off).pack(side="left", padx=6)
                ttk.Button(bottom, text="Close", style="Accent.TButton", command=apply_layers).pack(side="right")
            ttk.Button(topbar, text="DXF Layers On/Off", command=open_layer_filter).pack(side="right")
            canvas=tk.Canvas(frame, bg="#111a24", highlightthickness=0); canvas.pack(fill="both", expand=True, pady=8)
            btns=ttk.Frame(frame); btns.pack(fill="x")
            minx=min(p[0] for p in all_pts); maxx=max(p[0] for p in all_pts); miny=min(p[1] for p in all_pts); maxy=max(p[1] for p in all_pts)
            state={"scale":1.0,"ox":0.0,"oy":0.0,"last":None}
            # Pre-compute candidate intersections from readable DXF geometry. These are only used
            # when the user selects Intersection Snap, and the final stored point is the exact
            # snapped CAD coordinate.
            def seg_intersection(a,b,c,d):
                ax,ay=a; bx,by=b; cx,cy=c; dx,dy=d
                r=(bx-ax, by-ay); svec=(dx-cx, dy-cy)
                den=r[0]*svec[1]-r[1]*svec[0]
                if abs(den) < 1e-9: return None
                q=(cx-ax, cy-ay)
                t=(q[0]*svec[1]-q[1]*svec[0])/den
                u=(q[0]*r[1]-q[1]*r[0])/den
                if -1e-7 <= t <= 1+1e-7 and -1e-7 <= u <= 1+1e-7:
                    return (ax+t*r[0], ay+t*r[1])
                return None
            # Snap candidates include true segment intersections PLUS structure centers, block
            # insertions, vertices, and kink/end points. This fixes structure picks where the
            # desired FL-OUT point is the center of the hourglass/structure graphic rather than a
            # clean line intersection.
            candidates=[]  # list of (x, y, kind, layer)
            max_segments=1800
            use_segments=segments[:max_segments]
            for i in range(len(use_segments)):
                a,b,lay1,typ1=use_segments[i]
                for j in range(i+1, len(use_segments)):
                    c,d,lay2,typ2=use_segments[j]
                    # quick bbox check
                    if max(a[0],b[0]) + 0.01 < min(c[0],d[0]) or max(c[0],d[0]) + 0.01 < min(a[0],b[0]):
                        continue
                    if max(a[1],b[1]) + 0.01 < min(c[1],d[1]) or max(c[1],d[1]) + 0.01 < min(a[1],b[1]):
                        continue
                    ip=seg_intersection(a,b,c,d)
                    if ip:
                        if not any(dist(ip, (q[0], q[1])) < 0.05 for q in candidates):
                            candidates.append((ip[0], ip[1], "intersection", (lay1 or lay2 or "")))
            for q in snap_candidates:
                if not any(dist((q[0], q[1]), (c[0], c[1])) < 0.05 for c in candidates):
                    candidates.append(q)
            def fit():
                w=max(100,canvas.winfo_width()); h=max(100,canvas.winfo_height())
                visible_pts=[]
                try:
                    for ee in entities:
                        if str(ee.get("layer") or "0") in active_layers:
                            visible_pts.extend(ee.get("pts") or [])
                except Exception:
                    visible_pts=[]
                if not visible_pts:
                    visible_pts=all_pts
                vminx=min(p[0] for p in visible_pts); vmaxx=max(p[0] for p in visible_pts)
                vminy=min(p[1] for p in visible_pts); vmaxy=max(p[1] for p in visible_pts)
                dx=max(vmaxx-vminx,1.0); dy=max(vmaxy-vminy,1.0)
                state["scale"]=min((w-40)/dx,(h-40)/dy)
                state["ox"]=20-vminx*state["scale"]; state["oy"]=20+vmaxy*state["scale"]
                draw()
            def to_screen(pt):
                x,y=pt; return (x*state["scale"]+state["ox"], -y*state["scale"]+state["oy"])
            def to_world(sx,sy): return ((sx-state["ox"])/state["scale"], -(sy-state["oy"])/state["scale"])
            def draw():
                canvas.delete("all")
                for e in entities:
                    if str(e.get("layer") or "0") not in active_layers:
                        continue
                    pts=e["pts"]; scr=[]
                    for pnt in pts: scr.extend(to_screen(pnt))
                    color="#667481"
                    if is_structure_layer_or_name(e.get("layer",""), self.flout_find_var.get()): color="#9bb3c8"
                    if len(scr)>=4: canvas.create_line(*scr, fill=color, width=1)
                    elif len(scr)==2: canvas.create_oval(scr[0]-1,scr[1]-1,scr[0]+1,scr[1]+1, fill=color, outline=color)
                # Show intersection candidates only in snap mode so the screen does not get cluttered.
                if pick_mode.get()=="intersection":
                    for q in candidates:
                        if str(q[3] or "0") not in active_layers:
                            continue
                        sx,sy=to_screen((q[0], q[1]))
                        if -20 <= sx <= canvas.winfo_width()+20 and -20 <= sy <= canvas.winfo_height()+20:
                            r = 4 if q[2] in ("structure center", "block insertion/center") else 3
                            fill = "#00d7ff" if q[2] in ("structure center", "block insertion/center") else ""
                            canvas.create_oval(sx-r,sy-r,sx+r,sy+r, outline="#00d7ff", fill=fill)
                for item in manual:
                    try: sx,sy=to_screen((float(item["x"]), float(item["y"])))
                    except Exception: continue
                    canvas.create_oval(sx-5,sy-5,sx+5,sy+5, fill="#ffd400", outline="#000000")
                    canvas.create_line(sx-9,sy,sx+9,sy, fill="#ffd400", width=2)
                    canvas.create_line(sx,sy-9,sx,sy+9, fill="#ffd400", width=2)
                    lab=(item.get("label") or "") + " " + (item.get("pipe") or "") + " " + str(item.get("elev") or "")
                    canvas.create_text(sx+10,sy-10, anchor="sw", text=lab.strip(), fill="#ffd400", font=("Arial",9,"bold"))
                canvas.create_text(12,12,anchor="nw",text=f"Manual FL OUT points: {len(manual)}  |  Snap points found: {len(candidates)}  |  Mode: {pick_mode.get().upper()}",fill="#ffffff",font=("Arial",12,"bold"))
            def ask_for_point(x,y, pick_source="Exact clicked center"):
                dlg=tk.Toplevel(win); apply_gradeguard_icon(dlg); dlg.title("Manual FL OUT Point"); dlg.geometry("460x310"); dlg.transient(win); dlg.grab_set()
                frm=ttk.Frame(dlg,padding=14); frm.pack(fill="both",expand=True)
                ttk.Label(frm,text=f"Picked CAD coordinate: N {y:.3f}, E {x:.3f}", font=("Arial", 10, "bold")).grid(row=0,column=0,sticky="w",pady=(0,4))
                ttk.Label(frm,text=f"Pick source: {pick_source}").grid(row=1,column=0,sticky="w",pady=(0,10))
                ttk.Label(frm,text="Structure number, such as AI-1, STMH-2, SSMH-1").grid(row=2,column=0,sticky="w")
                lab=tk.StringVar(); ttk.Entry(frm,textvariable=lab,width=28).grid(row=3,column=0,sticky="we",pady=(2,8))
                ttk.Label(frm,text='Pipe size, such as 18" or 8"').grid(row=4,column=0,sticky="w")
                pipe=tk.StringVar(); ttk.Entry(frm,textvariable=pipe,width=28).grid(row=5,column=0,sticky="we",pady=(2,8))
                ttk.Label(frm,text="FL OUT elevation").grid(row=6,column=0,sticky="w")
                elev=tk.StringVar(); ttk.Entry(frm,textvariable=elev,width=28).grid(row=7,column=0,sticky="we",pady=(2,10))
                def add():
                    try:
                        z=float(elev.get())
                    except Exception:
                        messagebox.showerror("Elevation required", "Enter a numeric FL OUT elevation.", parent=dlg); return
                    manual.append({"x":x,"y":y,"label":lab.get().strip(),"pipe":pipe.get().strip(),"elev":z,"pick_source":pick_source})
                    save_manual_flout_points(self.manual_flout_path, manual, cad)
                    info.set(f"Added manual FL OUT point at N {y:.3f}, E {x:.3f}. Total: {len(manual)}")
                    dlg.destroy(); draw()
                ttk.Button(frm,text="Add Manual FL OUT Point",style="Accent.TButton",command=add).grid(row=8,column=0,sticky="e")
            def click(ev):
                wx,wy=to_world(ev.x, ev.y)
                if pick_mode.get()=="intersection" and candidates:
                    best=None; bd=999999
                    for q in candidates:
                        if str(q[3] or "0") not in active_layers:
                            continue
                        sx,sy=to_screen((q[0], q[1])); d=((sx-ev.x)**2+(sy-ev.y)**2)**0.5
                        if d<bd: best=q; bd=d
                    if best is not None and bd <= 30:
                        ask_for_point(best[0], best[1], f"Snapped to nearest {best[2]}")
                        return
                    messagebox.showwarning("No snap point close enough", "Click closer to a cyan snap point, structure center, intersection, or kink. You can also switch to Exact click / center.", parent=win)
                    return
                ask_for_point(wx, wy, "Exact clicked center")
            def wheel(ev):
                factor=1.15 if ev.delta>0 else 1/1.15; wx,wy=to_world(ev.x,ev.y); state["scale"]*=factor; state["ox"]=ev.x-wx*state["scale"]; state["oy"]=ev.y+wy*state["scale"]; draw()
            def start_pan(ev): state.update(last=(ev.x,ev.y))
            def pan(ev):
                if state["last"]:
                    lx,ly=state["last"]; state["ox"]+=ev.x-lx; state["oy"]+=ev.y-ly; state["last"]=(ev.x,ev.y); draw()
            def clear():
                if messagebox.askyesno("Clear manual FL OUT points", "Clear all manual FL OUT points?", parent=win):
                    manual.clear(); save_manual_flout_points(self.manual_flout_path, manual, cad); draw(); info.set("Manual FL OUT points cleared.")
            canvas.bind("<Button-1>", click); canvas.bind("<MouseWheel>", wheel); canvas.bind("<ButtonPress-3>", start_pan); canvas.bind("<B3-Motion>", pan)
            pick_mode.trace_add("write", lambda *_: draw())
            ttk.Button(btns,text="Fit",command=fit).pack(side="left")
            ttk.Button(btns,text="Clear Manual Points",command=clear).pack(side="left",padx=8)
            ttk.Button(btns,text="Done",style="Accent.TButton",command=win.destroy).pack(side="right")
            win.after(300, fit)
        except Exception as e:
            messagebox.showerror("Pick FL OUT error", str(e))

    def generate_flout_points(self):
        try:
            self.log.delete("1.0", "end")
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing CAD", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first. The DXF stays fixed and is never scaled.")
                return
            self.log_msg("Generating storm/sanitary FL OUT points...")
            csv_path, debug_path, total, assigned, review = run_flout_generation(
                cad_path=cad,
                pdf_path=self.pdf_var.get().strip(),
                out_dir=self.out_var.get().strip(),
                start_pt=int(float(self.flout_start_var.get())),
                find_mode=self.flout_find_var.get(),
                desc_format=self.flout_desc_format_var.get(),
                search_radius=float(self.flout_search_radius_var.get()),
                manual_path=self.manual_flout_path,
                units_confirmed=bool(self.unitless_confirm_var.get()),
            )
            self.log_msg(f"Generated FL OUT points: {total}")
            self.log_msg(f"Points with elevations: {assigned}")
            self.log_msg(f"Review items: {review}")
            self.log_msg(f"CSV: {csv_path}")
            self.log_msg(f"QA/source CSV: {debug_path}")
            messagebox.showinfo("Done", f"Generated {total} FL OUT points.\nPoints with elevations: {assigned}\nReview items: {review}\n\n{csv_path}")
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("FL OUT Error", str(e))


    def pick_edge_sidewalk_entities(self):
        """Use the proven geometry picker for edge-of-sidewalk objects without changing BC selection."""
        self._picker_save_path_override = self.selected_sidewalk_path
        self._picker_keywords_override = "SIDEWALK, P-SIDEWALK, EDGE OF SIDEWALK, CONCRETE SIDEWALK, SIDEWALK EDGE, WALK, P-WALK, ESW, E-SW, SWLK, S-WALK, SW"
        self._picker_title_override = "Pick Edge of Sidewalk Objects"
        self._picker_instruction_override = "Click the actual EDGE OF SIDEWALK linework. GradeGuard shows any DXF layer containing SIDEWALK, plus common sidewalk abbreviations. Yellow = selected sidewalk edge. Gray = other sidewalk/walk linework. Mouse wheel zooms. Right drag pans."
        self._picker_saved_label_override = "Manual edge-of-sidewalk selection saved"
        try:
            self.pick_back_of_curb_entities()
        finally:
            for name in ["_picker_save_path_override", "_picker_keywords_override", "_picker_title_override", "_picker_instruction_override", "_picker_saved_label_override"]:
                try:
                    delattr(self, name)
                except Exception:
                    pass

    def generate_edge_sidewalk_points(self):
        try:
            self.log.delete("1.0", "end")
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing CAD", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first. The DXF stays fixed and is never scaled.")
                return
            self.log_msg("Generating edge-of-sidewalk staking points...")
            csv_path, debug_path, report_path, total, assigned, missing = run_edge_sidewalk_generation(
                cad_path=cad,
                pdf_path=self.pdf_var.get().strip(),
                surface_path=self.surface_var.get().strip(),
                surface_name=self.surface_name_var.get().strip(),
                units_confirmed=bool(self.unitless_confirm_var.get()),
                out_dir=self.out_var.get().strip(),
                start_pt=int(float(self.esw_start_var.get())),
                spacing=float(self.esw_spacing_var.get()),
                description=self.esw_desc_var.get().strip() or "SIDEWALK",
                elev_layer_keywords=self.elev_layer_var.get(),
                contour_radius=float(self.contour_radius_var.get()),
                selected_sidewalk_path=self.selected_sidewalk_path,
                cross_slope_percent=float(self.esw_cross_slope_var.get() or 0.0),
                sidewalk_width=float(self.esw_width_var.get() or 0.0),
                cross_slope_adjust=self.esw_adjust_var.get(),
                auto_detect_width=bool(self.esw_auto_width_var.get()),
            )
            self.log_msg(f"Generated edge-of-sidewalk points: {total}")
            self.log_msg(f"Points with elevations: {assigned}")
            self.log_msg(f"Missing elevations: {missing}")
            self.log_msg(f"CSV: {csv_path}")
            self.log_msg(f"Debug/source CSV: {debug_path}")
            self.log_msg(f"Report: {report_path}")
            messagebox.showinfo("Done", f"Generated {total} edge-of-sidewalk points.\nPoints with elevations: {assigned}\nMissing elevations: {missing}\n\n{csv_path}")
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("Edge Sidewalk Error", str(e))

    def generate_grade_grid(self):
        try:
            self.log.delete("1.0", "end")
            cad = self.cad_var.get().strip()
            if not cad:
                messagebox.showerror("Missing CAD", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first. The DXF stays fixed and is never scaled.")
                return
            grid_type = self.grade_grid_type_var.get().strip().upper()
            desc = "SUBGRADE" if grid_type == "SUBGRADE" else "FINISHED GRADE"
            subtract = float(self.grade_grid_subtract_var.get()) if desc == "SUBGRADE" else 0.0
            self.log_msg(f"Generating {desc} grid points...")
            csv_path, debug_path, total, assigned, missing = run_grade_grid_generation(
                cad_path=cad,
                surface_path=self.surface_var.get().strip(),
                surface_name=self.surface_name_var.get().strip(),
                units_confirmed=bool(self.unitless_confirm_var.get()),
                out_dir=self.out_var.get().strip(),
                start_pt=int(float(self.grade_grid_start_var.get())),
                grid_spacing=float(self.grade_grid_spacing_var.get()),
                subtract_amount=subtract,
                description=desc,
                elev_layer_keywords=self.elev_layer_var.get(),
                contour_radius=float(self.contour_radius_var.get()),
                selected_curb_path=self.selected_curb_path,
            )
            self.log_msg(f"Generated {total} {desc} grid points.")
            self.log_msg(f"Points with elevations: {assigned}")
            self.log_msg(f"Points missing elevations: {missing}")
            self.log_msg(f"CSV: {csv_path}")
            self.log_msg(f"Debug/source CSV: {debug_path}")
            messagebox.showinfo("Done", f"Generated {total} {desc} grid points.\nPoints with elevations: {assigned}\nMissing elevations: {missing}\n\n{csv_path}")
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("Grade Grid Error", str(e))

    def generate_with_offset(self):
        self.create_offset_var.set(True)
        self.generate()

    def generate(self):
        try:
            if self._generation_running:
                self.log_msg("BC generation is already running. Please wait for it to finish.")
                return
            self.log.delete("1.0", "end")
            cad = self.cad_var.get().strip()
            out = self.out_var.get().strip()
            if not cad:
                messagebox.showerror("Missing CAD", "Pick a Civil CAD DXF first.")
                return
            if cad.lower().endswith(".dwg"):
                messagebox.showwarning("Use DXF", "Export the DWG to DXF first. The DXF stays fixed and is never scaled.")
                return
            pdf = self.pdf_var.get().strip()
            if not pdf or not os.path.exists(pdf) or not pdf.lower().endswith(".pdf"):
                messagebox.showerror("Civil PDF Required", "Load the certified civil PDF plan set before generating BC points. PDF callouts and curb details hold over CAD/DXF.")
                return
            surface = self.surface_var.get().strip()
            mode = self.source_mode.get()
            if mode == "surface" and not surface:
                if not messagebox.askyesno("No LandXML selected", "No LandXML surface was selected. Generate using DXF surface/contours as fallback?"):
                    return
            self.log_msg(f"Running Schoel GradeGuard v{APP_VERSION} with automatic QA/QC...")
            self.log_msg("Automatic QA/QC active: PDF required, PDF controls over CAD/DXF, HP/LP protected, and unsupported elevations flagged in the review report.")
            selected_curb_height, selected_curb_height_verified, selected_curb_height_note = self._selected_curb_height_and_note()
            self.height_var.set(f"{selected_curb_height:.2f}")
            if not self._confirm_preflight(cad, pdf, surface, out, selected_curb_height, selected_curb_height_note):
                self.log_msg("Generation cancelled during preflight review.")
                return
            self.log_msg(selected_curb_height_note)
            self.log_msg("DXF is fixed. Surface/contour interpretation: " + self.surface_interpret_var.get())
            self.log_msg("Picked BC file: " + str(self.selected_curb_path))
            self.log_msg("Picked FL/gutter file: " + str(self.selected_flowline_path))
            anchor_path = self.anchor_path
            generation_args = dict(
                cad_path=cad,
                pdf_path=self.pdf_var.get().strip(),
                out_dir=out,
                start_pt=int(float(self.start_var.get())),
                spacing=float(self.spacing_var.get()),
                default_curb_height=float(selected_curb_height),
                curb_layer_keywords=self.curb_layer_var.get(),
                elev_layer_keywords=self.elev_layer_var.get(),
                label_radius=float(self.label_radius_var.get()),
                contour_radius=float(self.contour_radius_var.get()),
                min_slope=float(self.min_slope_var.get()),
                add_curb_height_to_contours=False,
                contour_check_tolerance=float(self.contour_tolerance_var.get()),
                manual_anchor_path=anchor_path,
                require_manual_pdf_spots=True,
                surface_path=surface,
                source_policy=self.source_mode.get(),
                surface_name=self.surface_name_var.get().strip(),
                units_confirmed=bool(self.unitless_confirm_var.get()),
                surface_mode=self.surface_interpret_var.get(),
                selected_curb_path=self.selected_curb_path,
                selected_flowline_path=self.selected_flowline_path,
                create_offset=bool(self.create_offset_var.get()),
                offset_distance=float(self.offset_distance_var.get()),
                offset_mode=self.offset_mode_var.get(),
                offset_reverse=bool(self.offset_reverse_var.get()),
            )
            self._generation_running = True
            self.generate_button.configure(state="disabled", text="GENERATING... PLEASE WAIT")
            self.config(cursor="watch")
            self.log_msg("Generation is running in the background. The window will remain responsive on slower computers.")
            threading.Thread(
                target=self._run_generation_worker,
                args=(generation_args, out),
                daemon=True,
                name="GradeGuardGeneration",
            ).start()
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("Error", str(e))

    def _run_generation_worker(self, generation_args, out):
        """Run CPU/file-heavy generation away from Tk's event loop."""
        try:
            result = run_generation(**generation_args)
            self.after(0, self._generation_finished, result, out)
        except Exception as e:
            details = traceback.format_exc()
            self.after(0, self._generation_failed, str(e), details)

    def _reset_generation_ui(self):
        self._generation_running = False
        self.generate_button.configure(state="normal", text="Review & generate BC points")
        self._workflow.set_busy(False)
        self.config(cursor="")

    def _generation_finished(self, result, out):
        review = os.path.join(out, 'Calculation-Review.json')
        self.last_calculation_review = review if os.path.isfile(review) else ''
        self._reset_generation_ui()
        self._workflow.show_results()
        csv_path, debug_csv_path, report_path, total, issues, assigned = result
        try:
            self.log_msg(f"Generated points: {total}")
            self.log_msg(f"Points with elevations: {assigned}")
            self.log_msg(f"Review items: {issues}")
            self.log_msg(f"CSV: {csv_path}")
            self.log_msg(f"Debug/source CSV: {debug_csv_path}")
            self.log_msg(f"Report: {report_path}")
            flow_pdf_path = os.path.join(out, "GradeGuard_BC_Water_Flow_Map.pdf")
            if os.path.exists(flow_pdf_path):
                self.log_msg(f"Water flow PDF: {flow_pdf_path}")
            messagebox.showinfo("Done", f"Generated {total} BC points.\nStake-ready points exported: {assigned}\nReview items: {issues}\n\nUnsafe points were withheld from the field CSV and written to GradeGuard_DO_NOT_STAKE.csv.\n\nCreated Survey Package outputs in the same folder:\n- Confidence Summary\n- Surface Conflict Report\n- Water Flow Map\n- Office QA Report\n\n{csv_path}")
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("Error", str(e))

    def _generation_failed(self, error, details):
        self._reset_generation_ui()
        self._workflow.show_results()
        self.log_msg("ERROR:")
        self.log_msg(error)
        self.log_msg(details)
        messagebox.showerror("Error", error)

if __name__ == "__main__":
    app = GradeGuardApp(show_splash=True)
    app.mainloop()
