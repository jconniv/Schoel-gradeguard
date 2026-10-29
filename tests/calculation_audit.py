"""Independent, synthetic calculation audit. Failures are never treated as success.

Run: python tests/calculation_audit.py --report <new-report.json>
This does not modify application code, user inputs, or deployed executables.
Fixtures are invented local coordinates in feet unless explicitly labelled metric.
Passes establish only the stated example, never general survey certification.
"""
import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import random
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument('--source', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--report', required=True)
args = parser.parse_args()
sys.path.insert(0, args.source)
import schoel_gradeguard as gg
import ezdxf
import fitz

CASES = []
RESULTS = []


def case(code, area, requirement):
    def register(fn):
        CASES.append((code, area, requirement, fn))
        return fn
    return register


def equal(actual, expected, tolerance=None):
    if tolerance is None:
        ok = actual == expected
    elif isinstance(expected, (list, tuple)):
        ok = len(actual) == len(expected) and all(abs(a-b) <= tolerance for a, b in zip(actual, expected))
    else:
        ok = actual is not None and math.isfinite(actual) and abs(actual-expected) <= tolerance
    return {'status': 'PASS' if ok else 'FAIL', 'expected': expected, 'actual': actual, 'tolerance': tolerance}


def reset():
    for name in ('CURB_SEGMENTS', 'FLOWLINE_SEGMENTS', 'CURB_CONTROL_POINTS', 'CURB_PC_POINTS', 'CURB_PT_POINTS', 'DXF_LEADER_SEGMENTS'):
        setattr(gg, name, [])
    gg.CONTOUR_POINT_OVERRIDES = {}
    gg.CURRENT_CURB_HEIGHT = 0.5
    gg.CURRENT_CURB_HEIGHT_VERIFIED_FROM_PDF = True


def contour(x, z, y0=-20, y1=20):
    return gg.ElevSegment((x, y0), (x, y1), z, 'P-CONTOUR', 'Synthetic contour')


def plane(z=600):
    return gg.LandXMLSurface([((0, 0, z), (100, 0, z+10), (0, 100, z+20))])


def gp(x, y, z, number=1, **kw):
    return gg.GenPoint(number, y, x, z, **kw)


def xml_surface(name, z):
    # NEZ coordinates where N > 1.5 E, so the current heuristic happens to work.
    return f'<Surface name="{name}"><Definition surfType="TIN"><Pnts><P id="1">3000 1000 {z}</P><P id="2">3000 1100 {z}</P><P id="3">3100 1000 {z}</P></Pnts><Faces><F>1 2 3</F></Faces></Definition></Surface>'


def xml_file(folder, surfaces, units='<Imperial linearUnit="USSurveyFoot" elevationUnit="feet"/>'):
    path = Path(folder)/'surface.xml'
    path.write_text(f'<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2"><Units>{units}</Units><Surfaces>{surfaces}</Surfaces></LandXML>', encoding='utf-8')
    return str(path)


def cad_fixture(folder, layer='P-BC', z=600, end_z=None, units=21):
    doc = ezdxf.new('R2010')
    doc.units = units
    msp = doc.modelspace()
    curb = msp.add_line((0, 0), (40, 0), dxfattribs={'layer': layer})
    if z is not None:
        msp.add_line((0, 10, z), (40, 10, z if end_z is None else end_z), dxfattribs={'layer': 'P-CONTOUR'})
    path = Path(folder)/'fixture.dxf'
    doc.saveas(path)
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), 'SYNTHETIC SOFTWARE TEST. NOT CERTIFIED PLANS.')
    pdf.save(str(Path(folder)/'fixture.pdf'))
    pdf.close()
    return str(path), str(Path(folder)/'fixture.pdf'), curb.dxf.handle


@case('G01', 'Geometry', '3-4-5 distance is 5 coordinate units.')
def _(): return equal(gg.dist((10, 20), (13, 24)), 5, 1e-12)


@case('G02', 'Geometry', 'Segment projection clamps to endpoint outside segment.')
def _(): return equal(gg.project_point_on_segment((20, 5), (0, 0), (10, 0)), (10, 0), 1e-12)


@case('G03', 'Geometry', 'A 62-foot straight run sampled at 25 feet yields 0, 25, 50, 62.')
def _(): return equal([p[0] for p in gg.sample_segment((0, 0), (62, 0), 25)], [0, 25, 50, 62])


@case('G04', 'Geometry', 'Crossing diagonals intersect at 5,5 and half station on each.')
def _():
    t, u, p = gg.segment_intersection((0, 0), (10, 10), (0, 10), (10, 0))
    return equal((t, u, *p), (0.5, 0.5, 5, 5), 1e-12)


