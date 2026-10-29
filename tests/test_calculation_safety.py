"""Additional boundary cases for 9.3.1; independent synthetic drawings only."""
import csv
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import ezdxf
import schoel_gradeguard as gg


class CalculationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        gg.reset_generation_state()

    def tearDown(self):
        gg.reset_generation_state()
        self.tmp.cleanup()

    def cad(self, name='drawing.dxf', units=21):
        doc = ezdxf.new('R2010'); doc.units = units
        doc.modelspace().add_line((0, 0), (40, 0), dxfattribs={'layer':'P-BC'})
        path = self.root / name; doc.saveas(path)
        return str(path)

    def xml(self, extra='', units='USSurveyFoot', excluded=''):
        surface = '<Surface name="FG"><Definition><Pnts><P id="1">0 0 600</P><P id="2">0 100 610</P><P id="3">100 0 620</P></Pnts><Faces><F '+excluded+'>1 2 3</F></Faces></Definition></Surface>'
        path = self.root / 'surface.xml'
        path.write_text(f'<LandXML><Units><Imperial linearUnit="{units}" elevationUnit="feet"/></Units><Surfaces>{surface}{extra}</Surfaces></LandXML>')
        return str(path)

    def point(self, **kw):
        values=dict(point=6000,northing=100,easting=200,elevation=612.34,source='Manual control',confidence='PDF_TICK')
        values.update(kw)
        return gg.GenPoint(**values)

    def test_finite_coordinates_required_even_for_held_grade(self):
        for field in ('northing','easting','elevation'):
            for value in (float('nan'),float('inf'),float('-inf')):
                with self.subTest(field=field,value=value):
                    self.assertFalse(gg.field_approval(self.point(**{field:value}))[0])

    def test_rejected_values_survive_repeated_sanitization_only_as_review_data(self):
        point=self.point(confidence='SURFACE_REVIEW')
        for _ in range(2): gg.sanitize_field_elevations([point])
        self.assertIsNone(point.elevation)
        self.assertEqual(point.calculated_elevation,612.34)
        self.assertEqual(gg.gg_v9_status_for_point(point)[0],'RED')
        self.assertEqual(gg.stake_ready_points([point]),[])

    def test_conflicting_or_unknown_source_cannot_be_promoted_by_old_confidence(self):
        for text in ('Conflicting controls','Unknown surface','Review required','Not bracketed'):
            self.assertFalse(gg.field_approval(self.point(issue=text))[0])

    def test_offset_requires_parent_approval(self):
        self.assertFalse(gg.field_approval(self.point(confidence='OFFSET',approved=False))[0])
        self.assertFalse(gg.field_approval(self.point(confidence='OFFSET',approved=None))[0])
        self.assertTrue(gg.field_approval(self.point(confidence='OFFSET',approved=True))[0])

    def test_raw_field_csv_has_single_line_description_with_quoted_comma(self):
        output=self.root/'field.csv'
        gg.write_csv([self.point(desc='BC, PC\nheld')],str(output))
        self.assertEqual(len(output.read_text().splitlines()),1)
        with output.open() as f: self.assertEqual(next(csv.reader(f))[-1],'BC, PC held')

    def test_unknown_numeric_grade_is_not_a_field_point(self):
        output=self.root/'field.csv'
        self.assertEqual(gg.write_csv([self.point(source='',confidence='')],str(output)),0)

    def test_units_require_known_us_survey_feet_or_explicit_unitless_confirmation(self):
        for units in (1,2,6):
            with self.assertRaises(ValueError): gg.validate_generation_inputs(self.cad(units=units),units_confirmed=True)
        path=self.cad(units=0)
        with self.assertRaises(ValueError): gg.validate_generation_inputs(path)
        gg.validate_generation_inputs(path,units_confirmed=True)
        gg.validate_generation_inputs(self.cad(units=21))

    def test_invalid_spacing_is_rejected_before_reading_input(self):
        for value in (0,-1,float('nan'),float('inf')):
            with self.assertRaises(ValueError): gg.validate_generation_inputs('missing.dxf',value)

    def test_negative_or_nonfinite_adjustments_rejected(self):
        for value in (-.1,float('nan'),float('inf')):
            with self.assertRaises(ValueError): gg.validate_numeric_settings(cross_slope=value)

    def test_saved_selection_is_bound_to_path_and_drawing_contents(self):
        cad=self.cad(); other=self.cad('other.dxf'); saved=str(self.root/'selection.json')
        gg.save_selected_curb_handles(saved,['2A'],cad)
        self.assertEqual(gg.load_selected_curb_handles(saved,cad),['2A'])
        with self.assertRaises(ValueError): gg.load_selected_curb_handles(saved,other)
        Path(cad).write_text(Path(cad).read_text()+'\n')
        with self.assertRaises(ValueError): gg.load_selected_curb_handles(saved,cad)

    def test_old_unbound_selection_is_not_silently_applied(self):
        saved=self.root/'selection.json'; saved.write_text('{"handles":["2A"]}')
        with self.assertRaises(ValueError): gg.load_selected_curb_handles(str(saved),self.cad())

    def test_manual_structure_controls_are_bound_to_the_drawing(self):
        cad=self.cad(); saved=str(self.root/'manual.json'); pts=[dict(x=1,y=2,elev=605)]
        gg.save_manual_flout_points(saved,pts,cad)
        self.assertEqual(gg.load_manual_flout_points(saved,cad),pts)
        with self.assertRaises(ValueError): gg.load_manual_flout_points(saved,self.cad('other.dxf'))

    def test_manual_tick_controls_are_bound_to_the_drawing(self):
        cad=self.cad(); saved=str(self.root/'manual.json')
        gg.save_manual_anchor_file(saved,{'manual_spots':[dict(cad=[20,0],kind='TC',elev=610)]},cad)
        self.assertEqual(gg.load_manual_anchor_file(saved,cad)[0][0].elev,610)
        with self.assertRaises(ValueError): gg.load_manual_anchor_file(saved,self.cad('other.dxf'))

    def test_rerun_preserves_existing_outputs(self):
        old=self.root/'Generated_BC_Points.csv'; old.write_text('ORIGINAL')
        folder=Path(gg.prepare_output_directory(str(self.root)))
        self.assertEqual(folder.parent,self.root)
        self.assertEqual(old.read_text(),'ORIGINAL')
        self.assertNotEqual(gg.prepare_output_directory(str(self.root)),str(folder))

    def test_landxml_unit_mismatch_is_rejected(self):
        for unit in ('foot','meter','internationalFoot'):
            with self.assertRaises(ValueError): gg.load_landxml_surface(self.xml(units=unit))

    def test_landxml_missing_selected_surface_is_rejected(self):
        with self.assertRaises(ValueError): gg.load_landxml_surface(self.xml(),'EG')

    def test_landxml_excluded_face_cannot_supply_grade(self):
        with self.assertRaises(ValueError): gg.load_landxml_surface(self.xml(excluded='i="1"'))

    def test_landxml_surface_metadata_and_axis_order(self):
        surface=gg.load_landxml_surface(self.xml(),'FG')
        self.assertEqual((surface.name,surface.linear_unit),('FG','USSurveyFoot'))
        self.assertEqual(surface.triangles[0][1],(100.,0.,610.))

    def test_bad_explicit_xml_never_silently_falls_back(self):
        bad=self.root/'bad.xml';bad.write_text('<broken>')
        with self.assertRaises(ValueError): gg.safe_load_landxml_surface(str(bad))

    def test_negative_extrusion_polylines_have_wcs_staking_and_pc_pt(self):
        msp=ezdxf.new().modelspace()
        entities=[msp.add_lwpolyline([(10,0,1),(20,0,0)],format='xyb',dxfattribs={'extrusion':(0,0,-1)}),
                  msp.add_polyline2d([(10,0),(20,0)],dxfattribs={'extrusion':(0,0,-1)})]
        entities[1].vertices[0].dxf.bulge=1
        for ent in entities:
            with self.subTest(kind=ent.dxftype()):
                points,controls=gg.staking_points_from_entity(ent,25)
                self.assertEqual(points,[(-10.,0.),(-15.,-5.),(-20.,0.)])
                self.assertEqual(gg.polyline_points(ent)[0],(-10.,0.))
                self.assertEqual(gg.pc_pt_points_from_entity(ent),([(-10.,0.)],[(-20.,0.)]))

    def test_3d_polyline_vertices_already_use_wcs(self):
        poly=ezdxf.new().modelspace().add_polyline3d([(1,2,600),(11,2,601)])
        self.assertEqual(gg.staking_points_from_entity(poly,25)[0],[(1.,2.),(11.,2.)])

    def test_bulge_stake_midpoints_match_circle_for_minor_major_and_both_signs(self):
        for bulge in (.414213562373095, -.414213562373095, 2., -2.):
            ent=ezdxf.new().modelspace().add_lwpolyline([(0,0,bulge),(10,0,0)],format='xyb')
            center,_,_,radius=ezdxf.math.bulge_to_arc((0,0),(10,0),bulge)
            mid=gg.staking_points_from_entity(ent,25)[0][1]
            self.assertAlmostEqual(math.hypot(mid[0]-center.x,mid[1]-center.y),radius,places=10)
            self.assertEqual(mid,(5.,-5.*bulge))

    def test_transformed_block_staking_matches_virtual_geometry(self):
        doc=ezdxf.new(); block=doc.blocks.new('TEST');block.add_line((0,0),(10,0))
        insert=doc.modelspace().add_blockref('TEST',(100,200),dxfattribs={'rotation':90,'xscale':2,'yscale':2})
        pts,_=gg.staking_points_from_entity(insert,25)
        self.assertAlmostEqual(pts[0][0],100);self.assertAlmostEqual(pts[0][1],200)
        self.assertAlmostEqual(pts[-1][0],100);self.assertAlmostEqual(pts[-1][1],220)

    def test_missing_intermediate_point_is_not_bridged_by_conflict_report(self):
        points=[self.point(point=1,easting=0,elevation=600),self.point(point=2,easting=10,elevation=None),self.point(point=3,easting=20,elevation=610)]
        result=gg.write_surface_conflict_report_csv(points,str(self.root/'conflicts.csv'))
        self.assertEqual(result['surface_conflict_rows'],0)

    def test_short_gap_contours_cannot_supply_approved_station_interpolation(self):
        gg.CURB_SEGMENTS=[gg.CurbSegment((0,0),(40,0),'BC')]
        contours=[gg.ElevSegment((x,1),(x,10),z,'P-CONTOUR','Contour') for x,z in [(0,600),(40,602)]]
        result=gg.assign_elevations([(20,0)],[],contours,.5,1,50,False,source_policy='contours')[0]
        self.assertIsNone(result[0])

    def test_conflicting_station_controls_cannot_be_interpolated(self):
        gg.CURB_SEGMENTS=[gg.CurbSegment((0,0),(40,0),'BC')]
        contours=[gg.ElevSegment((x,-10),(x,10),z,'P-CONTOUR','Contour') for x,z in [(0,600),(0,610),(40,602)]]
        result=gg.assign_elevations([(20,0)],[],contours,.5,1,50,False,source_policy='contours')[0]
        self.assertIsNone(result[0]);self.assertIn('CONFLICT',result[3])

    def test_branched_chain_is_not_treated_as_one_station_profile(self):
        gg.CURB_SEGMENTS=[gg.CurbSegment((0,0),(20,0),'BC'),gg.CurbSegment((20,0),(40,0),'BC'),gg.CurbSegment((20,0),(20,20),'BC')]
        self.assertEqual(gg.safe_local_curb_chain((10,0)),[])

    def test_dxf_surface_priority_uses_explicit_surface_edge(self):
        gg.CURB_SEGMENTS=[gg.CurbSegment((0,0),(40,0),'BC')]
        contours=[gg.ElevSegment((x,-10),(x,10),z,'P-CONTOUR','Contour') for x,z in [(0,600),(40,602)]]
        contours.append(gg.ElevSegment((0,0),(40,0),650,'Topo-P-Surface','DXF surface'))
        result=gg.assign_elevations([(20,0)],[],contours,.5,1,50,False,source_policy='dxf_surface')[0]
        self.assertEqual(result[0],650)

    def test_flow_pairs_follow_station_order_and_include_missing_grade_gaps(self):
        gg.CURB_SEGMENTS=[gg.CurbSegment((0,0),(40,0),'BC')]
        points=[self.point(point=i,easting=x,northing=0) for i,x in [(3,40),(1,0),(2,20)]]
        self.assertEqual([(a.point,b.point,d) for a,b,d in gg.diagnostic_pairs(points)],[(1,2,20),(2,3,20)])
        points[-1].elevation=None
        self.assertEqual(list(gg.diagnostic_pairs(points)),[])


if __name__=='__main__': unittest.main()
