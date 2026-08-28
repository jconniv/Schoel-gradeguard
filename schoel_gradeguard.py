import os
import sys
import re
import math
import csv
import json
import traceback
import base64
import ctypes
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
APP_VERSION = "9.0.4"

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
        (r"\bB\.?\s*O\.?\s*C\.?\s*=?\s*([0-9]{2,4}\.[0-9]{1,3})", "BC"),
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
        pts = [(float(v.x), float(v.y)) for v in tool.vertices(list(tool.angles(n)))]
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
    h_sq = max(r * r - (chord / 2.0) ** 2, 0.0)
    h = math.sqrt(h_sq)
    # For positive bulge, center is to the left of the chord from start to end.
    # For negative bulge, center is to the right.
    sign = 1.0 if bulge > 0 else -1.0
    cx, cy = mx + sign * nx * h, my + sign * ny * h
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
    return pts


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
                for v in ent.vertices(params=[i/72 for i in range(73)]):
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
    """Return PC, MID, PT for a true ARC entity in WCS."""
    dense = arc_points_from_entity(ent, spacing=1.0)
    if len(dense) >= 2:
        mid = _point_at_half_station(dense) or dense[len(dense)//2]
        return [dense[0], mid, dense[-1]]
    center = (float(ent.dxf.center.x), float(ent.dxf.center.y))
    r = float(ent.dxf.radius)
    start = math.radians(float(ent.dxf.start_angle))
    end = math.radians(float(ent.dxf.end_angle))
    if end < start:
        end += math.tau
    mid = (start + end) / 2.0
    return [
        (center[0] + r * math.cos(start), center[1] + r * math.sin(start)),
        (center[0] + r * math.cos(mid), center[1] + r * math.sin(mid)),
        (center[0] + r * math.cos(end), center[1] + r * math.sin(end)),
    ]


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
                dense = bulge_segment_points(a, b, bulge, spacing=max(0.5, min(2.0, spacing/8.0)))
                mid = _point_at_half_station(dense) or lerp(a, b, 0.5)
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
                dense = bulge_segment_points(a, b, bulge, spacing=max(0.5, min(2.0, spacing/8.0)))
                mid = _point_at_half_station(dense) or lerp(a, b, 0.5)
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

    return dedupe_points(stake), dedupe_points(controls)

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
    return pcs, pts


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
    for attr in ("elevation", "thickness"):
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
    txt = (text or '').strip()
    if not txt:
        return None
    vals = []
    for v in re.split(r'[,\s]+', txt):
        if not v.strip():
            continue
        try:
            vals.append(float(v))
        except Exception:
            return None
    if len(vals) < 3:
        return None
    a, b, z = vals[0], vals[1], vals[2]
    # LandXML files from Civil 3D commonly write northing easting elevation for Alabama State Plane exports.
    # GradeGuard stores XY as easting, northing. Use the larger coordinate as northing when obvious.
    if abs(a) > abs(b) * 1.5:
        n, e = a, b
    elif abs(b) > abs(a) * 1.5:
        e, n = a, b
    else:
        e, n = a, b
    return float(e), float(n), float(z)


def load_landxml_surface(path: str) -> Optional[LandXMLSurface]:
    """Read Civil 3D/LandXML data.

    GradeGuard supports both of these common Civil 3D exports:
    1) A true TIN surface with <Surfaces>/<Definition>/<Pnts>/<P> and <Faces>/<F>.
    2) A grading XML that contains <PlanFeatures>/<PlanFeature>/<CoordGeom> feature lines.

    The second case is what many grading files actually contain. Older GradeGuard builds only
    looked for TIN faces, so they returned no elevations from these files.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        raise RuntimeError(f"Could not read LandXML/XML surface: {e}")

    # --- True LandXML TIN surface ---
    pnts: Dict[str, Tuple[float, float, float]] = {}
    for el in root.iter():
        if _xml_local(el.tag).lower() == 'p':
            pid = el.attrib.get('id') or el.attrib.get('ID')
            xyz = _parse_landxml_xyz(el.text or '')
            if not pid or xyz is None:
                continue
            e, n, z = xyz
            if valid_grade_z(z):
                pnts[str(pid)] = (float(e), float(n), float(z))
    faces = []
    for el in root.iter():
        if _xml_local(el.tag).lower() == 'f':
            ids = [v for v in re.split(r'[,\s]+', (el.text or '').strip()) if v]
            if len(ids) >= 3 and all(i in pnts for i in ids[:3]):
                faces.append((pnts[ids[0]], pnts[ids[1]], pnts[ids[2]]))

    # --- Civil 3D PlanFeatures / FeatureLines ---
    feature_segments: List[ElevSegment] = []
    feature_count = 0
    for pf in root.iter():
        if _xml_local(pf.tag).lower() != 'planfeature':
            continue
        feature_count += 1
        layer = 'LandXML FeatureLine'
        try:
            for prop in pf.iter():
                if _xml_local(prop.tag).lower() == 'property' and (prop.attrib.get('label','').lower() == 'layer'):
                    layer = prop.attrib.get('value') or layer
        except Exception:
            pass
        for geom in pf.iter():
            if _xml_local(geom.tag).lower() == 'line':
                start = end = None
                for child in geom:
                    lname = _xml_local(child.tag).lower()
                    if lname == 'start':
                        start = _parse_landxml_xyz(child.text or '')
                    elif lname == 'end':
                        end = _parse_landxml_xyz(child.text or '')
                if start and end and valid_grade_z(start[2]) and valid_grade_z(end[2]):
                    feature_segments.append(ElevSegment((start[0], start[1]), (end[0], end[1]), start[2], layer, f"LandXML FeatureLine {layer}", end[2]))
            elif _xml_local(geom.tag).lower() == 'curve':
                start = end = center = None
                for child in geom:
                    lname = _xml_local(child.tag).lower()
                    if lname == 'start':
                        start = _parse_landxml_xyz(child.text or '')
                    elif lname == 'end':
                        end = _parse_landxml_xyz(child.text or '')
                    elif lname == 'center':
                        xy = [float(v) for v in re.split(r'[,\s]+', (child.text or '').strip()) if v.strip()]
                        if len(xy) >= 2:
                            # Center has no Z. Use same coordinate order heuristic.
                            a,b = xy[0], xy[1]
                            if abs(a) > abs(b)*1.5:
                                center = (b,a)
                            elif abs(b) > abs(a)*1.5:
                                center = (a,b)
                            else:
                                center = (a,b)
                if start and end and valid_grade_z(start[2]) and valid_grade_z(end[2]):
                    # Approximate curves with a short chord for elevation sampling. Geometry remains from the DXF BC layer.
                    feature_segments.append(ElevSegment((start[0], start[1]), (end[0], end[1]), start[2], layer, f"LandXML FeatureLine curve {layer}", end[2]))

    if not faces and not feature_segments:
        return None
    src = os.path.basename(path)
    if feature_segments and not faces:
        src += f" ({len(feature_segments)} feature-line segments)"
    elif feature_segments:
        src += f" ({len(faces)} TIN faces, {len(feature_segments)} feature-line segments)"
    return LandXMLSurface(faces, feature_segments, source=src)

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
        if l1 >= -0.005 and l2 >= -0.005 and l3 >= -0.005:
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
        z = float(z)
        # Project elevations vary by job. Reject obvious zeros/placeholders but allow normal site elevations.
        return abs(z) > 1e-6 and -10000.0 <= z <= 10000.0
    except Exception:
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


def detect_curb_layers(dxf_path: str, keywords: Optional[List[str]] = None) -> List[Dict[str, str]]:
    if ezdxf is None:
        raise RuntimeError("ezdxf is not installed. Double click install_requirements.bat first.")
    doc = ezdxf.readfile(dxf_path)
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



def load_selected_curb_handles(path: str) -> List[str]:
    """Read manually picked back-of-curb entity handles.
    When this list exists, GradeGuard ignores automatic curb-layer detection for geometry
    and uses only these entities as BC. This is critical when one layer, such as P-CURB,
    contains face of curb, gutter, and back of curb linework.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return [str(x) for x in data.get("handles", [])]
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        pass
    return []


def save_selected_curb_handles(path: str, handles: List[str], dxf_path: str = ""):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "version": "8.5.5",
            "purpose": "Manually selected DXF entity handles. Used for BC or FL/gutter depending on selection file.",
            "dxf_path": dxf_path,
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
    stats["curb_layer_candidates"] = detect_curb_layers(dxf_path, curb_keywords)

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
                    z2 = float(ent.dxf.end.z) if valid_grade_z(ent.dxf.end.z) else z1
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
    global CONTOUR_POINT_OVERRIDES
    CONTOUR_POINT_OVERRIDES[point_override_key(p)] = (float(elev), src, conf, issue)

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
            q, d = _nearest_point_on_curb((lab.x, lab.y), max_dist=max_label_to_curb_dist)
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
    """Detect likely HP and LP grade breaks after elevations are assigned.

    This does not replace plan callouts. It helps GradeGuard understand curb flow
    by recognizing where generated grades rise then fall (HP) or fall then rise
    (LP) along nearby consecutive BC points. These locations are tagged in the
    source/issue column so they are visible in the debug CSV and report.
    """
    for i in range(1, len(points) - 1):
        prev = points[i-1]
        cur = points[i]
        nxt = points[i+1]
        if prev.elevation is None or cur.elevation is None or nxt.elevation is None:
            continue
        d1 = dist((prev.easting, prev.northing), (cur.easting, cur.northing))
        d2 = dist((cur.easting, cur.northing), (nxt.easting, nxt.northing))
        if d1 <= 0.1 or d2 <= 0.1 or d1 > max_gap or d2 > max_gap:
            continue
        if cur.elevation >= prev.elevation + min_change and cur.elevation >= nxt.elevation + min_change:
            note = "Detected possible HIGH POINT from curb elevations. Verify against C-5.0 HP/T.C. callout."
            cur.source = (cur.source + " | " if cur.source else "") + "POSSIBLE HP"
            cur.desc = "BC HP"
            cur.issue = (cur.issue + " " if cur.issue else "") + note
            if not cur.confidence:
                cur.confidence = "HP_REVIEW"
        elif cur.elevation <= prev.elevation - min_change and cur.elevation <= nxt.elevation - min_change:
            note = "Detected possible LOW POINT from curb elevations. Verify against C-5.0 L.P./T.C. callout and drainage inlet direction."
            cur.source = (cur.source + " | " if cur.source else "") + "POSSIBLE LP"
            cur.desc = "BC LP"
            cur.issue = (cur.issue + " " if cur.issue else "") + note
            if not cur.confidence:
                cur.confidence = "LP_REVIEW"



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
    # Use all callouts as a best-fit transform. This is good enough to make PDF spots primary when close,
    # and the report still records the source and confidence for review.
    ce = np.linalg.lstsq(A, E, rcond=None)[0]
    cn = np.linalg.lstsq(A, N, rcond=None)[0]
    errs = []
    for a in anchors:
        v = np.array([a[0], a[1], 1.0])
        errs.append(math.hypot(float(ce @ v) - a[2], float(cn @ v) - a[3]))
    rmse = math.sqrt(sum(e*e for e in errs) / len(errs)) if errs else None
    info["rmse_ft"] = rmse
    if rmse and rmse > 20:
        info["warning"] = "PDF to CAD alignment is approximate. Spot elevations are primary, but review labels near dense curb areas."
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


def _pdf_leader_tip_for_text_block(page, bbox, max_leader_dist: float = 95.0) -> Optional[Point2]:
    """Find the probable arrow/tick endpoint for a PDF grade callout text block.

    Rule: the text box does not hold.  The arrow, leader, or tick endpoint holds.
    We search PDF vector segments near the text and choose the endpoint farthest
    away from the text block center, because CAD/PDF leaders generally run from
    the label toward the physical control point.
    """
    segs = _pdf_page_line_segments(page)
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
    parsed = parse_grade_text(raw)
    up = raw.replace("\\P", " ").replace("\n", " ").upper()
    # Add F.G. and standalone spot-grade values from C-5.0. Keep them as lower confidence than TC/FL/HP/LP.
    for m in re.finditer(r"\bF\.?\s*G\.?\s*=?\s*([0-9]{3}\.[0-9]{1,3})", up):
        try:
            parsed.append(("FG", float(m.group(1))))
        except Exception:
            pass
    if not parsed:
        nums = re.findall(r"\b(7[34][0-9]\.[0-9]{1,3})\b", up)
        if len(nums) == 1 and len(up.strip()) <= 24:
            try:
                parsed.append(("SPOT", float(nums[0])))
            except Exception:
                pass
    return parsed


def extract_pdf_spot_labels(pdf_path: str, default_curb_height: float, cad_reference_points: Optional[List[Point2]] = None) -> Tuple[List[GradeLabel], Dict[str, Any]]:
    """Extract C-5.0 grading spot labels and roughly locate them in CAD coordinates.
    These are used before contour interpolation.
    """
    labels: List[GradeLabel] = []
    info = {"grading_page": None, "pdf_spot_labels": 0, "alignment": {}}
    if fitz is None or not pdf_path or not os.path.exists(pdf_path):
        return labels, info
    doc = fitz.open(pdf_path)
    pi = find_plan_page(doc, "C-5.0", "GRADING", 5)
    info["grading_page"] = pi + 1
    model, ainfo = affine_from_layout_coordinates(pdf_path)
    if model is None:
        model, ainfo = fallback_pdf_to_cad_model_from_extents(pdf_path, pi, cad_reference_points)
    info["alignment"] = ainfo
    if model is None:
        return labels, info
    page = doc[pi]
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        raw = block_text(b)
        parsed = parse_pdf_grade_block(raw)
        if not parsed:
            continue
        # Skip the legend block. It contains terms without useful point locations.
        if "FINISHED GRADE" in raw.upper() and "TOP OF CURB" in raw.upper():
            continue
        x0, y0, x1, y1 = b["bbox"]
        tip = _pdf_leader_tip_for_text_block(page, b["bbox"])
        if tip is None:
            # Last resort only.  The review/source text will show that this came from the text box.
            px, py = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            tip_note = "text box center fallback"
        else:
            px, py = tip
            tip_note = "leader/arrow/tick endpoint"
        e, n = apply_affine(model, px, py)
        for kind, elev in parsed:
            # Do not hard-code one project elevation range.  This job is around 610-625,
            # while other Schoel jobs may be 700s or very different datum values.
            if -10000.0 <= elev <= 100000.0:
                labels.append(GradeLabel(e, n, kind, elev, raw.strip(), f"PDF grading spot page {pi+1} at {tip_note}"))
    info["pdf_spot_labels"] = len(labels)
    return labels, info


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
    """Pick the controlling spot elevation for a BC point.

    Important surveying rule:
    - TC / Top of Curb / HP-TC / LP-TC controls BC staking.
    - FL is flow line and only becomes BC by adding curb height when no TC/BC/HP/LP label controls.
    - FG and standalone contour/spot values are weaker and should not beat a nearby TC callout.
    """
    if not labels:
        return None, "", ""
    candidates = []
    for lab in labels:
        d = dist(p, (lab.x, lab.y))
        if d <= label_radius:
            pr = label_priority(lab.kind)
            # Prefer PDF C-5.0 callouts over CAD text when priority and distance are close.
            source_bonus = 0 if lab.source.upper().startswith("PDF") else 0.25
            candidates.append((pr, source_bonus, d, lab))
    if not candidates:
        return None, "", ""
    candidates.sort(key=lambda t: (t[0], t[1], t[2]))
    pr, _, d, best = candidates[0]
    prefix = "PDF C-5.0 spot" if best.source.upper().startswith("PDF") else "CAD spot"
    if best.kind == "FL":
        return best.elev + default_curb_height, f"{prefix} FL plus curb height, {d:.1f} ft away, from {best.raw}", "HIGH"
    if best.kind in ("TC", "BC", "HP", "LP", "HP_TC", "LP_TC"):
        return best.elev, f"{prefix} {best.kind} HELD AS TOP/BACK OF CURB, {d:.1f} ft away, from {best.raw}", "HIGH"
    if best.kind in ("FG", "SPOT"):
        return best.elev, f"{prefix} {best.kind}, {d:.1f} ft away, from {best.raw}", "MEDIUM"
    return best.elev, f"{prefix} grade label {best.kind}, {d:.1f} ft away, from {best.raw}", "MEDIUM"



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
        if auto_enforce:
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
        pts.append((x,y)); zs.append(sum(zlist)/len(zlist))
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
    """Survey-style local contour interpolation for 2D curb gaps.

    This is used only when the stronger selected-BC / selected-FL chain controls do
    not bracket the point.  It keeps the field output from staying at 0.000 when
    the point is clearly controlled by nearby 3D contour polylines.

    Rules:
    - Use only real contour layers with valid Z values.
    - If a valid contour is essentially on the point, hold that contour exactly.
    - Otherwise find the nearest lower and higher contour elevations and prorate
      by distance to those contour lines. This is interpolation, not blind averaging.
    - If the point cannot be bracketed by clean contours, return None so the normal
      safety rule can still export 0.000.
    """
    if not segments:
        return None, "no contour segments available for local prorating", "", 0.0
    by_z: Dict[float, Tuple[float, ElevSegment]] = {}
    for seg in segments:
        if not is_contour_layer_name(seg.layer):
            continue
        z = float(segment_z_at_point(p, seg))
        # Reject known placeholder and wild values.  Real project contours should
        # already be sanitized, but keep this guard for safety.
        if abs(z) < 1e-9 or z < 100.0 or z > 2000.0:
            continue
        d = point_segment_distance(p, seg.a, seg.b)
        if d > radius:
            continue
        rz = round(z, 3)
        if rz not in by_z or d < by_z[rz][0]:
            by_z[rz] = (d, seg)
    if not by_z:
        return None, f"no valid 3D contour within {radius:.1f} ft for local prorating", "", 0.0

    # Direct or near-direct contour hold. This catches cases where the contour is
    # within drafting tolerance of the picked BC line but the exact intersection failed.
    nearest_z, (nearest_d, nearest_seg) = min(by_z.items(), key=lambda kv: kv[1][0])
    if nearest_d <= near_hold_tol:
        return float(nearest_z), f"LOCAL CONTOUR HOLD: {nearest_z:.3f} contour within {nearest_d:.2f} ft of selected BC point on {nearest_seg.layer}", "LOCAL_CONTOUR_HOLD", nearest_d

    # Bracket the point between the nearest lower and upper contours.
    zs = sorted(by_z.keys())
    # The point does not have its own Z yet, so use distance-based bracketing:
    # choose the nearest pair with different elevations and reasonable spacing.
    best = None
    for i, zlo in enumerate(zs):
        dlo, seglo = by_z[zlo]
        for zhi in zs[i+1:]:
            dhi, seghi = by_z[zhi]
            dz = zhi - zlo
            if dz <= 0 or dz > 5.0:
                continue
            total = dlo + dhi
            if total <= 1e-9:
                continue
            # Prefer close, adjacent contour pairs.  Do not use two far-away contours.
            score = total + dz * 2.0
            if best is None or score < best[0]:
                best = (score, zlo, zhi, dlo, dhi, seglo, seghi)
    if best is None:
        return None, "valid contours found, but no clean lower/upper bracket for local prorating", "", nearest_d
    _score, zlo, zhi, dlo, dhi, seglo, seghi = best
    # Distance weighted linear interpolation between contour lines:
    # on low contour = zlo, on high contour = zhi.
    z = zlo + (dlo / max(dlo + dhi, 1e-9)) * (zhi - zlo)
    src = (f"LOCAL CONTOUR PRORATE: interpolated between {zlo:.3f} contour "
           f"({dlo:.2f} ft away on {seglo.layer}) and {zhi:.3f} contour "
           f"({dhi:.2f} ft away on {seghi.layer})")
    return float(z), src, "LOCAL_CONTOUR_PRORATE", min(dlo, dhi)

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


def contour_bc_rule(p: Point2, celev: Optional[float], csrc: str, cconf: str, nearest_d: float, default_curb_height: float, surface_mode: str = "bc") -> Tuple[Optional[float], str, str, str]:
    """Convert secondary contour/surface evidence into a BC elevation.

    IMPORTANT surveying rule:
    - If the source is TC/BC/top/back of curb, use it directly.
    - If the source is FL/gutter/pavement/typical Civil 3D surface, add curb height.
    - If set to Auto, only add when the layer/source text says FL/gutter/face of curb.
    """
    if celev is None:
        return None, csrc, cconf, ""

    mode = (surface_mode or "bc").strip().lower()

    if mode.startswith("bc") or mode.startswith("top") or mode.startswith("back"):
        return celev, csrc + " treated as top/back of curb, used directly", cconf if cconf else "CONTOUR_BC", ""

    # v8.4.6: selected BC direct-contour override.
    # If the contour evidence is physically on or very close to the selected BC point,
    # use the plotted contour elevation directly. Do NOT add curb height just because
    # the global surface mode says flowline/pavement. This fixes short contour/curb
    # gaps where the PDF/DXF clearly shows a 610 contour at the BC but the linework
    # stops just short of an actual mathematical intersection.
    near_bc_direct = (nearest_d is not None and nearest_d <= 2.50 and
                      not is_flowline_or_gutter_source(csrc) and
                      ("CONTOUR" in (csrc or "").upper() or "LOCAL_CONTOUR" in (cconf or "").upper() or "BC_STOP_SHORT" in (cconf or "").upper()))
    if near_bc_direct:
        warn = (f"v8.4.6 held nearby contour directly at selected BC because it is within {nearest_d:.2f} ft. "
                "No curb height was added. Review only if this contour is actually FL/gutter rather than BC.")
        return celev, csrc + " treated as nearby/short-gap selected BC contour, used directly", cconf if cconf else "CONTOUR_BC_DIRECT_NEAR", warn

    if mode.startswith("flow") or mode.startswith("gutter") or mode.startswith("pavement"):
        if is_back_curb_source(csrc):
            return celev, csrc + " explicitly looks like BC/top curb, used directly", cconf if cconf else "CONTOUR_BC", ""
        return celev + default_curb_height, csrc + f" treated as FL/gutter/pavement surface plus curb height {default_curb_height:.2f}", "SURFACE_PLUS_CURB", ""

    # Auto mode. Use layer/source wording first, then distance confidence.
    if is_flowline_or_gutter_source(csrc):
        if not CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF:
            return None, csrc + " interpreted as FL/gutter but no PDF verified curb height", "FL_HEIGHT_NOT_VERIFIED", "No PDF verified curb height. Elevation zeroed for review."
        return celev + default_curb_height, csrc + f" interpreted as FL/gutter plus PDF verified curb height {default_curb_height:.2f}", "FL_CONTOUR", ""
    if is_back_curb_source(csrc):
        return celev, csrc + " interpreted as BC/top curb, used directly", cconf if cconf else "CONTOUR_BC", ""
    if nearest_d <= 0.35:
        warn = ("Auto mode used this contour/surface directly because it touches the BC point. "
                "Review if the Civil surface is really pavement/flowline instead of top back of curb.")
        return celev, csrc + " interpreted as contour/surface at BC, used directly", cconf if cconf else "CONTOUR_BC", warn
    warn = (f"Secondary contour/surface is {nearest_d:.2f} ft from BC and is not labeled FL/gutter or BC. "
            f"GradeGuard used it directly in Auto mode, but review because a nearby contour could be on pavement, gutter, or grass.")
    return celev, csrc + " interpreted as nearby contour/surface, direct only", "LOW_CONTOUR", warn



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


def _order_connected_curb_run(run: List[CurbSegment], start_i: int = 0, tol: float = 1.50) -> List[CurbSegment]:
    """Order a connected set of curb segments into a station chain.

    This is intentionally simple and conservative.  It follows endpoint-to-endpoint
    connectivity so a curb run grades like the drawn curb, instead of making every
    point use only the single nearest short segment.
    """
    if len(run) <= 1:
        return run[:]

    remaining = run[:]
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
        for i, seg in enumerate(remaining):
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
        best = min(range(len(remaining)), key=lambda i: min(dist(tail.b, remaining[i].a), dist(tail.b, remaining[i].b)))
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

    visited = {start_i}
    stack = [start_i]
    while stack:
        i = stack.pop()
        for j, seg in enumerate(CURB_SEGMENTS):
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
    local_curb_segments = _local_curb_segments_for_point(p)
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

    # v8.4.6 short-gap BC contour anchors.
    # Real plan linework often stops just short of a true CAD intersection. If a contour
    # endpoint is within a small gap of the selected BC, or a selected BC endpoint is
    # within a small gap of a contour segment, treat that as a BC contour control.
    # This is direct BC elevation. No curb height is added.
    short_gap = min(6.0, max(2.5, search_radius * 0.06))
    cum = 0.0
    for i, cs in enumerate(local_curb_segments):
        L = max(dist(cs.a, cs.b), 1e-12)
        for es in contour_segments:
            # contour endpoint stops short of BC
            for ep in (es.a, es.b):
                t_bc, q_bc, d_bc = closest_point_t_on_segment(ep, cs.a, cs.b)
                if d_bc <= short_gap:
                    z = segment_z_at_point(ep, es)
                    st = cum + max(0.0, min(1.0, t_bc)) * L
                    anchors.append((st, float(z), q_bc, f"{z:.3f} short-gap contour endpoint projected to selected BACK OF CURB on {es.layer}; gap {d_bc:.2f} ft", "BC_STOP_SHORT_CONTOUR_DIRECT"))
            # selected BC endpoint stops short of contour
            for t_bc_end, bc_ep in ((0.0, cs.a), (1.0, cs.b)):
                t_con, q_con, d_con = closest_point_t_on_segment(bc_ep, es.a, es.b)
                if d_con <= short_gap:
                    z = segment_z_at_point(q_con, es)
                    st = cum + t_bc_end * L
                    anchors.append((st, float(z), bc_ep, f"{z:.3f} selected BACK OF CURB endpoint projected to nearby contour on {es.layer}; gap {d_con:.2f} ft", "BC_STOP_SHORT_CONTOUR_DIRECT"))
        cum += L

    # v8.4.9 near-point contour anchor:
    # If the BC point itself is within a tight tolerance of a contour segment, hold
    # that contour directly at this station.  This catches cases where CAD linework
    # stops a hair short or contours are slightly offset from the picked BC.
    near_point_tol = max(0.20, min(0.35, search_radius * 0.01))
    near_point_candidates = []
    for es in contour_segments:
        d_np = point_segment_distance(p, es.a, es.b)
        if d_np <= near_point_tol:
            _t_np, q_np, _d_np2 = closest_point_t_on_segment(p, es.a, es.b)
            z_np = float(segment_z_at_point(q_np, es))
            near_point_candidates.append((d_np, p_station, z_np, p_q, es.layer))
    if near_point_candidates:
        near_point_candidates.sort(key=lambda c: c[0])
        d_np, st_np, z_np, q_np, layer_np = near_point_candidates[0]
        anchors.append((
            st_np,
            z_np,
            q_np if q_np is not None else p,
            f"{z_np:.3f} near-point contour projected to selected BACK OF CURB on {layer_np}; gap {d_np:.2f} ft",
            "BC_NEAR_POINT_CONTOUR_DIRECT",
        ))

    # v8.4.9 endpoint extension:
    # If the curb stops short but a contour is clearly just beyond the local curb end,
    # hold that contour at the curb endpoint.  This is a direct BC contour control.
    endpoint_anchor = _chain_endpoint_extension_anchor(p, local_curb_segments, contour_segments, search_radius)
    if endpoint_anchor is not None:
        anchors.append(endpoint_anchor)

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
                if st is None:
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
            # Prefer direct BC/short-gap BC contour over FL projected at the same station.
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

def assign_elevations(points: List[Point2], labels: List[GradeLabel], segments: List[ElevSegment], default_curb_height: float, label_radius: float, contour_radius: float, add_curb_height_to_contours: bool, contour_check_tolerance: float = 0.15, landxml_surface: Optional[LandXMLSurface] = None, surface_mode: str = "bc", contour_tin: Optional[ContourTinSurface] = None) -> List[Tuple[Optional[float], str, str, str]]:
    assigned = []
    for p in points:
        # Primary: C-5.0/PDF or CAD spot callouts. TC/BC/HP/LP hold the BC elevation.
        elev, src, conf = nearest_label_elevation(p, labels, default_curb_height, label_radius)
        exact_override = get_contour_point_override(p)
        # Secondary 1: true Civil 3D/LandXML TIN surface if supplied.
        # This is the preferred non-PDF source because it is the actual design model, not plotted text.
        selev, ssrc, sconf = landxml_elevation_at(p, landxml_surface, search_pad=max(5.0, min(contour_radius, 25.0)))

        # Secondary 1B: if no XML surface exists, build/use a temporary TIN from 3D DXF contours.
        # This lets 2D curb workflows prorate between contour elevations instead of snapping to nearest contour.
        tin_elev = tin_src = tin_conf = None
        if selev is None and contour_tin is not None:
            tin_elev, tin_src, tin_conf = contour_tin_elevation(p, contour_tin)

        # Secondary 1C: strongest 2D contour method for curbs.  It builds controls from
        # actual contour crossings on the picked BC and picked FL/gutter lines, then
        # prorates along the selected BC chain.  This must beat the temporary TIN.
        chain_elev, chain_src, chain_conf, chain_d = curb_chain_contour_elevation(p, segments, contour_radius, default_curb_height)

        # Secondary 1D: local survey-style contour interpolation.  This fills small
        # gaps where exact BC/FL intersections missed due to exploded CAD geometry,
        # while still returning None when the point cannot be bracketed safely.
        local_elev, local_src, local_conf, local_d = local_contour_prorated_elevation(p, segments, max(contour_radius, 35.0))

        # Secondary 2: contours/surface that actually touch the selected BC layer.
        # This prevents a nearby contour from being used when it does not cross the BC line.
        belev, bsrc, bconf, bd = bc_touching_elevation(p, segments, contour_radius, default_curb_height)
        if belev is not None:
            celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = contour_bc_rule(p, belev, bsrc, bconf, bd, default_curb_height, "bc")
            nearest_d = bd
            felev = fsrc = fconf = None; fd = 0.0
        else:
            felev, fsrc, fconf, fd = flowline_touching_elevation(p, segments, contour_radius, default_curb_height)
            if felev is not None:
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = felev, fsrc, fconf, ""
                nearest_d = fd
            else:
                celev, csrc, cconf, nearest_d = contour_elevation(p, segments, contour_radius)
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = contour_bc_rule(p, celev, csrc, cconf, nearest_d, default_curb_height, surface_mode)

        if elev is not None:
            # Spot/callout anchors hold first, but surface and contour are QA checks.
            if chain_elev is not None:
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = chain_elev, chain_src, chain_conf, ""
            elif local_elev is not None:
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = local_elev, local_src, local_conf, ""
            elif selev is not None:
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = contour_bc_rule(p, selev, ssrc, sconf, 0.0, default_curb_height, surface_mode)
            elif tin_elev is not None:
                celev_for_check, csrc_for_check, cconf_for_check, contour_rule_warn = contour_bc_rule(p, tin_elev, tin_src, tin_conf, 0.0, default_curb_height, surface_mode)
            issue = ""
            if celev_for_check is not None:
                diff = abs(float(elev) - float(celev_for_check))
                # The spot still holds. The contour is only a check.
                if diff > contour_check_tolerance:
                    issue = (
                        f"Secondary contour check differs from held spot by {diff:.2f} ft. "
                        f"Spot elevation is primary and was held. Review contour/surface near curb. "
                        f"Contour source: {csrc_for_check}"
                    )
                elif contour_rule_warn:
                    issue = contour_rule_warn + " Spot elevation still held."
            assigned.append((elev, src, issue, conf))
            continue

        if exact_override is not None:
            oelev, osrc, oconf, oissue = exact_override
            assigned.append((oelev, osrc, oissue, oconf))
            continue

        if chain_elev is not None and (chain_conf or "").upper() in ("BC_CONTOUR_EXACT", "FL_CONTOUR_PLUS_CURB_EXACT", "BC_CHAIN_CONTOUR_INTERP", "BC_CHAIN_CONTOUR_REVIEW"):
            issue = "Elevation calculated from selected curb-component contour controls. "
            src_upper = (chain_src or "").upper()
            if (chain_conf or "").upper() == "FL_CONTOUR_PLUS_CURB_EXACT":
                issue += "Exact contour hit selected FLOWLINE/GUTTER/EP, so GradeGuard added verified curb height to compute BC. "
            elif (chain_conf or "").upper() == "BC_CONTOUR_EXACT":
                issue += "Exact contour hit selected BACK OF CURB, so GradeGuard used contour elevation directly. "
            elif "FLOWLINE/GUTTER" in src_upper and "BACK OF CURB" in src_upper:
                issue += "Interpolated/prorated along BC between BC-equivalent contour controls. FL/gutter controls were first converted to BC using the verified curb height, then the point was prorated between controls. "
            elif "FLOWLINE/GUTTER" in src_upper:
                issue += "Interpolated/prorated along BC from FL/gutter contour controls after adding the verified curb height. Review if catch/spill/header curb height changes in this area. "
            elif "BACK OF CURB" in src_upper:
                issue += "Interpolated/prorated along BC between exact BC contour crossings. "
            if "REVIEW" in (chain_conf or ""):
                issue += "Review this point because the adjacent contour controls create a steep curb profile. "
            assigned.append((chain_elev, chain_src, issue, chain_conf))
            continue

        # No usable spot callout nearby. For 2D curb workflows, actual contour touches
        # on the selected BC or selected FL/gutter must beat the temporary TIN.
        # This preserves the survey rule: contour on BC = exact elevation; contour on
        # FL/gutter/EP = contour elevation plus verified curb height.
        if celev_for_check is not None and (cconf_for_check or "").upper() in ("BC_TOUCH", "BC_TOUCH_INTERP", "FL_TOUCH_PLUS_CURB", "FL_TOUCH_INTERP_PLUS_CURB"):
            issue = "Elevation calculated from contour crossing selected curb component because no held TC/BC/HP/LP/FL/FG spot callout was found nearby. "
            if cconf_for_check and "FL_TOUCH" in cconf_for_check:
                issue += "Contour touched selected FLOWLINE/GUTTER/EP, so GradeGuard added curb height to compute BC. "
            elif cconf_for_check and "BC_TOUCH" in cconf_for_check:
                issue += "Contour touched selected BACK OF CURB, so GradeGuard used the contour elevation directly. "
            if contour_rule_warn:
                issue += contour_rule_warn
            assigned.append((celev_for_check, csrc_for_check, issue, cconf_for_check or "CONTOUR_TOUCH"))
        elif local_elev is not None and (local_conf or "").upper() in ("LOCAL_CONTOUR_HOLD", "LOCAL_CONTOUR_PRORATE"):
            issue = "Elevation calculated by local survey-style contour interpolation because exact selected BC/FL contour chain controls did not bracket this point. "
            issue += "This uses nearby 3D contour polylines and prorates by distance between contour elevations; review against the certified PDF if callouts exist. "
            assigned.append((local_elev, local_src, issue, local_conf))
        elif selev is not None:
            selev_bc, ssrc_bc, sconf_bc, swarn = contour_bc_rule(p, selev, ssrc, sconf, 0.0, default_curb_height, surface_mode)
            issue = "Elevation calculated from LandXML/TIN design surface because no held TC/BC/HP/LP/FL/FG spot callout or picked curb-component contour touch was found nearby. Use PDF C-5.0 as QA check. "
            if swarn:
                issue += swarn
            assigned.append((selev_bc, ssrc_bc, issue, sconf_bc or "SURFACE"))
        elif tin_elev is not None:
            tin_bc, tin_src_bc, tin_conf_bc, tin_warn = contour_bc_rule(p, tin_elev, tin_src, tin_conf, 0.0, default_curb_height, surface_mode)
            issue = "Elevation calculated from temporary TIN built from 3D DXF contours because no held TC/BC/HP/LP/FL/FG spot callout, LandXML surface, or picked BC/FL contour touch was found. "
            issue += "This prorates between contour elevations and is fallback behind actual BC/FL contour intersections. "
            if tin_warn:
                issue += tin_warn
            assigned.append((tin_bc, tin_src_bc, issue, tin_conf_bc or "TIN_CONTOUR"))
        elif celev_for_check is not None:
            issue = "Elevation calculated from secondary contour/surface source because no stronger source was found nearby. "
            if cconf_for_check and "FL_TOUCH" in cconf_for_check:
                issue += "Contour touched selected FLOWLINE/GUTTER, so GradeGuard added curb height to compute BC. "
            elif cconf_for_check and "BC_TOUCH" in cconf_for_check:
                issue += "Contour touched selected BACK OF CURB, so GradeGuard used the contour elevation directly. "
            if contour_rule_warn:
                issue += contour_rule_warn
            else:
                issue += "Contour/surface rule applied. Review before final staking."
            assigned.append((celev_for_check, csrc_for_check, issue, cconf_for_check or "CONTOUR"))
        else:
            assigned.append((None, csrc or "no grade label or contour source found", "Missing calculated elevation. Review CAD/PDF spot grades or surface.", ""))
    return assigned

def add_flow_warnings(points: List[GenPoint], min_slope: float = 0.003, max_gap: float = 55.0):
    # This is a first pass. It flags flat pairs only when consecutive generated points are close enough to be likely on the same run.
    for a, b in zip(points[:-1], points[1:]):
        if a.elevation is None or b.elevation is None:
            continue
        d = dist((a.easting, a.northing), (b.easting, b.northing))
        if d <= 0.1 or d > max_gap:
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
        op = GenPoint(point=next_pt, northing=oy, easting=ox, elevation=bc.elevation, desc=bc.desc, source="Offset from BC", issue=issue, confidence="OFFSET")
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
        if abs(z) < 1e-9 or z < 100.0 or z > 2000.0:
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
        if abs(z) < 1e-9 or z < 100.0 or z > 2000.0:
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
        nearest = min(anchors, key=lambda a: abs(a[0] - st))
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

        before = [a for a in anchors if a[0] < st]
        after = [a for a in anchors if a[0] > st]
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
        "MANUAL", "PDF_TICK", "PDF LEADER", "PDF_SPOT"
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
            if abs(z) > 1e-9 and 100.0 <= z <= 2000.0:
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
            if abs(z) < 1e-9 or z < 100.0 or z > 2000.0:
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

        before = [a for a in anchors if a[0] < st]
        after = [a for a in anchors if a[0] > st]
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
        "HELD EXACT", "CALLOUT HELD", "TICK", "ARROW ENDPOINT", "LEADER ENDPOINT",
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


def sanitize_field_elevations(points: List[GenPoint], unknown_to_zero: bool = True, max_local_jump: float = 8.0) -> int:
    """Zero out unsafe or unknown staking elevations before field CSV export.

    v8.3 field-safety rule:
    GradeGuard must not export a believable elevation unless the source is strong enough
    for field staking. Unknown or less-than-high-confidence points are exported as
    0.000 so the crew does not accidentally build from a wrong elevation.

    Accepted as high-confidence:
    - held/manual CAD/PDF callouts at the leader/arrow point,
    - exact selected BC contour crossing,
    - exact selected FL/gutter contour crossing plus verified curb height,
    - LandXML/design surface,
    - clean interpolation/proration between contour controls after converting FL/gutter controls to BC-equivalent elevations.

    Zeroed for review:
    - no source,
    - obvious project outlier,
    - nearby contour fallback,
    - one-sided control,
    - mixed BC and FL/gutter interpolation,
    - any source text containing REVIEW/LOW/UNKNOWN/UNSAFE.
    """
    if not points:
        return 0
    vals = [float(p.elevation) for p in points if p.elevation is not None and abs(float(p.elevation)) > 1e-9]
    if not vals:
        for p in points:
            p.elevation = 0.0 if unknown_to_zero else None
            p.confidence = (p.confidence + "; ZEROED_UNKNOWN").strip("; ") if p.confidence else "ZEROED_UNKNOWN"
            p.issue = (p.issue + " " if p.issue else "") + "No reliable elevation source found. Field elevation set to 0.000 for safety."
        return len(points)

    vals_sorted = sorted(vals)
    med = vals_sorted[len(vals_sorted)//2]
    hard_outlier = max(50.0, max_local_jump * 5.0)
    zeroed = 0

    def strong_enough(gp: GenPoint) -> Tuple[bool, str]:
        conf = (gp.confidence or "").upper()
        src = (gp.source or "").upper()
        issue = (gp.issue or "").upper()
        text = " ".join([conf, src, issue])

        # v8.4.1: accept real survey controls/interpolations BEFORE looking for
        # generic REVIEW text.  Previous builds zeroed good interpolated values only
        # because the explanatory issue said "review against the certified PDF."
        high_confidence_tokens = [
            "BC_CONTOUR_EXACT",
            "BC_DIRECT_CONTOUR_HOLD_V850",
            "BC_CONTOUR_PROFILE_PRORATE_V850",
            "BC_NEAR_POINT_CONTOUR_DIRECT",
            "BC_ENDPOINT_EXTENSION_CONTOUR_DIRECT",
            "FL_CONTOUR_PLUS_CURB_EXACT",
            "BC_CHAIN_CONTOUR_INTERP",
            "SURVEY INTERPOLATION",
            "LOCAL_CONTOUR_HOLD",
            "LOCAL_CONTOUR_PRORATE",
            "TIN_CONTOUR",
            "CONTOUR_TIN",
            "SURFACE_MODE_TIN_PLUS_CURB_V853",
            "SURFACE_MODE_FL_EP_PLUS_CURB_V853",
            "SURFACE_MODE_DIRECT_BC_CONTOUR_V853",
            "SURFACE_MODE_TIN_DIRECT_V853",
            "BC_POST_CHAIN_INTERP",
            "BC_POST_NEAR_HOLD",
            "HP_TC",
            "LP_TC",
            "HIGH POINT",
            "LOW POINT",
            "HP/LP CALLOUT",
            "PROJECTED TO SELECTED BC AS HP/LP CALLOUT HOLD",
            "HP_HARD_HOLD",
            "LP_HARD_HOLD",
            "HP/LP HARD HOLD",
            "HP_LP_CLUSTER_HOLD_V857",
        ]
        if any(tok in text for tok in high_confidence_tokens) and "ZEROED" not in text and "UNSAFE" not in text:
            # v8.4.2: HP/LP are true design break points. Do not reject an LP just
            # because the words LOW POINT appear in the issue/source text.
            return True, ""

        # Explicit unsafe wording still loses.  Do not use plain "LOW " here,
        # because that incorrectly catches LOW POINT / LP callouts.
        if any(tok in text for tok in ["ZEROED", "UNKNOWN", "UNSAFE", "LOW_CONF", "LOW-CONFIDENCE", "LOW_BC_TOUCH", "ONE-SIDED", "NEARBY CONTOUR FALLBACK", "SURFACE_PLUS_CURB"]):
            return False, "Source is fallback/low-confidence/nearby contour, not a 90% field stake source."
        if "REVIEW" in text and not any(tok in conf for tok in ["BC_CONTOUR_EXACT", "FL_CONTOUR_PLUS_CURB_EXACT", "SURFACE_MODE_TIN_PLUS_CURB_V853", "SURFACE_MODE_FL_EP_PLUS_CURB_V853", "SURFACE_MODE_DIRECT_BC_CONTOUR_V853", "SURFACE_MODE_TIN_DIRECT_V853"]):
            return False, "Source is marked REVIEW, so it was not exported as a field elevation."

        exact_ok = [
            "MANUAL", "HELD", "CAD SPOT", "PDF", "TC", "HP", "LP",
            "BC_CONTOUR_EXACT", "FL_CONTOUR_PLUS_CURB_EXACT", "LANDXML", "SURFACE"
        ]
        if any(tok in text for tok in exact_ok):
            return True, ""

        if "BC_CHAIN_CONTOUR_INTERP" in conf or "SURVEY INTERPOLATION" in src:
            return True, ""

        if "LOCAL_CONTOUR_HOLD" in conf or "LOCAL_CONTOUR_PRORATE" in conf:
            return True, ""

        if "TIN_CONTOUR" in conf or "CONTOUR_TIN" in conf:
            # v8.3: Allow prorated temporary TIN values from 3D contours when no stronger
            # source exists, as long as no review/unsafe wording is present and the value
            # passes the project outlier check below. This is equivalent to interpolating
            # between contours instead of snapping to the nearest contour.
            return True, ""

        if "BC_TOUCH_INTERP" in conf and "FLOWLINE" not in src and "GUTTER" not in src:
            return True, ""

        return False, "Source was not strong enough for the 90% field-staking rule."

    for gp in points:
        reason = ""
        if gp.elevation is None:
            reason = "No reliable elevation source found."
        else:
            z = float(gp.elevation)
            if abs(z) < 1e-9:
                reason = "Elevation was already zero or missing."
            elif abs(z - med) > hard_outlier:
                reason = f"Elevation {z:.3f} is an obvious outlier from the project median {med:.3f}."
            else:
                ok, why = strong_enough(gp)
                if not ok:
                    reason = why
        if reason:
            gp.elevation = 0.0 if unknown_to_zero else None
            gp.confidence = (gp.confidence + "; ZEROED_REVIEW").strip("; ") if gp.confidence else "ZEROED_REVIEW"
            gp.issue = (gp.issue + " " if gp.issue else "") + reason + " Field elevation set to 0.000 for safety; review before staking."
            zeroed += 1
    return zeroed

def write_csv(points: List[GenPoint], out_path: str):
    """Write field import CSV with NO header row.

    CAD/data collector import expects raw P,N,E,Z,D rows.  The detailed debug
    CSV keeps headers and sources for review.
    """
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for p in points:
            elev = "0.000" if p.elevation is None else f"{p.elevation:.3f}"
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, p.desc or "BC"])