@case('G05', 'Geometry', 'Quarter-circle ARC staking gives PC, true arc midpoint, PT.')
def _():
    arc = ezdxf.new().modelspace().add_arc((0, 0), 10, 0, 90)
    pts, _controls = gg.staking_points_from_entity(arc, 25)
    return equal(pts[1], (math.sqrt(50), math.sqrt(50)), 1e-9)


for code, bulge in [('G06', math.tan(math.pi/8)), ('G07', -math.tan(math.pi/8)), ('G08', 2.0), ('G09', -2.0)]:
    @case(code, 'Geometry', f'Bulge {bulge:.8f} follows the reference circle, including major arcs.')
    def _(bulge=bulge):
        # Independent library reference (not GradeGuard formula).
        center, _a, _b, radius = ezdxf.math.bulge_to_arc((0, 0), (10, 0), bulge)
        pts = gg.bulge_segment_points((0, 0), (10, 0), bulge, 0.25)
        return equal(max(abs(math.hypot(p[0]-center.x, p[1]-center.y)-radius) for p in pts), 0.0, 1e-8)


@case('G10', 'Geometry', 'Full ellipse covers the leftmost point at x=-10, not only parameters 0..1 radian.')
def _():
    ellipse = ezdxf.new().modelspace().add_ellipse((0, 0), major_axis=(10, 0), ratio=0.5)
    pts = gg.picker_entity_points(ellipse)
    return equal(min(x for x, _y in pts), -10, 0.02)


@case('G11', 'Geometry', 'ARC with negative extrusion preserves WCS start point.')
def _():
    arc = ezdxf.new().modelspace().add_arc((0, 0), 10, 0, 90, dxfattribs={'extrusion': (0, 0, -1)})
    return equal(gg.arc_points_from_entity(arc)[0], (arc.start_point.x, arc.start_point.y), 1e-9)


@case('I01', 'Imports', 'LandXML NEZ order is respected when E exceeds N.')
def _(): return equal(gg._parse_landxml_xyz('1000 2000 600'), (2000, 1000, 600))


@case('I02', 'Imports', 'LandXML NEZ order also works for N much greater than E.')
def _(): return equal(gg._parse_landxml_xyz('3000000 600000 612.345'), (600000, 3000000, 612.345))