def write_debug_csv(points: List[GenPoint], out_path: str):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Point", "Northing", "Easting", "Elevation", "Description", "Source", "Confidence", "Issue"])
        for p in points:
            elev = "0.000" if p.elevation is None else f"{p.elevation:.3f}"
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, p.desc or "BC", p.source, p.confidence, p.issue])



# ----------------------------
# GradeGuard v9.0 Survey Package QA
# ----------------------------

def gg_v9_status_for_point(p: GenPoint) -> Tuple[str, str]:
    """Return GREEN/YELLOW/RED and a short reason.

    This is office QA, not a separate crew report.
    GREEN  = ready to stake based on strong source.
    YELLOW = calculated but review recommended.
    RED    = do not stake until reviewed/fixed.
    """
    txt = " ".join([(p.source or ""), (p.confidence or ""), (p.issue or ""), (p.desc or "")]).upper()

    if p.elevation is None:
        return "RED", "Missing elevation."
    try:
        z = float(p.elevation)
    except Exception:
        return "RED", "Elevation is not numeric."
    if abs(z) < 1e-9:
        return "RED", "0.000 elevation requires review."

    red_terms = [
        "ZEROED_REVIEW", "UNSUPPORTED", "MISSING", "NO RELIABLE", "DO NOT STAKE",
        "FIELD ELEVATION SET TO 0.000", "NOT BRACKETED", "COULD NOT"
    ]
    if any(t in txt for t in red_terms):
        return "RED", "Point has a blocking review/source warning."

    strong_terms = [
        "PDF_TICK", "PDF LEADER", "PDF_SPOT", "MANUAL", "TC/BC", "BC_CONTOUR",
        "DIRECT BC", "CONTOUR ON SELECTED BC", "FL/GUTTER", "FLOWLINE/GUTTER/EP",
        "PLUS CURB", "CURB HEIGHT", "HP_LP", "HP/LP", "SURFACE_PLUS_CURB",
        "BC_TOUCH_INTERP", "STATION", "TIN", "LANDXML", "SURFACE"
    ]

    yellow_terms = [
        "REVIEW", "LOW_CONTOUR", "SURFACE_REVIEW", "FALLBACK", "NEARBY",
        "ENDPOINT", "CLUSTER HOLD", "FLAT", "NO-FLOW", "CHECK"
    ]

    if any(t in txt for t in yellow_terms):
        # Strong source with review wording is still not red, but should be checked.
        return "YELLOW", "Calculated from usable evidence, but flagged for office review."

    if any(t in txt for t in strong_terms):
        return "GREEN", "Trusted GradeGuard source."

    return "YELLOW", "Calculated value, but source confidence is not explicit."