@case('I03', 'Imports', 'Repeated point IDs in separate surfaces must not overwrite one surface with another.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        path = xml_file(folder, xml_surface('FG', 600)+xml_surface('EG', 700))
        # 9.3.1 requires explicit model selection rather than combining models.
        try:
            gg.load_landxml_surface(path)
            return equal('ambiguous file accepted', 'selection required')
        except ValueError:
            pass
        surfaces = [gg.load_landxml_surface(path, name) for name in ('FG', 'EG')]
        return equal([surface.triangles[0][0][2] for surface in surfaces], [600, 700])


@case('I04', 'Imports', 'A metric surface cannot silently enter the declared foot workflow without conversion/confirmation.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        path = xml_file(folder, xml_surface('Metric', 200), '<Metric linearUnit="meter" elevationUnit="meter"/>')
        try:
            result = gg.safe_load_landxml_surface(path)
            blocked = result is None
        except (ValueError, RuntimeError):
            blocked = True
        return equal(blocked, True)


@case('I05', 'Imports', 'An explicitly selected corrupt XML surfaces an error rather than silently falling back.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/'bad.xml'; path.write_text('<broken')
        try:
            gg.safe_load_landxml_surface(str(path)); raised = False
        except (ValueError, RuntimeError):
            raised = True
        return equal(raised, True)


@case('I06', 'Imports', 'DXF zero endpoint is preserved or rejected, never silently replaced by the opposite Z.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, _pdf, _handle = cad_fixture(folder, z=600, end_z=0)
        segs = gg.load_dxf_geometry_and_elevations(cad, ['P-BC'], ['CONTOUR'], 25)[3]
        result = equal(not segs or all(s.z2 == 0 for s in segs), True)
        result['imported_end_elevations'] = [s.z2 for s in segs]
        return result


@case('S01', 'Surface math', 'LandXML plane Z=600+0.1X+0.2Y gives 607.5 at 25,25.')
def _(): return equal(gg.landxml_elevation_at((25, 25), plane())[0], 607.5, 1e-9)


@case('S02', 'Surface math', 'Barycentric plane interpolation remains correct at large local grid coordinates.')
def _():
    surface = plane()
    surface.triangles = [tuple((x+600000, y+3000000, z) for x, y, z in tri) for tri in surface.triangles]
    return equal(gg.landxml_elevation_at((600025, 3000025), surface)[0], 607.5, 1e-8)


@case('S03', 'Surface safety', 'A point 1 foot outside a 1000-foot triangle must not receive SURFACE_HIGH.')
def _():
    surface = gg.LandXMLSurface([((0, 0, 600), (1000, 0, 610), (0, 1000, 620))])
    result = gg.landxml_elevation_at((-1, 10), surface)
    return equal(result[2] == 'SURFACE_HIGH', False)


@case('S04', 'Surface math', 'A far-outside point has no LandXML elevation.')
def _(): return equal(gg.landxml_elevation_at((10000, 10000), plane())[0], None)


@case('S05', 'Surface math', '3D feature-line midpoint elevation interpolates from 600 to 602 as 601.')
def _(): return equal(gg.segment_z_at_point((5, 0), gg.ElevSegment((0, 0), (10, 0), 600, 'FG', 'fixture', 602)), 601, 1e-12)


@case('S06', 'Surface math', 'Contour TIN reproduces a plane at an interior point.')
def _():
    tin = gg.build_contour_tin_surface([contour(0, 600, 0, 100), contour(100, 610, 0, 100)])
    return equal(gg.contour_tin_elevation((25, 40), tin)[0], 602.5, 1e-9)


@case('S07', 'Surface safety', 'Conflicting elevations at identical XY must not be averaged into a trusted invented surface.')
def _():
    tin = gg.build_contour_tin_surface([contour(0, 600, 0, 100), contour(0, 640, 0, 100), contour(100, 610, 0, 100)])
    z, src, conf = gg.contour_tin_elevation((0, 50), tin)
    result = equal(z is None or 'REVIEW' in conf, True)
    result['observed_elevation'] = z
    return result


@case('S08', 'Surface math', 'Perpendicular contour interpolation brackets 600/602 and returns 600.5 at one-quarter width.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((2.5, -10), (2.5, 10), 'SW')]
    return equal(gg.normal_contour_interpolation((2.5, 0), [contour(0, 600), contour(10, 602)])[0], 600.5, 1e-9)


@case('S09', 'Surface safety', 'Two contours on the same side of a point are not a spatial interpolation bracket.')
def _():
    z, src, conf, _d = gg.local_contour_prorated_elevation((0, 0), [contour(10, 600), contour(20, 602)], 35)
    result = equal(z is None or 'REVIEW' in conf, True)
    result['observed_elevation'] = z
    return result


@case('S10', 'Surface range', 'Local contour interpolation supports a valid ten-foot interval at 600/610.')
def _(): return equal(gg.local_contour_prorated_elevation((5, 0), [contour(0, 600), contour(10, 610)], 35)[0], 605, 1e-9)


@case('S11', 'Surface range', 'Local contour interpolation supports a valid 50/52-foot site.')
def _(): return equal(gg.local_contour_prorated_elevation((5, 0), [contour(0, 50), contour(10, 52)], 35)[0], 51, 1e-9)


@case('C01', 'Curb math', 'An explicit 600-foot FL plus a 0.5-foot curb gives 600.5.')
def _(): return equal(gg.nearest_label_elevation((0, 0), [gg.GradeLabel(0, 0, 'FL', 600, 'FL 600.00', 'CAD')], 0.5, 1)[0], 600.5, 1e-12)


@case('C02', 'Curb math', 'An explicit TC is held directly without adding curb height.')
def _(): return equal(gg.nearest_label_elevation((0, 0), [gg.GradeLabel(0, 0, 'TC', 600, 'TC 600.00', 'CAD')], 0.5, 1)[0], 600, 1e-12)


@case('C03', 'Curb controls', 'Bare BOC is ambiguous and cannot be automatically classified as BC.')
def _(): return equal(gg.parse_grade_text('BOC 612.34'), [('BOC_AMBIG', 612.34)])


@case('C04', 'Curb controls', 'HP BOC marked ambiguous must not still supply a numeric elevation through generic label lookup.')
def _():
    lab = gg.GradeLabel(0, 0, 'HP_BOC_AMBIG', 612.34, 'HP BOC 612.34', 'CAD text')
    return equal(gg.nearest_label_elevation((0, 0), [lab], 0.5, 1)[0], None)


@case('C05', 'Curb math', 'Station interpolation between 600/602 at 0/40 feet gives 601 at station 20.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (40, 0), 'BC')]
    return equal(gg.curb_chain_contour_elevation((20, 0), [contour(0, 600), contour(40, 602)], 50, 0.5)[0], 601, 1e-9)


@case('C06', 'Curb math', 'FL contour crossing projected to BC adds the confirmed 0.5-foot curb.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (40, 0), 'BC')]
    gg.FLOWLINE_SEGMENTS = [gg.CurbSegment((0, -3), (40, -3), 'FL')]
    return equal(gg.curb_chain_contour_elevation((20, 0), [contour(20, 600, -4, -2)], 50, 0.5)[0], 600.5, 1e-9)


@case('C07', 'Curb controls', 'A matching trusted HP callout holds its exact 612.345-foot value.')
def _():
    point = gp(0, 0, 600)
    gg.hard_lock_hp_lp_callout_elevations([point], [gg.GradeLabel(0, 0, 'HP_TC', 612.345, 'HP TC 612.345', 'MANUAL')])
    return equal(point.elevation, 612.345, 1e-12)


@case('C08', 'Curb range', 'HP hard holds also work for valid sites below 100 feet.')
def _():
    point = gp(0, 0, 45)
    gg.hard_lock_hp_lp_callout_elevations([point], [gg.GradeLabel(0, 0, 'HP_TC', 50, 'HP TC 50.00', 'MANUAL')])
    return equal(point.elevation, 50, 1e-12)


@case('C09', 'Curb topology', 'Missing elevations cannot be filled from a disconnected curb run.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (10, 0), 'A'), gg.CurbSegment((1000, 0), (1010, 0), 'B')]
    pts = [gp(10, 0, 600, 1, confidence='BC_CONTOUR_EXACT'), gp(1000, 0, None, 2)]
    gg.fill_missing_bc_elevations_by_station_interpolation(pts)
    return equal(pts[1].elevation, None)


@case('C10', 'Curb precedence', 'A supplied LandXML value must not be overwritten by a conflicting temporary contour TIN.')
def _():
    point = gp(50, 50, 650, source='LandXML/TIN surface design', confidence='SURFACE_HIGH')
    segs = [contour(0, 600, 0, 100), contour(100, 610, 0, 100)]
    gg.apply_contour_surface_mode_v851([point], gg.build_contour_tin_surface(segs), segs, surface_mode='bc')
    return equal(point.elevation, 650, 1e-9)


@case('C11', 'Curb precedence', 'Manual tick elevation remains primary after source assignment and contour correction.')
def _():
    labels = [gg.GradeLabel(50, 50, 'TC', 650, 'TC 650.00', 'MANUAL PDF tick anchor')]
    z, src, conf = gg.nearest_label_elevation((50, 50), labels, 0.5, 1)
    point = gp(50, 50, z, source=src, confidence=conf)
    segs = [contour(0, 600, 0, 100), contour(100, 610, 0, 100)]
    gg.apply_contour_surface_mode_v851([point], gg.build_contour_tin_surface(segs), segs, surface_mode='bc')
    return equal(point.elevation, 650, 1e-9)


@case('T01', 'PDF alignment', 'Two-point similarity correctly rotates/scales/translates a third point.')
def _():
    model = gg.similarity_transform_from_two_points((0, 0), (10, 0), (100, 200), (100, 220))
    return equal(gg.transform_pdf_to_cad(model, (5, 3)), (94, 210), 1e-9)


@case('T02', 'PDF alignment', 'Three-point similarity fits an exact non-reflected known transform.')
def _():
    pts = [{'pdf': [x, y], 'cad': [100+2*x-3*y, 200+3*x+2*y]} for x, y in [(0, 0), (10, 0), (0, 10), (10, 10)]]
    model, info = gg.affine_transform_from_points(pts)
    return equal(info['rmse_ft'], 0, 1e-8)


@case('T03', 'PDF alignment', 'Reflected PDF Y fits the exact E=100+2X, N=200-2Y mapping.')
def _():
    pts = [{'pdf': [x, y], 'cad': [100+2*x, 200-2*y]} for x, y in [(0, 0), (10, 0), (0, 10), (10, 10)]]
    model, info = gg.affine_transform_from_points(pts)
    result = equal(info['rmse_ft'], 0, 1e-8)
    result['test_point'] = gg.transform_pdf_to_cad(model, (5, 3))
    return result


@case('T04', 'PDF alignment', 'Coincident controls are rejected as an underdetermined alignment.')
def _():
    model, info = gg.affine_transform_from_points([{'pdf': [1, 1], 'cad': [100, 200]}]*3)
    return equal(model is None or bool(info.get('warning')), True)


@case('O01', 'Offset math', 'Three-foot offset away from FL preserves the BC stake elevation and shifts north.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (10, 0), 'BC')]
    gg.FLOWLINE_SEGMENTS = [gg.CurbSegment((0, -3), (10, -3), 'FL')]
    pts, _pairs = gg.make_offset_points([gp(5, 0, 612.34)], 3)
    return equal((pts[0].easting, pts[0].northing, pts[0].elevation), (5, 3, 612.34), 1e-12)


@case('O02', 'Offset math', 'Reverse offsets switch side without changing elevation.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (10, 0), 'BC')]
    gg.FLOWLINE_SEGMENTS = [gg.CurbSegment((0, -3), (10, -3), 'FL')]
    pts, _pairs = gg.make_offset_points([gp(5, 0, 612.34)], 3, reverse=True)
    return equal((pts[0].easting, pts[0].northing, pts[0].elevation), (5, -3, 612.34), 1e-12)


@case('W01', 'Sidewalk math', 'Ten-foot width at 1 percent adds 0.10 foot when Add is selected.')
def _(): return equal(gg.sidewalk_cross_slope_auto_adjust((0, 0), 600, '', [], 10, 1, 'add', 50)[0], 600.1, 1e-12)


@case('W02', 'Sidewalk math', 'Ten-foot width at 2 percent subtracts 0.20 foot when Subtract is selected.')
def _(): return equal(gg.sidewalk_cross_slope_auto_adjust((0, 0), 600, '', [], 10, 2, 'subtract', 50)[0], 599.8, 1e-12)


@case('W03', 'Sidewalk math', 'Direct mode leaves selected elevation unchanged.')
def _(): return equal(gg.sidewalk_cross_slope_auto_adjust((0, 0), 600, '', [], 10, 2, 'direct', 50)[0], 600, 1e-12)


@case('W04', 'Sidewalk controls', 'Local smoothing must preserve an explicit held design spot instead of replacing it with neighbors.')
def _():
    points = [(0, 0), (-5, 0), (5, 0), (0, 5), (0, -5)]
    elevs = [(602, 'MANUAL held TC design spot', '', 'HIGH')] + [(600, 'LandXML', '', 'SURFACE_HIGH')]*4
    return equal(gg.enforce_sidewalk_local_reasonableness(points, elevs)[0][0], 602, 1e-12)


@case('W05', 'Sidewalk math', 'Auto cross-slope pairing clamps 1 foot difference across 10 feet to 0.10 foot at 1 percent.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((-10, 0), (10, 0), 'SW')]
    values = gg.enforce_sidewalk_cross_slope_pairs([(0, 0), (0, 10)], [(600, '', '', ''), (601, '', '', '')], 10, 1, 'auto')
    return equal(values[1][0]-values[0][0], 0.1, 1e-9)


@case('E01', 'Exports', 'PNEZD output preserves axis order and rounds only the output to 3 decimals.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        path = str(Path(folder)/'points.csv')
        # Export format is tested with an approved control. An unapproved numeric
        # coordinate is now intentionally withheld by the common export gate.
        gg.write_csv([gp(200.56789, 100.12345, 612.34567, 6000, source='Manual control', confidence='PDF_TICK')], path)
        return equal(list(csv.reader(Path(path).open()))[0], ['6000', '100.123', '200.568', '612.346', 'BC'])


@case('E02', 'Exports', 'Missing and zero elevations are omitted from field CSV.')
def _(): return equal(len(gg.stake_ready_points([gp(0, 0, None), gp(0, 0, 0)])), 0)


@case('E03', 'Exports', 'Nonfinite elevations cannot enter field CSV.')
def _(): return equal(len(gg.stake_ready_points([gp(0, 0, float('inf')), gp(0, 0, float('nan'))])), 0)


@case('E04', 'Exports', 'A blocking NOT BRACKETED review cannot be overridden by a historic TIN token.')
def _():
    point = gp(0, 0, 600, confidence='TIN_CONTOUR', issue='NOT BRACKETED. Review required.')
    gg.sanitize_field_elevations([point])
    result = equal(len(gg.stake_ready_points([point])), 0)
    result['qa_status'] = gg.gg_v9_status_for_point(point)[0]
    return result


@case('E05', 'Exports', 'Review-only nearby contour values are withheld by the curb sanitizer.')
def _():
    point = gp(0, 0, 600, confidence='LOW_CONF', source='nearby contour fallback')
    gg.sanitize_field_elevations([point])
    return equal(point.elevation, None)


@case('F01', 'Flow math', '0.02 feet over 10 feet is 0.20 percent and flags the 0.30-percent minimum.')
def _():
    points = [gp(0, 0, 600, 1), gp(10, 0, 600.02, 2)]
    gg.add_flow_warnings(points, 0.003)
    return equal('0.20 percent' in points[0].issue, True)


@case('F02', 'Flow topology', 'Disconnected nearby curb runs are not treated as one drainage segment.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (10, 0), 'A'), gg.CurbSegment((0, 20), (10, 20), 'B')]
    points = [gp(10, 0, 600, 1), gp(0, 20, 600, 2)]
    gg.add_flow_warnings(points, 0.003)
    return equal(points[0].issue, '')


@case('P01', 'Structure parsing', 'Explicit OUT and IN callouts are distinguished.')
def _(): return equal(gg.parse_inverts('INV IN 612.50 INV OUT 611.25'), ([611.25], [611.25, 612.5]))


@case('P02', 'Structure workflow', 'Unknown OUT direction with only an IN invert is withheld from field CSV.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, _handle = cad_fixture(folder)
        st = [{'x': 0, 'y': 0, 'label': 'DI-1', 'source': 'fixture', 'layer': 'STORM'}]
        texts = [{'x': 0, 'y': 0, 'label': 'DI-1', 'pipe': '', 'outs': [], 'inverts': [600], 'raw': 'DI-1 INV IN 600.00'}]
        with patch.object(gg, 'load_dxf_structures_and_text', return_value=(st, texts)):
            result = gg.run_flout_generation(cad, pdf, folder, 6000, 'Both', 'Label', 50)
        return equal(len(list(csv.reader(Path(result[0]).open()))), 0)


@case('P03', 'Structure workflow', 'Structure DI-1 OUT=610 is not replaced by nearby DI-2 OUT=600.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, _handle = cad_fixture(folder)
        st = [{'x': 0, 'y': 0, 'label': 'DI-1', 'source': 'fixture', 'layer': 'STORM'}]
        texts = [{'x': x, 'y': 0, 'label': label, 'pipe': '', 'outs': [z], 'inverts': [z], 'raw': f'{label} INV OUT {z}'} for x, label, z in [(0, 'DI-1', 610), (10, 'DI-2', 600)]]
        with patch.object(gg, 'load_dxf_structures_and_text', return_value=(st, texts)):
            result = gg.run_flout_generation(cad, pdf, folder, 6000, 'Both', 'Label', 50)
        return equal(float(list(csv.reader(Path(result[0]).open()))[0][3]), 610, 1e-9)


@case('R01', 'Sidewalk workflow', 'A full DXF sidewalk run does not export review-only nearby contour fallback as field stakes.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, _handle = cad_fixture(folder, layer='SIDEWALK')
        with patch.object(gg, 'write_water_flow_pdf', return_value=None):
            result = gg.run_edge_sidewalk_generation(cad, pdf, '', folder, 6000, 20, 'SIDEWALK', 'CONTOUR', 50, auto_detect_width=False)
        return equal(len(list(csv.reader(Path(result[0]).open()))), 0)


@case('R02', 'Grade-grid workflow', 'Subgrade subtracts the exact requested 0.5 feet from a known flat 600-foot surface.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, _pdf, _handle = cad_fixture(folder, z=None)
        surface = gg.LandXMLSurface([((-100, -100, 600), (100, -100, 600), (0, 200, 600))])
        with patch.object(gg, 'safe_load_landxml_surface', return_value=surface):
            result = gg.run_grade_grid_generation(cad, '', folder, 6000, 20, 0.5, 'SUBGRADE', 'CONTOUR', 50)
        return equal(sorted({float(r[3]) for r in csv.reader(Path(result[0]).open())}), [599.5])


@case('R03', 'Grade-grid workflow', 'A full grid run does not export review-only fallback contour elevations as field stakes.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, _pdf, _handle = cad_fixture(folder)
        result = gg.run_grade_grid_generation(cad, '', folder, 6000, 20, 0, 'FINISHED GRADE', 'CONTOUR', 50)
        return equal(len(list(csv.reader(Path(result[0]).open()))), 0)


@case('R04', 'Curb workflow', 'Full DXF/PDF curb run respects BC contour crossings and the linear midpoint.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, _handle = cad_fixture(folder, z=None)
        doc = ezdxf.readfile(cad)
        for x, z in [(0, 600), (40, 602)]:
            doc.modelspace().add_line((x, -10, z), (x, 10, z), dxfattribs={'layer': 'P-CONTOUR'})
        doc.saveas(cad)
        with patch.object(gg, 'write_water_flow_pdf', return_value=None):
            result = gg.run_generation(cad, pdf, folder, 6000, 20, 0.5, 'P-BC', 'CONTOUR', 8, 50, 0.003)
        rows = list(csv.reader(Path(result[0]).open()))
        return equal([(float(r[2]), float(r[3])) for r in rows], [(0, 600), (20, 601), (40, 602)])


@case('R05', 'Curb workflow', 'Full curb run preserves an explicitly held manual TC against conflicting contours.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, _handle = cad_fixture(folder, z=None)
        doc = ezdxf.readfile(cad)
        for x, z in [(0, 600), (40, 602)]:
            doc.modelspace().add_line((x, -10, z), (x, 10, z), dxfattribs={'layer': 'P-CONTOUR'})
        doc.saveas(cad)
        anchors = Path(folder)/'manual.json'
        gg.save_manual_anchor_file(str(anchors), {'manual_spots': [{'kind': 'TC', 'elev': 610, 'cad': [20, 0], 'raw': 'TC 610.00'}]}, cad)
        with patch.object(gg, 'write_water_flow_pdf', return_value=None):
            result = gg.run_generation(cad, pdf, folder, 6000, 20, 0.5, 'P-BC', 'CONTOUR', 1, 50, 0.003, manual_anchor_path=str(anchors))
        rows = list(csv.reader(Path(result[0]).open()))
        return equal([float(r[3]) for r in rows if float(r[2]) == 20], [610])


@case('C12', 'Curb controls', 'A contour that crosses both selected BC and FL keeps the exact BC crossing elevation.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (40, 0), 'BC')]
    gg.FLOWLINE_SEGMENTS = [gg.CurbSegment((0, -3), (40, -3), 'FL')]
    gg.add_contour_crossing_bc_points([], [contour(20, 600)])
    return equal(gg.get_contour_point_override((20, 0))[0], 600, 1e-9)


@case('C13', 'Curb controls', 'A calculated possible HP is not promoted from a low-confidence estimate to a field control.')
def _():
    pts = [gp(0, 0, 600, 1, confidence='LOW_CONF'), gp(10, 0, 601, 2, confidence='LOW_CONF'), gp(20, 0, 600, 3, confidence='LOW_CONF')]
    gg.tag_high_low_points(pts)
    gg.sanitize_field_elevations(pts)
    return equal(pts[1].elevation, None)


@case('C14', 'Curb topology', 'Profile rebuilding cannot mix controls on disconnected runs with identical local station ranges.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (40, 0), 'A'), gg.CurbSegment((1000, 0), (1040, 0), 'B')]
    pts = [gp(0, 0, 600, 1, confidence='BC_CONTOUR_EXACT'), gp(40, 0, 602, 2, confidence='BC_CONTOUR_EXACT'), gp(1020, 0, 650, 3, confidence='SURFACE_HIGH')]
    gg.rebuild_bc_profile_from_contours_v850(pts, [])
    return equal(pts[2].elevation, 650, 1e-9)


@case('C15', 'Curb math', 'The dedicated FL-touch solver correctly adds height at an actual FL intersection.')
def _():
    gg.CURB_SEGMENTS = [gg.CurbSegment((0, 0), (40, 0), 'BC')]
    gg.FLOWLINE_SEGMENTS = [gg.CurbSegment((0, -3), (40, -3), 'FL')]
    return equal(gg.flowline_touching_elevation((20, 0), [contour(20, 600, -4, -2)], 50, 0.5)[0], 600.5, 1e-9)


@case('W06', 'Sidewalk math', 'Perpendicular ray intersection measures a ten-foot opposite-edge distance.')
def _(): return equal(gg._ray_segment_intersection_distance((0, 0), (0, 1), (-10, 10), (10, 10)), 10, 1e-9)


@case('W07', 'Sidewalk workflow', 'Opposite-edge width detection finds ten feet from two parallel DXF edges.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, _pdf, handle = cad_fixture(folder, layer='SIDEWALK', z=None)
        doc = ezdxf.readfile(cad)
        doc.modelspace().add_line((0, 10), (40, 10), dxfattribs={'layer': 'SIDEWALK'})
        doc.saveas(cad)
        return equal(gg.detect_sidewalk_width_from_opposite_edge(cad, [handle])[0], 10, 1e-9)


@case('R06', 'Curb workflow', 'Full curb run between FL contour controls uses 601.5, including the 0.5-foot curb height.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, bc_handle = cad_fixture(folder, z=None)
        doc = ezdxf.readfile(cad)
        fl = doc.modelspace().add_line((0, -3), (40, -3), dxfattribs={'layer': 'P-FL'})
        for x, z in [(0, 600), (40, 602)]:
            doc.modelspace().add_line((x, -4, z), (x, -2, z), dxfattribs={'layer': 'P-CONTOUR'})
        doc.saveas(cad)
        bc_path = str(Path(folder)/'bc.json'); fl_path = str(Path(folder)/'fl.json')
        gg.save_selected_curb_handles(bc_path, [bc_handle], cad)
        gg.save_selected_curb_handles(fl_path, [fl.dxf.handle], cad)
        with patch.object(gg, 'write_water_flow_pdf', return_value=None):
            result = gg.run_generation(cad, pdf, folder, 6000, 20, 0.5, 'P-BC', 'CONTOUR', 1, 50, 0.003, selected_curb_path=bc_path, selected_flowline_path=fl_path)
        rows = list(csv.reader(Path(result[0]).open()))
        result = equal([float(r[3]) for r in rows if float(r[2]) == 20], [601.5])
        result['all_x_z'] = [(float(r[2]), float(r[3])) for r in rows]
        return result


@case('R07', 'Curb workflow', 'Full curb run does not add height where contours actually cross BC as well as FL.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, pdf, bc_handle = cad_fixture(folder, z=None)
        doc = ezdxf.readfile(cad)
        fl = doc.modelspace().add_line((0, -3), (40, -3), dxfattribs={'layer': 'P-FL'})
        for x, z in [(0, 600), (40, 602)]:
            doc.modelspace().add_line((x, -10, z), (x, 10, z), dxfattribs={'layer': 'P-CONTOUR'})
        doc.saveas(cad)
        bc_path = str(Path(folder)/'bc.json'); fl_path = str(Path(folder)/'fl.json')
        gg.save_selected_curb_handles(bc_path, [bc_handle], cad)
        gg.save_selected_curb_handles(fl_path, [fl.dxf.handle], cad)
        with patch.object(gg, 'write_water_flow_pdf', return_value=None):
            generated = gg.run_generation(cad, pdf, folder, 6000, 20, 0.5, 'P-BC', 'CONTOUR', 1, 50, 0.003, selected_curb_path=bc_path, selected_flowline_path=fl_path)
        rows = list(csv.reader(Path(generated[0]).open()))
        return equal([(float(r[2]), float(r[3])) for r in rows], [(0, 600), (20, 601), (40, 602)])


@case('P04', 'Structure geometry', 'A closed four-foot square structure is centered at 2,2, without double weighting the closure vertex.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        doc = ezdxf.new()
        doc.modelspace().add_lwpolyline([(0, 0), (4, 0), (4, 4), (0, 4)], close=True, dxfattribs={'layer': 'STORM-STRUCTURE'})
        cad = str(Path(folder)/'structures.dxf'); doc.saveas(cad)
        structures, _texts = gg.load_dxf_structures_and_text(cad, 'Both')
        if len(structures) != 1:
            return equal(len(structures), 1)
        return equal((structures[0]['x'], structures[0]['y']), (2, 2), 1e-9)


@case('S12', 'Surface math', 'Two hundred deterministic interior points match a known plane after grid translation.')
def _():
    rng = random.Random(20261028)
    surface = gg.LandXMLSurface([((600000, 3000000, 600), (600100, 3000000, 610), (600000, 3000100, 620))])
    errors = []
    for _ in range(200):
        x, y = rng.random()*100, rng.random()*100
        if x+y > 100:
            x, y = 100-x, 100-y
        expected = 600+0.1*x+0.2*y
        actual = gg.landxml_elevation_at((600000+x, 3000000+y), surface)[0]
        errors.append(abs(actual-expected))
    return equal(max(errors), 0, 1e-8)


@case('D01', 'Detail math', 'Six inches of curb detail converts to one-half foot.')
def _():
    values = [r['value_ft'] for r in gg.parse_pdf_detail_rules_text('6 INCH CURB')['rules'] if r['kind'] == 'curb_height']
    return equal(values, [0.5])


@case('D02', 'Detail controls', 'Conflicting 4-inch and 6-inch PDF heights are not automatically verified by the detail helper.')
def _():
    scan = {'detail_rules': gg.parse_pdf_detail_rules_text('4 INCH CURB and 6 INCH CURB')['rules']}
    return equal(gg.determine_verified_curb_height_from_pdf(scan)[1], False)


@case('R08', 'Cross-tool state', 'A grade-grid run cannot reuse a contour override from an earlier curb job.')
def _():
    with tempfile.TemporaryDirectory() as folder:
        cad, _pdf, _handle = cad_fixture(folder, z=None)
        gg.set_contour_point_override((-5, -5), 620, 'Previous job BC crossing', 'BC_CONTOUR_EXACT')
        surface = gg.LandXMLSurface([((-100, -100, 600), (100, -100, 600), (0, 200, 600))])
        with patch.object(gg, 'safe_load_landxml_surface', return_value=surface):
            result = gg.run_grade_grid_generation(cad, '', folder, 6000, 20, 0.5, 'SUBGRADE', 'CONTOUR', 50)
        rows = list(csv.reader(Path(result[0]).open()))
        return equal(float(rows[0][3]), 599.5, 1e-9)


if __name__ == '__main__':
    called_functions = set()
    def profile(frame, event, _arg):
        if event == 'call' and frame.f_code.co_filename == gg.__file__ and not frame.f_code.co_name.startswith('<'):
            called_functions.add((frame.f_code.co_firstlineno, frame.f_code.co_name))
    sys.setprofile(profile)
    for code, area, requirement, fn in CASES:
        reset()
        try:
            result = fn()
        except Exception as ex:
            result = {'status': 'ERROR', 'actual': f'{type(ex).__name__}: {ex}', 'traceback': traceback.format_exc()}
        result.update({'id': code, 'area': area, 'requirement': requirement})
        RESULTS.append(result)
        print(f"{code} {result['status']}: {requirement}")
    sys.setprofile(None)
    counts = {status: sum(r['status'] == status for r in RESULTS) for status in ('PASS', 'FAIL', 'ERROR')}
    data = {'app_version': gg.APP_VERSION, 'source': str(Path(gg.__file__).resolve()),
            'source_sha256': hashlib.sha256(Path(gg.__file__).read_bytes()).hexdigest(),
            'python': sys.version, 'dependencies': {p: importlib.metadata.version(p) for p in ('ezdxf', 'scipy', 'numpy', 'PyMuPDF', 'reportlab')},
            'scope': 'Synthetic software audit; no field or real-project validation. Production calculation code unchanged.',
            'counts': counts, 'checks': RESULTS,
            'exercised_functions': [{'line': line, 'name': name} for line, name in sorted(called_functions)]}
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(counts))
    raise SystemExit(1 if counts['FAIL'] or counts['ERROR'] else 0)