def write_confidence_summary_csv(points: List[GenPoint], out_path: str) -> Dict[str, int]:
    counts = {"GREEN": 0, "YELLOW": 0, "RED": 0}
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Point", "Northing", "Easting", "Elevation", "Description", "QA_Status", "QA_Reason", "Confidence", "Source", "Issue"])
        for p in points:
            status, reason = gg_v9_status_for_point(p)
            counts[status] = counts.get(status, 0) + 1
            elev = "0.000" if p.elevation is None else f"{p.elevation:.3f}"
            w.writerow([p.point, f"{p.northing:.3f}", f"{p.easting:.3f}", elev, p.desc or "BC", status, reason, p.confidence, p.source, p.issue])
    return counts


def write_surface_conflict_report_csv(points: List[GenPoint], out_path: str, max_normal_jump: float = 0.35, min_distance: float = 0.25, max_distance: float = 75.0) -> Dict[str, int]:
    """Office QA report for suspicious profile jumps.

    This is intentionally separate from the staking CSV. It does not change
    elevations. It flags profile problems like the 6006/6019 type jumps.
    """
    rows = []
    good = [p for p in points if p.elevation is not None]
    for a, b in zip(good[:-1], good[1:]):
        pa = (float(a.easting), float(a.northing))
        pb = (float(b.easting), float(b.northing))
        d = dist(pa, pb)
        if d < min_distance or d > max_distance:
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


def write_v9_package_manifest(out_dir: str, csv_path: str, debug_csv_path: str, report_path: str, confidence_path: str, conflict_path: str, flow_pdf_path: str, dxf_stats: Dict[str, Any]) -> str:
    manifest_path = os.path.join(out_dir, "GradeGuard_v9_Survey_Package_Manifest.txt")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("Schoel GradeGuard v9.0 Survey Package Mode\n")
        f.write("=========================================\n\n")
        f.write("Core staking rules locked in this build:\n")
        f.write("1. Final signed PDF controls.\n")
        f.write("2. CAD/DXF gives geometry.\n")
        f.write("3. Proposed contours build the grading surface.\n")
        f.write("4. Actual contour on selected BC holds exact elevation.\n")
        f.write("5. Actual contour on selected EP/FL/gutter adds standard/PDF curb height.\n")
        f.write("6. Normal BC points between controls prorate by station along the connected curb run.\n")
        f.write("7. HP and LP always hold and are never averaged through.\n")
        f.write("8. Bad jumps, surface conflicts, drainage flat spots, and unsupported elevations are flagged for office QA.\n")
        f.write("9. No separate crew-view report is created.\n\n")
        f.write("Outputs:\n")
        for label, path in [
            ("Clean staking CSV, no header", csv_path),
            ("Source/debug CSV", debug_csv_path),
            ("HTML office QA report", report_path),
            ("Confidence summary CSV", confidence_path),
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
    for a, b in zip(good[:-1], good[1:]):
        pa = (a.easting, a.northing); pb = (b.easting, b.northing)
        d = dist(pa, pb)
        if d < 0.25 or d > max_gap:
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
            elev = "0.000" if p.elevation is None else f"{p.elevation:.3f}"
            f.write(f"<tr><td>{p.point}</td><td>{p.northing:.3f}</td><td>{p.easting:.3f}</td><td>{elev}</td><td>{p.confidence}</td><td>{p.issue}</td><td>{p.source}</td></tr>")
        f.write("</table>")
        f.write("<h2>First 100 Generated Points</h2>")
        f.write("<table><tr><th>Point</th><th>N</th><th>E</th><th>Elev</th><th>Source</th></tr>")
        for p in points[:100]:
            elev = "0.000" if p.elevation is None else f"{p.elevation:.3f}"
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
    """Build a PDF-to-CAD transform without ever scaling the DXF.

    v1.7 rule:
    - CAD/DXF coordinates stay fixed in real State Plane feet.
    - The PDF is what gets scaled, rotated, and shifted to CAD.
    - Use a best-fit SIMILARITY transform, not a free affine/shear transform.
      That keeps the PDF like a real sheet of paper and prevents it from being
      warped just to fit imperfect text boxes.
    - The routine tests both normal and reflected PDF-Y direction because PDF Y
      increases downward while CAD Northing increases upward.
    """
    pts = alignment_points or []
    clean = []
    for p in pts:
        try:
            x, y = float(p["pdf"][0]), float(p["pdf"][1])
            e, n = float(p["cad"][0]), float(p["cad"][1])
            clean.append((x, y, e, n))
        except Exception:
            pass
    if len(clean) < 2:
        return None, {"points": len(clean), "method": "none", "warning": "Need at least two alignment points or Auto Overlay."}
    try:
        import numpy as np
        X = np.array([[x, y] for x, y, e, n in clean], dtype=float)
        Y = np.array([[e, n] for x, y, e, n in clean], dtype=float)

        def solve_model(reflect=False):
            # Model with no shear.
            # Non-reflect: E = a*x - b*y + tx ; N = b*x + a*y + ty
            # Reflect:     E = a*x + b*y + tx ; N = b*x - a*y + ty
            rows = []
            vals = []
            for (x, y), (e, n) in zip(X, Y):
                if reflect:
                    rows.append([x, y, 1, 0]); vals.append(e)
                    rows.append([y, -x, 0, 1]); vals.append(n)  # n = b*x - a*y + ty -> [y,-x] on [b,a]? handled below by coeff map
                else:
                    rows.append([x, -y, 1, 0]); vals.append(e)
                    rows.append([y, x, 0, 1]); vals.append(n)
            A = np.array(rows, dtype=float)
            B = np.array(vals, dtype=float)
            sol = np.linalg.lstsq(A, B, rcond=None)[0]
            if reflect:
                # rows used unknowns [a,b,tx,ty] for E; N row [b,-a,0,1]
                a, b, tx, ty = sol
                ce = [float(a), float(b), float(tx)]
                cn = [float(b), float(-a), float(ty)]
            else:
                a, b, tx, ty = sol
                ce = [float(a), float(-b), float(tx)]
                cn = [float(b), float(a), float(ty)]
            errs = []
            for x, y, e, n in clean:
                pe = ce[0]*x + ce[1]*y + ce[2]
                pn = cn[0]*x + cn[1]*y + cn[2]
                errs.append(((pe-e)**2 + (pn-n)**2)**0.5)
            rmse = (sum(v*v for v in errs)/len(errs))**0.5 if errs else 0.0
            scale = (ce[0]**2 + ce[1]**2)**0.5
            return (ce, cn, rmse, scale, reflect)

        cand = [solve_model(False), solve_model(True)]
        ce, cn, rmse, scale, reflect = min(cand, key=lambda z: z[2])
        warn = ""
        if rmse > 2.0:
            warn = "Auto overlay RMSE is over 2 ft. The PDF is scaled/rotated, but click-held TC tick anchors should still be reviewed."
        return (ce, cn), {"points": len(clean), "method": "PDF similarity transform, scale/rotate/shift only, DXF fixed", "rmse_ft": rmse, "pdf_scale_ft_per_point": scale, "reflected_pdf_y": reflect, "warning": warn}
    except Exception:
        # Fallback: exact two-point scale/rotate/shift.
        p1, p2 = clean[0], clean[1]
        x1,y1,e1,n1 = p1; x2,y2,e2,n2 = p2
        dxp, dyp = x2-x1, y2-y1
        dxc, dyc = e2-e1, n2-n1
        lp = math.hypot(dxp, dyp)
        lc = math.hypot(dxc, dyc)
        if lp == 0:
            return None, {"points": len(clean), "method": "fallback failed", "warning": "Alignment points are coincident."}
        sc = lc / lp
        ap = math.atan2(dyp, dxp)
        ac = math.atan2(dyc, dxc)
        th = ac - ap
        a = sc * math.cos(th); b = sc * math.sin(th)
        ce = [a, -b, e1 - (a*x1 - b*y1)]
        cn = [b,  a, n1 - (b*x1 + a*y1)]
        return (ce, cn), {"points": len(clean), "method": "two point PDF scale/rotate/shift, DXF fixed", "warning": "Numpy unavailable, using first two alignment points."}


def transform_pdf_to_cad(model, pdf_xy):
    if model is None:
        return None
    ce, cn = model
    x, y = pdf_xy
    return (float(ce[0]*x + ce[1]*y + ce[2]), float(cn[0]*x + cn[1]*y + cn[2]))


def load_manual_anchor_file(path: str) -> Tuple[List[GradeLabel], Dict[str, Any]]:
    info = {"manual_anchor_file": path or "", "alignment_points": 0, "manual_spot_anchors": 0, "alignment": {}}
    labels: List[GradeLabel] = []
    if not path or not os.path.exists(path):
        return labels, info
    try:
        data = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception as e:
        info["warning"] = f"Could not read manual anchor file: {e}"
        return labels, info
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
            if not (-10000.0 <= elev <= 100000.0):
                continue
            if "cad" in s:
                e, n = float(s["cad"][0]), float(s["cad"][1])
            elif "pdf" in s and model is not None:
                e, n = transform_pdf_to_cad(model, tuple(s["pdf"]))
            else:
                continue
            raw = s.get("raw") or f"Manual {kind} {elev:.3f}"
            page = s.get("page", "")
            labels.append(GradeLabel(e, n, kind, elev, raw, f"MANUAL PDF C-5.0 tick anchor page {page}"))
        except Exception:
            continue
    return labels, info


def save_manual_anchor_file(path: str, data: Dict[str, Any]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def run_generation(cad_path: str, pdf_path: str, out_dir: str, start_pt: int, spacing: float, default_curb_height: float, curb_layer_keywords: str, elev_layer_keywords: str, label_radius: float, contour_radius: float, min_slope: float, add_curb_height_to_contours: bool=False, contour_check_tolerance: float=0.15, manual_anchor_path: str="", require_manual_pdf_spots: bool=True, surface_path: str="", surface_mode: str="bc", selected_curb_path: str="", selected_flowline_path: str="", create_offset: bool=False, offset_distance: float=3.0, offset_mode: str="away_from_flowline", offset_reverse: bool=False):
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(cad_path)[1].lower()
    if ext == ".dwg":
        raise RuntimeError("DWG files need to be converted to DXF first. Export from Civil 3D or use ODA File Converter, then load the DXF.")
    if ext != ".dxf":
        raise RuntimeError("Generate Mode currently reads DXF. Save the civil CAD as DXF first.")

    curb_keywords = [x.strip() for x in re.split(r"[,;]", curb_layer_keywords) if x.strip()]
    elev_keywords = [x.strip() for x in re.split(r"[,;]", elev_layer_keywords) if x.strip()]
    if not pdf_path or not os.path.exists(pdf_path) or not str(pdf_path).lower().endswith(".pdf"):
        raise RuntimeError("Civil PDF plan set is required. GradeGuard will not calculate BC elevations without the certified PDF plans because PDF callouts/details hold over CAD/DXF.")
    selected_handles = load_selected_curb_handles(selected_curb_path)
    selected_fl_handles = load_selected_curb_handles(selected_flowline_path)
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
    dxf_stats["v8_4_4_auto_qa_rule"] = "Automatic QA/QC active: final signed civil PDF is required; PDF controls over CAD/DXF; PDF callouts use leader/arrow/tick endpoint when readable, not the text box; standard curb +0.50 unless the certified PDF detail specifically supports catch +0.42, spill +0.58, or custom height; HP/LP points are protected; unsupported 0.000 elevations are flagged for review."
    raw_points, pcpt_added = add_curb_geometry_control_points(raw_points, tolerance=0.05)
    dxf_stats["pc_pt_geometry_points_added"] = pcpt_added
    raw_points, contour_crossings_added, fl_crossings_added = add_contour_crossing_bc_points(raw_points, segments, tolerance=0.35)
    dxf_stats["contour_crossing_bc_points_added"] = contour_crossings_added
    dxf_stats["flowline_contour_crossing_bc_points_added"] = fl_crossings_added
    landxml_surface = safe_load_landxml_surface(surface_path)
    dxf_stats["landxml_surface"] = {"path": surface_path if landxml_surface else "", "triangles": len(landxml_surface.triangles) if landxml_surface else 0, "feature_segments": len(getattr(landxml_surface, "segments", []) or []) if landxml_surface else 0}
    # For 2D curb projects with no LandXML, build a temporary TIN from 3D contours so
    # curb elevations can be prorated between contours instead of reading the nearest contour.
    contour_tin_surface = build_contour_tin_surface(segments)
    dxf_stats["temporary_contour_tin"] = {"created": bool(contour_tin_surface), "points": int(len(contour_tin_surface.points)) if contour_tin_surface is not None else 0}
    manual_labels, manual_info = load_manual_anchor_file(manual_anchor_path)
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
            pdf_scan["pdf_grade_labels_found"] = len(pdf_auto_labels)
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
            pdf_scan["pdf_hp_lp_labels_found"] = len(hp_lp_candidates)
            pdf_scan["pdf_hp_lp_labels_accepted_for_hard_hold"] = len(pdf_hp_lp_labels)
            pdf_scan["pdf_hp_lp_labels_review_only"] = len(hp_lp_candidates) - len(pdf_hp_lp_labels)
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
                "Automatic PDF HP/LP hard holds require a vector leader/tick endpoint and PDF-to-CAD alignment RMSE <= 1.0 ft with no alignment warning. "
                "All other PDF HP/LP detections are review-only and require a manual tick anchor."
            )
            if not require_manual_pdf_spots:
                pdf_labels = pdf_auto_labels
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
        "Certified civil PDF plan set is required. PDF callouts/details govern over CAD/DXF.",
        "Manual/CAD/PDF tick TC/BC/HP/LP controls hold first at the leader/arrow endpoint. v8.4.4 attempts automatic PDF-to-DXF matching and reads the PDF leader/arrow/tick endpoint instead of the text box center; if auto alignment is approximate, the point is still projected to selected BC and flagged in the source report.",
        "LandXML/Civil 3D TIN surface is the preferred calculation source when supplied.",
        "v8.5.1 Surface Mode: when no LandXML is supplied, GradeGuard builds a temporary proposed grading surface from 3D DXF contours and samples normal BC points from that surface. Contours are interpolated as a surface first; station interpolation is QA/fallback.",
        "DXF Topo-P-Surface / 3D feature lines are the next source.",
        "DXF contour/surface evidence is first safety-filtered: zero-Z placeholders, far-away segments, and outlier elevations are removed before staking calculations.",
        "Contours touching the BC layer are secondary; nearby contours are low confidence fallback.",
        "If user selects a flowline/gutter line, contour hits there are raised by curb height to calculate BC.",
        "If a contour touches selected BC, it holds exact contour elevation. If it touches selected FL/gutter/EP, GradeGuard adds the verified curb height from the PDF detail to make a BC-equivalent control. Points between clean controls are prorated/interpolated along the selected BC chain. Standard curb uses +0.50. Catch curb, spill curb, header curb, ramps, and transitions are flagged so the user can verify the correct height and direction from the certified PDF detail.",
        "v8.4.7 measures from each generated BC point along the selected curb station to the contour control station. Short-gap contours projected to BC are direct BC controls and do not receive curb height.",
        "v8.4.8 hard-locks true HP/LP callouts. HP/LP elevations hold exactly at the leader/tick/projected BC point and are never averaged, interpolated through, or overwritten by contour profile corrections."
    ]
    elevs = assign_elevations(raw_points, primary_labels, segments, verified_height, label_radius, contour_radius, False, contour_check_tolerance, landxml_surface=landxml_surface, surface_mode=surface_mode, contour_tin=contour_tin_surface)

    gen: List[GenPoint] = []
    pt = int(start_pt)
    for (x, y), (elev, src, issue, conf) in zip(raw_points, elevs):
        gen.append(GenPoint(point=pt, northing=y, easting=x, elevation=elev, source=src, issue=issue, confidence=conf))
        pt += 1
    apply_pc_pt_descriptions(gen, base_desc='BC')
    hp_lp_hard_locked = hard_lock_hp_lp_callout_elevations(gen, primary_labels, tolerance=2.50)
    dxf_stats["v8_4_8_hp_lp_hard_locked"] = hp_lp_hard_locked
    surface_mode_v851 = apply_contour_surface_mode_v851(gen, contour_tin_surface, segments, direct_tol=0.85, max_replace_diff=0.12, default_curb_height=verified_height, surface_mode=surface_mode)
    dxf_stats["v8_5_1_surface_mode_corrected"] = surface_mode_v851
    dxf_stats["v8_5_1_surface_mode_rule"] = "GradeGuard builds a temporary proposed grading surface from 3D DXF contours, samples normal BC points from that surface, then protects PDF/manual TC/BC/HP/LP callouts, storm low-point drainage intent, and direct contour hits. BC station interpolation is QA/fallback, not the primary surface." 
    dxf_stats["v8_5_3_surface_component_rule"] = "Surface Mode is component-aware. A contour/TIN on picked BC holds directly. A contour/TIN that controls picked FL/gutter/EP is raised by the standard/PDF curb height to stake BC. Direct BC contours still do not get curb height."
    dxf_stats["v8_5_4_curb_component_rule"] = "Locked survey rule: actual contour on selected BC holds exact elevation; actual contour on selected EP/FL/gutter is converted to BC by adding standard/PDF curb height; every other normal BC point is prorated by station between those BC-equivalent contour controls. Surface/TIN cannot overwrite these stronger curb-component controls."
    add_flow_warnings(gen, min_slope=min_slope)
    tag_high_low_points(gen, max_gap=max(spacing * 2.25, 55.0), min_change=0.02)
    normalize_high_low_descriptions(gen)
    station_profile_corrected = apply_bc_station_contour_profile_to_normal_points(gen, segments, contour_radius, verified_height)
    dxf_stats["v8_4_7_station_distance_contour_profile_corrected"] = station_profile_corrected
    direct_contour_holds_v850 = force_nearby_contour_direct_holds_v850(gen, segments, direct_tol=0.35, review_tol=1.25)
    dxf_stats["v8_5_0_direct_contour_holds"] = direct_contour_holds_v850
    profile_prorated_v850 = rebuild_bc_profile_from_contours_v850(gen, segments, max_gap=max(spacing * 3.25, 90.0))
    dxf_stats["v8_5_0_contour_profile_prorated"] = profile_prorated_v850
    surface_mode_v851_final = apply_contour_surface_mode_v851(gen, contour_tin_surface, segments, direct_tol=0.85, max_replace_diff=0.12, default_curb_height=verified_height, surface_mode=surface_mode)
    dxf_stats["v8_5_1_surface_mode_final_corrected"] = surface_mode_v851_final
    post_interp_filled = fill_missing_bc_elevations_by_station_interpolation(gen)
    dxf_stats["v8_4_1_post_station_interpolation_filled"] = post_interp_filled
    # Re-run flow/high-low tagging after the final v8.5 contour profile corrections.
    add_flow_warnings(gen, min_slope=min_slope)
    tag_high_low_points(gen, max_gap=max(spacing * 2.25, 55.0), min_change=0.02)
    normalize_high_low_descriptions(gen)
    hp_lp_cluster_holds_v857 = protect_close_hp_lp_clusters_v857(gen, cluster_tol=1.25, max_allowed_delta=0.20)
    dxf_stats["v8_5_7_hp_lp_cluster_holds"] = hp_lp_cluster_holds_v857
    dxf_stats["v8_4_8_hp_lp_hold_rule"] = "HP/LP plan callouts are hard vertical controls. Held HP/LP elevations are hard-locked to the callout value and are never averaged, interpolated through, overwritten by station contour profile correction, or zeroed by the sanitizer. HP/LP points with no trusted callout elevation remain 0.000 and flagged for review."
    dxf_stats["v8_4_2_hp_lp_hold_rule"] = "HP/LP plan callouts are hard controls. Existing HP/LP elevations are never averaged; zero HP/LP points remain review items instead of being smoothed through."
    zeroed_review_count = sanitize_field_elevations(gen, unknown_to_zero=True)
    dxf_stats["field_elevations_zeroed_for_review"] = zeroed_review_count
    hp_lp_relocked_after_sanitize = hard_lock_hp_lp_callout_elevations(gen, primary_labels, tolerance=2.50)
    dxf_stats["v8_4_8_hp_lp_relocked_after_sanitize"] = hp_lp_relocked_after_sanitize
    normalize_high_low_descriptions(gen)

    csv_path = os.path.join(out_dir, "Generated_BC_Points.csv")
    debug_csv_path = os.path.join(out_dir, "Generated_BC_Points_with_sources.csv")
    report_path = os.path.join(out_dir, "GradeGuard_Generate_Report.html")
    confidence_path = os.path.join(out_dir, "GradeGuard_Confidence_Summary.csv")
    conflict_path = os.path.join(out_dir, "GradeGuard_Surface_Conflict_Report.csv")
    write_csv(gen, csv_path)
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

    manifest_path = write_v9_package_manifest(out_dir, csv_path, debug_csv_path, report_path, confidence_path, conflict_path, created_flow_pdf or "", dxf_stats)
    dxf_stats["v9_0_package_manifest"] = manifest_path
    write_report(gen, layers, pdf_scan, dxf_stats, report_path)
    return csv_path, debug_csv_path, report_path, len(gen), len([p for p in gen if p.issue]), len([p for p in gen if p.elevation is not None])





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
                    add_struct(sum(xs)/len(xs), sum(ys)/len(ys), "", "DXF closed structure/polyline center", layer, handle)
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
        for idx, label in labels:
            win = text[max(0, idx-350): idx+700].replace("\n", " ")
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

def load_manual_flout_points(path: str) -> List[Dict[str, Any]]:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("points", []) if isinstance(data, dict) else []
    except Exception:
        pass
    return []

def save_manual_flout_points(path: str, points: List[Dict[str, Any]], cad_path: str):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cad_path": cad_path, "points": points}, f, indent=2)
    except Exception:
        pass

def run_flout_generation(cad_path: str, pdf_path: str, out_dir: str, start_pt: int, find_mode: str, desc_format: str, search_radius: float, manual_path: str = "") -> Tuple[str, str, int, int, int]:
    os.makedirs(out_dir, exist_ok=True)
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
        if outs:
            elev=min(outs); conf="HIGH"; issue=""
            assigned += 1
        elif invs:
            elev=min(invs); conf="MEDIUM"; issue="No explicit OUT found. Used lowest nearby invert/FL. Review in/out direction."
            assigned += 1; review += 1
        else:
            issue="No FL OUT / invert elevation found nearby. Review plans."
            review += 1
        desc=build_flout_description(label, pipe, desc_format, review=(elev is None))
        src="; ".join(source_parts[:3]) if source_parts else f"Structure center from {st['source']} layer {st['layer']}"
        points.append(GenPoint(start_pt+i, st["y"], st["x"], elev, desc, src, issue, conf))
    # Add manually picked structure center / FL OUT records last. These are high confidence
    # because the user clicked the structure center and keyed the flowline out data.
    manual = load_manual_flout_points(manual_path)
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
        points.append(GenPoint(start_pt+len(points), y, x, elev, desc, f"{pick_source} / keyed FL OUT", issue, "HIGH" if elev is not None else "REVIEW"))

    csv_path=os.path.join(out_dir,"Generated_FL_OUT_Points.csv")
    debug_path=os.path.join(out_dir,"Generated_FL_OUT_QA_Report.csv")
    write_csv(points, csv_path)
    write_debug_csv(points, debug_path)
    return csv_path, debug_path, len(points), assigned, review



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


def enforce_sidewalk_local_reasonableness(points: List[Point2], elevs: List[Tuple[Optional[float], str, str, str]], max_local_jump: float = 0.75, search_radius: float = 35.0) -> List[Tuple[Optional[float], str, str, str]]:
    """Protect sidewalk staking from bad contour/TIN triangles.

    Sidewalk grades should be interpolated locally.  If one point is several feet
    different from nearby sidewalk points, it is almost always a bad contour/TIN
    association from a distant contour or an open contour triangle.  This routine
    does not create design data, but it prevents obvious bad outliers from being
    exported as final field points.  It replaces only large local outliers and
    writes a review note in the debug CSV.
    """
    if not points or len(points) != len(elevs):
        return elevs
    out=[list(e) for e in elevs]
    zs=[e[0] for e in elevs]
    for i,p in enumerate(points):
        zi=out[i][0]
        if zi is None:
            continue
        neigh=[]
        for j,q in enumerate(points):
            if i==j or zs[j] is None:
                continue
            d=math.hypot(q[0]-p[0], q[1]-p[1])
            if 0.1 < d <= search_radius:
                neigh.append((d,float(zs[j])))
        if len(neigh) < 4:
            continue
        neigh.sort(key=lambda x:x[0])
        vals=[z for d,z in neigh[:10]]
        vals.sort()
        med=vals[len(vals)//2]
        if abs(float(zi)-med) > max_local_jump:
            old=float(zi)
            out[i][0]=round(med,3)
            out[i][1]=(out[i][1] or '') + f"; local sidewalk sanity adjusted {old:.3f} to nearby grade {med:.3f}"
            out[i][2]=((out[i][2] + ' ') if out[i][2] else '') + f"Review: sidewalk elevation was a local outlier by {abs(old-med):.2f} ft. Likely bad distant contour/TIN hit; adjusted to nearby local sidewalk grade."
            out[i][3]=out[i][3] or 'LOCAL_REVIEW'
    return [tuple(e) for e in out]


def run_edge_sidewalk_generation(cad_path: str, pdf_path: str, surface_path: str, out_dir: str, start_pt: int, spacing: float, description: str, elev_layer_keywords: str, contour_radius: float, selected_sidewalk_path: str = "", cross_slope_percent: float = 0.0, sidewalk_width: float = 0.0, cross_slope_adjust: str = "direct", auto_detect_width: bool = True):
    """Generate edge-of-sidewalk staking points from selected sidewalk edge geometry.

    This is intentionally separate from BC staking. It uses the same surface/contour
    elevation engine, but descriptions default to SIDEWALK and it does not apply curb height.
    """
    os.makedirs(out_dir, exist_ok=True)
    if not cad_path or not os.path.exists(cad_path):
        raise RuntimeError("Pick a Civil CAD DXF first.")
    if cad_path.lower().endswith(".dwg"):
        raise RuntimeError("DWG files need to be converted to DXF first.")
    selected_handles = load_selected_curb_handles(selected_sidewalk_path)
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
    landxml_surface = safe_load_landxml_surface(surface_path)
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
    write_csv(gen, csv_path)
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
    return csv_path, debug_path, report_path, len(gen), len([p for p in gen if p.elevation is not None]), len([p for p in gen if p.elevation is None])


def run_grade_grid_generation(cad_path: str, surface_path: str, out_dir: str, start_pt: int, grid_spacing: float, subtract_amount: float, description: str, elev_layer_keywords: str, contour_radius: float, selected_curb_path: str = ""):
    """Generate a simple finished grade or subgrade grid across the selected curb/site extents.

    This is intentionally separate from BC staking.  CAD/DXF supplies the horizontal limits,
    while LandXML/Topo-P-Surface/contours supply finished grade.  For SUBGRADE, the user-entered
    amount is subtracted from finished grade.  For FINISHED GRADE, subtract should be 0.
    """
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(cad_path)[1].lower()
    if ext == ".dwg":
        raise RuntimeError("DWG files need to be converted to DXF first. Export from Civil 3D or use ODA File Converter, then load the DXF.")
    if ext != ".dxf":
        raise RuntimeError("Grade grid currently reads DXF. Save the civil CAD as DXF first.")
    if grid_spacing <= 0:
        raise RuntimeError("Grid spacing must be greater than zero.")

    elev_keywords = [x.strip() for x in re.split(r"[,;]", elev_layer_keywords) if x.strip()]
    # Use the selected BC objects if present for the grid limits; otherwise use auto curb detection.
    selected_handles = load_selected_curb_handles(selected_curb_path)
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

    landxml_surface = safe_load_landxml_surface(surface_path)
    # For site grade grids, contours/surfaces represent finished grade directly.  Never apply curb height here.
    elevs = assign_elevations(grid, [], segments, 0.0, 0.0, contour_radius, False, 0.15, landxml_surface=landxml_surface, surface_mode="bc")

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
    write_csv(pts, csv_path)
    write_debug_csv(pts, debug_path)
    return csv_path, debug_path, len(pts), len([p for p in pts if p.elevation is not None]), len([p for p in pts if p.elevation is None])



def safe_load_landxml_surface(path: str) -> Optional[LandXMLSurface]:
    """Load LandXML only when the selected file is actually XML/LandXML.

    Many jobs only have 2D DXF plus 3D contour polylines.  In that workflow the
    LandXML box should be blank.  If a DXF, PDF, DWG, or bad XML is accidentally
    left in the surface slot, GradeGuard should keep going and use DXF contours
    instead of crashing.
    """
    if not path or not str(path).strip():
        return None
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in (".xml", ".landxml"):
        return None
    try:
        return load_landxml_surface(path)
    except Exception:
        return None


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
    def __init__(self):
        set_gradeguard_app_id()
        super().__init__()
        self.title(f"Schoel GradeGuard v{APP_VERSION} Multi-Job")
        apply_gradeguard_icon(self)
        self.geometry("1180x820")
        self.minsize(1000, 700)

        self.cad_var = tk.StringVar()
        self.surface_var = tk.StringVar()
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

    def _build(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#17212e"
        fg = "#e7edf5"
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, foreground=fg)
        style.configure("TLabelframe.Label", background=bg, foreground=fg, font=("Arial", 12, "bold"))
        style.configure("TButton", padding=(10, 7), background="#2f435c", foreground="#ffffff")
        style.configure("Accent.TButton", padding=(16, 10), background="#6b8e23", foreground="#ffffff", font=("Arial", 13, "bold"))
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TEntry", fieldbackground="#eef3f8", foreground="#111111")

        # Scrollable main screen so lower sections never get cut off on laptops or lower resolution monitors.
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=bg, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        root = ttk.Frame(canvas, padding=14)
        root_window = canvas.create_window((0, 0), window=root, anchor="nw")

        def _on_frame_configure(event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(root_window, width=event.width)

        def _on_mousewheel(event):
            # Windows uses event.delta; Linux may use Button-4/Button-5.
            if getattr(event, "delta", 0):
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        root.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        header = ttk.Frame(root)
        header.pack(fill="x")
        self.logo_img = None
        logo_path = resource_path("gradeguard_logo.png")
        if os.path.exists(logo_path):
            try:
                img = tk.PhotoImage(file=logo_path)
                factor = max(1, int(img.width()/210))
                self.logo_img = img.subsample(factor, factor)
                ttk.Label(header, image=self.logo_img).pack(side="left", padx=(0, 14))
            except Exception:
                pass
        title_box = ttk.Frame(header)
        title_box.pack(side="left", fill="x", expand=True)
        ttk.Label(title_box, text="Schoel GradeGuard", font=("Arial", 18, "bold")).pack(anchor="w")
        ttk.Label(title_box, text="Multi-job workflow: load CAD, load surface, pick/confirm BC, generate BC staking points", font=("Arial", 12)).pack(anchor="w", pady=(2,0))
        ttk.Label(title_box, text="Tip: use the mouse wheel to scroll this screen if your monitor cuts off lower sections.", font=("Arial", 9, "italic")).pack(anchor="w", pady=(2,0))



        # BIG VISIBLE OFFSET SECTION.  This stays near the top so it cannot be missed.
        offset_top = ttk.LabelFrame(root, text="CONTRACTOR OFFSET", padding=14)
        offset_top.pack(fill="x", pady=(8, 6))
        ttk.Checkbutton(offset_top, text="Create separate offset CSV and offset line DXF", variable=self.create_offset_var).grid(row=0, column=0, columnspan=2, sticky="w", padx=(0,18))
        ttk.Label(offset_top, text="Offset distance from BC:", font=("Arial", 10, "bold")).grid(row=0, column=2, sticky="e")
        ttk.Entry(offset_top, textvariable=self.offset_distance_var, width=10).grid(row=0, column=3, sticky="w", padx=(6,18))
        ttk.Label(offset_top, text="ft").grid(row=0, column=4, sticky="w")
        ttk.Label(offset_top, text="Use the single GENERATE BC POINTS button below. Offset files are created when this box is checked.", font=("Arial", 10, "italic")).grid(row=0, column=5, sticky="w", padx=(20,0))
        ttk.Label(offset_top, text="Offset direction:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(8,0))
        ttk.Radiobutton(offset_top, text="Away from picked FL/gutter", variable=self.offset_mode_var, value="away_from_flowline").grid(row=1, column=1, sticky="w", pady=(8,0))
        ttk.Radiobutton(offset_top, text="Toward picked FL/gutter", variable=self.offset_mode_var, value="toward_flowline").grid(row=1, column=2, sticky="w", pady=(8,0))
        ttk.Radiobutton(offset_top, text="Left of BC line", variable=self.offset_mode_var, value="left").grid(row=1, column=3, sticky="w", pady=(8,0))
        ttk.Radiobutton(offset_top, text="Right of BC line", variable=self.offset_mode_var, value="right").grid(row=1, column=4, sticky="w", pady=(8,0))
        ttk.Checkbutton(offset_top, text="Flip side", variable=self.offset_reverse_var).grid(row=1, column=5, sticky="w", pady=(8,0), padx=(10,0))
        ttk.Label(offset_top, text="Optional. Normal BC CSV is always created. Offset CSV and DXF are separate.", font=("Arial", 10, "italic"), wraplength=1040).grid(row=2, column=0, columnspan=6, sticky="w", pady=(8,0))
        offset_top.columnconfigure(5, weight=1)

        gen_settings_top = ttk.LabelFrame(root, text="Point Numbering and BC Point Spacing", padding=12)
        gen_settings_top.pack(fill="x", pady=(6, 8))
        ttk.Label(gen_settings_top, text="Starting point number:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Entry(gen_settings_top, textvariable=self.start_var, width=12).grid(row=0, column=1, sticky="w", padx=(6,24))
        ttk.Label(gen_settings_top, text="Distance to add BC points on straight curb:", font=("Arial", 10, "bold")).grid(row=0, column=2, sticky="w")
        ttk.Entry(gen_settings_top, textvariable=self.spacing_var, width=10).grid(row=0, column=3, sticky="w", padx=(6,6))
        ttk.Label(gen_settings_top, text="ft").grid(row=0, column=4, sticky="w", padx=(0,24))
        ttk.Label(gen_settings_top, text="Curb detail from PDF:", font=("Arial", 10, "bold")).grid(row=0, column=5, sticky="w")
        curb_combo = ttk.Combobox(gen_settings_top, textvariable=self.curb_type_var, values=["Standard curb (+0.50)", "Catch curb per PDF (+0.42)", "Spill curb per PDF (+0.58)", "Custom PDF detail height"], width=24, state="readonly")
        curb_combo.grid(row=0, column=6, sticky="w", padx=(6,6))
        ttk.Entry(gen_settings_top, textvariable=self.height_var, width=8).grid(row=0, column=7, sticky="w", padx=(6,4))
        ttk.Label(gen_settings_top, text="ft").grid(row=0, column=8, sticky="w")
        def _sync_curb_height(*_args):
            v = self.curb_type_var.get()
            if v.startswith("Standard"):
                self.height_var.set("0.50")
            elif v.startswith("Catch"):
                self.height_var.set("0.42")
            elif v.startswith("Spill"):
                self.height_var.set("0.58")
        self.curb_type_var.trace_add("write", _sync_curb_height)
        ttk.Label(gen_settings_top, text="Curve rule: PC, MID, PT only. Field CSV has no header. Descriptions: BC PC, BC PT, BC HP, BC LP, or BC. v8.4.5 uses standard +0.50 unless the certified PDF detail specifically supports catch +0.42, spill +0.58, or a custom height.", font=("Arial", 10, "italic"), wraplength=1040).grid(row=1, column=0, columnspan=9, sticky="w", pady=(8,0))

        project = ttk.LabelFrame(root, text="Project", padding=10)
        project.pack(fill="x", pady=(8, 6))
        ttk.Label(project, textvariable=self.project_var, font=("Arial", 11, "bold")).pack(side="left", fill="x", expand=True, anchor="w")
        ttk.Button(project, text="New Job", command=self.new_job).pack(side="right", padx=(6,0))
        ttk.Button(project, text="Open Job", command=self.open_job).pack(side="right", padx=(6,0))
        ttk.Button(project, text="Save Job", command=self.save_job).pack(side="right", padx=(6,0))

        steps = ttk.LabelFrame(root, text="Start Here", padding=14)
        steps.pack(fill="x", pady=(6, 6))
        self._file_row(steps, 0, "1. Main CAD/DXF for geometry, curbs, sidewalk, storm/san", self.cad_var, self.pick_cad)
        self._file_row(steps, 1, "2. LandXML/XML proposed surface, optional. Leave blank for 2D CAD + contours", self.surface_var, self.pick_surface)
        self._file_row(steps, 2, "3. Certified civil PDF plan set, REQUIRED for BC", self.pdf_var, self.pick_pdf)
        self._file_row(steps, 3, "4. Output folder", self.out_var, self.pick_out)
        pickbar = ttk.Frame(steps)
        pickbar.grid(row=4, column=0, columnspan=3, sticky="we", pady=(10, 2))
        ttk.Label(pickbar, text="Generic curb layer? Pick the actual curb components:", font=("Arial", 10, "bold")).pack(side="left", padx=(0, 12))
        ttk.Button(pickbar, text="PICK TRUE BACK OF CURB", style="Accent.TButton", command=self.pick_back_of_curb_entities).pack(side="left", padx=(0,8))
        ttk.Button(pickbar, text="PICK FLOWLINE / GUTTER", command=self.pick_flowline_entities).pack(side="left")

        qa = ttk.LabelFrame(root, text="Automatic QA/QC", padding=12)
        qa.pack(fill="x", pady=(6, 6))
        ttk.Label(
            qa,
            text="No manual QA checklist required. GradeGuard automatically requires the civil PDF, keeps PDF callouts/details over CAD/DXF, checks selected BC/FL geometry, builds station-based contour controls, protects HP/LP break points, flags unsupported 0.000 elevations, and writes the QA/source report.",
            font=("Arial", 10, "italic"),
            wraplength=1080,
        ).grid(row=0, column=0, sticky="w")

        mode = ttk.LabelFrame(root, text="Elevation Source", padding=10)
        mode.pack(fill="x", pady=(6, 6))
        mode.columnconfigure(0, weight=1)
        mode.columnconfigure(1, weight=1)

        src_box = ttk.Frame(mode)
        src_box.grid(row=0, column=0, sticky="nwe", padx=(0, 18))
        ttk.Label(src_box, text="Use elevations from:", font=("Arial", 10, "bold")).pack(anchor="w")
        ttk.Radiobutton(src_box, text="LandXML / Civil 3D surface first", variable=self.source_mode, value="surface").pack(anchor="w")
        ttk.Radiobutton(src_box, text="3D DXF / Topo-P-Surface first", variable=self.source_mode, value="dxf_surface").pack(anchor="w")
        ttk.Radiobutton(src_box, text="Contours touching curb only", variable=self.source_mode, value="contours").pack(anchor="w")

        interp_box = ttk.Frame(mode)
        interp_box.grid(row=0, column=1, sticky="nwe")
        ttk.Label(interp_box, text="Surface / contour meaning:", font=("Arial", 10, "bold")).pack(anchor="w")
        ttk.Radiobutton(interp_box, text="Top/back of curb / grading surface: use directly", variable=self.surface_interpret_var, value="bc").pack(anchor="w")
        ttk.Radiobutton(interp_box, text="Auto: direct BC contours hold, pavement/EP surface adds curb height", variable=self.surface_interpret_var, value="auto").pack(anchor="w")
        ttk.Radiobutton(interp_box, text="FL/gutter/pavement surface: add curb height to BC", variable=self.surface_interpret_var, value="flowline").pack(anchor="w")

        ttk.Label(mode, text="Rule: picked/held TC/HP/LP points hold first. Contours are secondary. Contour on BC = exact elevation. Contour on FL/gutter = + curb height from the plan detail.", font=("Arial", 10, "italic"), wraplength=1050).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8,0))
        ttk.Label(mode, text="2D curb workflow: DXF supplies horizontal curb only. Pick True BC and Flowline/Gutter, then use LandXML, CAD contours, PDF detail/callouts, or manual holds for elevations.", font=("Arial", 10, "italic"), wraplength=1050).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4,0))

        flout = ttk.LabelFrame(root, text="Storm / Sanitary FL OUT Points", padding=12)
        flout.pack(fill="x", pady=(6, 6))
        ttk.Label(flout, text="Start point:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Entry(flout, textvariable=self.flout_start_var, width=10).grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Label(flout, text="Find:", font=("Arial", 10, "bold")).grid(row=0, column=2, sticky="e")
        ttk.Combobox(flout, textvariable=self.flout_find_var, values=["Both", "Storm", "Sanitary"], width=12, state="readonly").grid(row=0, column=3, sticky="w", padx=(6, 18))
        ttk.Label(flout, text="Search radius:", font=("Arial", 10, "bold")).grid(row=0, column=4, sticky="e")
        ttk.Entry(flout, textvariable=self.flout_search_radius_var, width=8).grid(row=0, column=5, sticky="w", padx=(6, 4))
        ttk.Label(flout, text="ft").grid(row=0, column=6, sticky="w", padx=(0, 18))
        ttk.Label(flout, text="Description:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(8,0))
        ttk.Combobox(flout, textvariable=self.flout_desc_format_var, values=["Structure + Pipe Size + FL-OUT", "Structure + FL-OUT", "Pipe Size + FL-OUT", "FL-OUT only"], width=32, state="readonly").grid(row=1, column=1, columnspan=3, sticky="w", padx=(6, 18), pady=(8,0))
        ttk.Button(flout, text="PICK STRUCTURE CENTERS / FL OUT", command=self.pick_flout_centers).grid(row=1, column=4, sticky="e", padx=(12,8), pady=(8,0))
        ttk.Button(flout, text="GENERATE FL OUT POINTS", style="Accent.TButton", command=self.generate_flout_points).grid(row=1, column=5, columnspan=2, sticky="e", padx=(12,0), pady=(8,0))
        ttk.Label(flout, text="Creates a separate CSV with no header. Auto mode reads structure centers from DXF and lowest OUT invert/flowline from CAD/PDF labels. If structures do not export cleanly, click PICK STRUCTURE CENTERS / FL OUT and key the label, pipe size, and FL OUT elevation.", font=("Arial", 10, "italic"), wraplength=1050).grid(row=2, column=0, columnspan=7, sticky="w", pady=(8,0))
        flout.columnconfigure(4, weight=1)


        esw = ttk.LabelFrame(root, text="Edge of Sidewalk Staking", padding=12)
        esw.pack(fill="x", pady=(6, 6))
        ttk.Label(esw, text="Start point:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Entry(esw, textvariable=self.esw_start_var, width=10).grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Label(esw, text="Spacing on straight sidewalk edge:", font=("Arial", 10, "bold")).grid(row=0, column=2, sticky="e")
        ttk.Entry(esw, textvariable=self.esw_spacing_var, width=10).grid(row=0, column=3, sticky="w", padx=(6, 4))
        ttk.Label(esw, text="ft").grid(row=0, column=4, sticky="w", padx=(0, 18))
        ttk.Label(esw, text="Description:", font=("Arial", 10, "bold")).grid(row=0, column=5, sticky="e")
        ttk.Entry(esw, textvariable=self.esw_desc_var, width=16).grid(row=0, column=6, sticky="w", padx=(6, 18))
        ttk.Button(esw, text="PICK EDGE OF SIDEWALK", command=self.pick_edge_sidewalk_entities).grid(row=0, column=7, sticky="e", padx=(12, 8))
        ttk.Button(esw, text="GENERATE EDGE OF SIDEWALK POINTS", style="Accent.TButton", command=self.generate_edge_sidewalk_points).grid(row=0, column=8, sticky="e")

        ttk.Label(esw, text="2D sidewalk workflow:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(8,0))
        ttk.Label(esw, text="Leave LandXML blank if there is no XML surface. Pick the sidewalk edge, then use 3D DXF contour layers such as P-Topo Major Contours / P-Topo Minor Contours for elevations.", font=("Arial", 10, "italic"), wraplength=950).grid(row=1, column=1, columnspan=8, sticky="w", pady=(8,0))

        ttk.Label(esw, text="Cross slope:", font=("Arial", 10, "bold")).grid(row=2, column=0, sticky="w", pady=(8,0))
        ttk.Entry(esw, textvariable=self.esw_cross_slope_var, width=8).grid(row=2, column=1, sticky="w", padx=(6,4), pady=(8,0))
        ttk.Label(esw, text="%",).grid(row=2, column=1, sticky="w", padx=(70,0), pady=(8,0))
        ttk.Label(esw, text="Sidewalk width:", font=("Arial", 10, "bold")).grid(row=2, column=2, sticky="e", pady=(8,0))
        ttk.Entry(esw, textvariable=self.esw_width_var, width=8).grid(row=2, column=3, sticky="w", padx=(6,4), pady=(8,0))
        ttk.Label(esw, text="ft",).grid(row=2, column=4, sticky="w", pady=(8,0))
        ttk.Label(esw, text="Adjustment:", font=("Arial", 10, "bold")).grid(row=2, column=5, sticky="e", pady=(8,0))
        ttk.Combobox(esw, textvariable=self.esw_adjust_var, values=["direct", "auto from contours", "add cross slope", "subtract cross slope"], width=18, state="readonly").grid(row=2, column=6, columnspan=2, sticky="w", padx=(6,18), pady=(8,0))

        ttk.Label(esw, text="Use direct when the selected edge elevation is being calculated. Auto samples perpendicular left/right of the linear sidewalk run to help determine which way water falls. Cross slope is always perpendicular to the walk. Example: 10 ft sidewalk at 1% = 0.10 ft elevation difference.", font=("Arial", 10, "italic"), wraplength=1150).grid(row=3, column=0, columnspan=9, sticky="w", pady=(8,0))
        esw.columnconfigure(8, weight=1)

        grade_grid = ttk.LabelFrame(root, text="Subgrade / Finished Grade Grid", padding=12)
        grade_grid.pack(fill="x", pady=(6, 6))
        ttk.Label(grade_grid, text="Grid type:", font=("Arial", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(grade_grid, text="SUBGRADE", variable=self.grade_grid_type_var, value="SUBGRADE").grid(row=0, column=1, sticky="w", padx=(6, 12))
        ttk.Radiobutton(grade_grid, text="FINISHED GRADE", variable=self.grade_grid_type_var, value="FINISHED GRADE").grid(row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Label(grade_grid, text="Start point:", font=("Arial", 10, "bold")).grid(row=0, column=3, sticky="e")
        ttk.Entry(grade_grid, textvariable=self.grade_grid_start_var, width=10).grid(row=0, column=4, sticky="w", padx=(6, 18))
        ttk.Label(grade_grid, text="Grid spacing:", font=("Arial", 10, "bold")).grid(row=0, column=5, sticky="e")
        ttk.Entry(grade_grid, textvariable=self.grade_grid_spacing_var, width=10).grid(row=0, column=6, sticky="w", padx=(6, 4))
        ttk.Label(grade_grid, text="ft").grid(row=0, column=7, sticky="w")
        ttk.Label(grade_grid, text="Subtract from finished grade:", font=("Arial", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(8,0))
        ttk.Entry(grade_grid, textvariable=self.grade_grid_subtract_var, width=10).grid(row=1, column=1, sticky="w", padx=(6, 4), pady=(8,0))
        ttk.Label(grade_grid, text="ft  (used for SUBGRADE; set 0.00 for FINISHED GRADE)").grid(row=1, column=2, columnspan=4, sticky="w", pady=(8,0))
        ttk.Button(grade_grid, text="GENERATE GRADE GRID", style="Accent.TButton", command=self.generate_grade_grid).grid(row=1, column=6, columnspan=2, sticky="e", padx=(12,0), pady=(8,0))
        ttk.Label(grade_grid, text="Creates a separate CSV with no header. Description is SUBGRADE or FINISHED GRADE. Uses LandXML / Topo-P-Surface / contours as finished grade, then subtracts the amount you enter.", font=("Arial", 10, "italic"), wraplength=1050).grid(row=2, column=0, columnspan=8, sticky="w", pady=(8,0))
        grade_grid.columnconfigure(2, weight=1)

        action = ttk.Frame(root)
        action.pack(fill="x", pady=12)
        ttk.Button(action, text="Auto Detect BC Layers", command=self.detect_layers).pack(side="left", padx=(0,8))
        ttk.Label(action, text="Pick tools are in each section: BC/FL in Start Here, Storm/San in FL OUT, and Sidewalk in Edge of Sidewalk.", font=("Arial", 10, "italic")).pack(side="left", padx=(10,0))
        self.generate_button = ttk.Button(action, text="GENERATE BC POINTS", style="Accent.TButton", command=self.generate)
        self.generate_button.pack(side="right")

        self.layer_box = tk.Text(root, height=4, wrap="word", bg="#0f1722", fg="#e7edf5", insertbackground="#e7edf5")
        self.layer_box.pack(fill="x", pady=(2, 10))
        self.layer_box.insert("1.0", 'BC layer detection will appear here. If the engineer uses generic layers like P-CURB, P-CURBS, HARDSCAPE, SITE-HARDSCAPE, or puts face, gutter, and back of curb all on one layer, click "Pick True Back of Curb in DXF" and manually select only the back-of-curb objects. If contours hit the gutter/flowline instead, click "Pick Flowline/Gutter in DXF" too. Contour on BC = exact elevation. Contour on FL/gutter = + verified catch/spill/custom curb height from the PDF detail. In 2D CAD, linework is geometry only, so use LandXML/CAD contours/PDF callouts/manual holds for elevations. For edge-of-sidewalk staking, use the Edge of Sidewalk section. GradeGuard looks for ANY layer name containing SIDEWALK, and verify sidewalk/turn-down/ramp details in the PDF scan report.')

        self.log = tk.Text(root, height=7, wrap="word", bg="#0f1722", fg="#e7edf5", insertbackground="#e7edf5")
        self.log.pack(fill="both", expand=True, pady=(2, 0))
        self.log_msg("Ready. Multi-job mode. Use LandXML/Civil 3D surface whenever possible. DXF remains fixed and is never scaled.")

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
            "version": "8.5.5",
            "cad_path": self.cad_var.get().strip(),
            "surface_path": self.surface_var.get().strip(),
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
        }

    def _load_project_data(self, data):
        self.cad_var.set(data.get("cad_path", ""))
        self.surface_var.set(data.get("surface_path", ""))
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

    def new_job(self):
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

    def pick_surface(self):
        p = filedialog.askopenfilename(title="Pick LandXML Surface", filetypes=[("LandXML/XML", "*.xml *.landxml"), ("All files", "*.*")])
        if p:
            self.surface_var.set(p)

    def pick_pdf(self):
        p = filedialog.askopenfilename(title="Pick PDF plan set", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if p:
            self.pdf_var.set(p)

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
            manual=load_manual_flout_points(self.manual_flout_path)
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
        self.generate_button.configure(state="normal", text="GENERATE BC POINTS")
        self.config(cursor="")

    def _generation_finished(self, result, out):
        self._reset_generation_ui()
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
            messagebox.showinfo("Done", f"Generated {total} BC points.\nPoints with elevations: {assigned}\nReview items: {issues}\n\nCreated v9.0 Survey Package outputs in the same folder:\n- Confidence Summary\n- Surface Conflict Report\n- Water Flow Map\n- Office QA Report\n\n{csv_path}")
        except Exception as e:
            self.log_msg("ERROR:")
            self.log_msg(str(e))
            self.log_msg(traceback.format_exc())
            messagebox.showerror("Error", str(e))

    def _generation_failed(self, error, details):
        self._reset_generation_ui()
        self.log_msg("ERROR:")
        self.log_msg(error)
        self.log_msg(details)
        messagebox.showerror("Error", error)

if __name__ == "__main__":
    app = GradeGuardApp()
    app.mainloop()
