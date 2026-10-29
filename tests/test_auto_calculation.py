"""Known-answer automatic packages and sheet calculations, not field validation."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import ezdxf
import fitz
import gradeguard_auto as auto
import gradeguard_pdf as pdf
import schoel_gradeguard as gg


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.plan=self.root/'plan.pdf'
        with fitz.open() as doc:
            page=doc.new_page(width=500,height=400)
            page.insert_text((100,60),'TC 600.00')
            page.draw_polyline([(100,100),(140,100),(140,80)],color=(0,0,0))
            doc.save(self.plan)
        self.job=dict(schema='gradeguard.pdf-calculation.v1',pdf_path=str(self.plan),pdf_sha256=pdf.fingerprint(self.plan),page=1,
            calibration=dict(mode='local',points=[dict(pdf=[100,100]),dict(pdf=[140,100])],length=40,units='US survey feet',reference='Synthetic known-distance feet / arbitrary vertical datum'),
            spacing=10,grid_spacing=10,association_tolerance=.35,start_point=6000,subgrade_depth=0,calculate_grid=False,
            paths=[dict(id='C1',kind='CURB',points=[[100,100],[140,100],[140,80]])],controls=[],boundary=[])

    def tearDown(self):
        gg.reset_generation_state(); self.tmp.cleanup()

    def control(self,ident,xy,z,kind='CURB_TOP',path='C1',**extra):
        return dict(id=ident,pdf=xy,z=z,kind=kind,path_id=path,role='Spot',**extra)

    def run_pdf(self,job=None): return auto.calculate_pdf_job(job or self.job,str(self.root/'results'))

    def test_local_calibration_reverses_page_y_and_uses_path_length(self):
        model=auto.calibrate(self.job['calibration'])
        self.assertEqual(auto.transform(model,[140,80]),(40,20))
        self.job['controls']=[self.control('A',[100,100],600),self.control('B',[140,80],606)]
        result=self.run_pdf(); at_corner=next(r for r in result['rows'] if r['station']==40)
        self.assertAlmostEqual(at_corner['elevation'],604)
        self.assertEqual(len(result['rows']),7)
        self.assertIn('LOCAL_PLAN_XY',result['manifest']['csv'])
        self.assertFalse(list(Path(result['folder']).glob('*Approved*')))

    def test_survey_fit_uses_independent_check(self):
        spec=dict(mode='survey',units='US survey feet',reference='Synthetic control',tolerance=.01,
            points=[dict(pdf=[100,100],xy=[1000,2000]),dict(pdf=[140,100],xy=[1000,2040]),dict(pdf=[140,80],xy=[980,2040])])
        model=auto.calibrate(spec)
        self.assertAlmostEqual(model['check_residual'],0)
        self.assertEqual(auto.transform(model,[120,90]),(990,2020))
        spec['points'][2]['xy'][0]=981
        with self.assertRaisesRegex(ValueError,'residual'): auto.calibrate(spec)

    def test_calibration_rejects_collinear_check_unknown_units_missing_reference(self):
        for field,value in [('units','feet'),('reference',''),('length',0),('length',float('nan'))]:
            spec=copy.deepcopy(self.job['calibration']); spec[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError): auto.calibrate(spec)
        spec=dict(mode='survey',units='US survey feet',reference='Test',tolerance=.1,
            points=[dict(pdf=[0,0],xy=[0,0]),dict(pdf=[10,0],xy=[10,0]),dict(pdf=[5,0],xy=[5,0])])
        with self.assertRaisesRegex(ValueError,'baseline'): auto.calibrate(spec)

    def test_lower_curb_rise_applied_once_and_top_unchanged(self):
        self.job['controls']=[self.control('A',[100,100],599.5,'CURB_FL',rise=.5),self.control('B',[140,80],606)]
        rows=self.run_pdf()['rows']
        self.assertEqual(rows[0]['elevation'],600); self.assertEqual(rows[-1]['elevation'],606)
        self.assertAlmostEqual(rows[3]['elevation'],603)

    def test_hp_lp_hold_and_break_interpolation(self):
        self.job['controls']=[self.control('A',[100,100],600),self.control('HP',[120,100],605),self.control('LP',[140,80],599)]
        self.job['controls'][1]['role']='HP'; self.job['controls'][2]['role']='LP'
        rows=self.run_pdf()['rows']
        self.assertEqual([r['elevation'] for r in rows],[600,602.5,605,603.5,602,600.5,599])
        self.assertIn('HP',rows[2]['source'])

    def test_missing_end_controls_leave_blank_not_extrapolated(self):
        self.job['controls']=[self.control('A',[110,100],601),self.control('B',[140,90],605)]
        result=self.run_pdf(); rows=result['rows']
        self.assertIsNone(rows[0]['elevation']); self.assertIsNone(rows[-1]['elevation'])
        with open(result['manifest']['csv'],encoding='utf-8-sig',newline='') as f: csv_rows=list(csv.DictReader(f))
        self.assertEqual(csv_rows[0]['elevation'],''); self.assertEqual(rows[1]['elevation'],601)

    def test_separate_paths_never_borrow_anchors(self):
        self.job['paths'].append(dict(id='SW',kind='SIDEWALK',points=[[100,99],[140,99]]))
        self.job['controls']=[self.control('A',[100,100],600),self.control('B',[140,80],606)]
        result=self.run_pdf(); sw=[r for r in result['rows'] if r['path']=='SW']
        self.assertTrue(sw); self.assertTrue(all(r['elevation'] is None for r in sw))

    def test_sidewalk_held_elevations_receive_no_curb_rise(self):
        self.job['paths'][0]['kind']='SIDEWALK'
        self.job['controls']=[self.control('A',[100,100],600,'SIDEWALK',rise=99),self.control('B',[140,80],606,'SIDEWALK',rise=99)]
        self.assertEqual(self.run_pdf()['rows'][3]['elevation'],603)

    def test_conflicting_or_distant_control_fails_path_without_partial_points(self):
        for controls in [[self.control('A',[100,100],600),self.control('B',[100,100],601)],
                         [self.control('A',[100,98],600)]]:
            self.job['controls']=controls; result=self.run_pdf()
            self.assertEqual(result['rows'],[]); self.assertEqual(result['manifest']['tools'][0]['status'],'Failed')

    def test_closed_profile_wraps_between_two_distinct_controls(self):
        self.job['paths'][0]['points']=[[100,100],[140,100],[140,60],[100,60],[100,100]]
        self.job['controls']=[self.control('A',[100,100],600),self.control('B',[140,60],608)]
        rows=self.run_pdf()['rows']
        self.assertEqual(len(rows),16)
        self.assertEqual(next(r['elevation'] for r in rows if r['station']==120),604)

    def test_duplicate_closed_controls_do_not_create_full_flat_profile(self):
        self.job['paths'][0]['points']=[[100,100],[140,100],[140,60],[100,60],[100,100]]
        self.job['controls']=[self.control('A',[100,100],600),self.control('B',[100,100],600)]
        rows=self.run_pdf()['rows']
        self.assertEqual(sum(r['elevation'] is not None for r in rows),1)

    def test_signed_path_slope_uses_station_distance_and_stops_at_gap(self):
        self.job['controls']=[self.control('A',[100,100],600),self.control('B',[140,90],605)]
        rows=self.run_pdf()['rows']
        self.assertAlmostEqual(rows[3]['slope_to_next_percent'],10)
        self.assertAlmostEqual(rows[4]['slope_to_next_percent'],10)
        self.assertEqual(rows[5]['slope_to_next_percent'],'')

    def test_grid_known_plane_subtracts_once_and_never_extrapolates(self):
        self.job.update(paths=[],calculate_grid=True,subgrade_depth=.75,boundary=[[100,100],[140,100],[140,60],[100,60]])
        self.job['controls']=[self.control('F1',[100,100],600,'FG',''),self.control('F2',[140,100],604,'FG',''),self.control('F3',[100,60],608,'FG','')]
        result=self.run_pdf(); rows=result['rows']
        center=next(r for r in rows if r['x']==10 and r['y']==10)
        self.assertAlmostEqual(center['elevation'],602.25)
        self.assertEqual(center['status'],'TIN_REVIEW')
        outside=next(r for r in rows if r['x']==40 and r['y']==40)
        self.assertIsNone(outside['elevation'])
        self.assertEqual(outside['status'],'OUTSIDE_CONTROL_SURFACE')
        near_outside=next(r for r in rows if r['x']==30 and r['y']==20)
        self.assertIsNone(near_outside['elevation'])

    def test_grid_rejects_crossing_boundary_collinear_and_duplicate_conflicts(self):
        for points in [[(0,0),(4,4),(0,4),(4,0)],[(0,0),(0,0),(4,4)]]:
            with self.assertRaises(ValueError): auto.polygon_valid(points)
        boundary=[(0,0),(40,0),(40,40),(0,40)]
        controls=[dict(kind='FG',xy=p,z=600) for p in [(0,0),(10,0),(20,0)]]
        with self.assertRaises(ValueError): auto.pdf_surface(controls,boundary)
        controls.append(dict(kind='FG',xy=(0,0),z=610))
        with self.assertRaisesRegex(ValueError,'Conflicting'): auto.pdf_surface(controls,boundary)

    def test_concave_grid_boundary_does_not_bridge_notch(self):
        polygon=[(0,0),(30,0),(30,30),(20,30),(20,10),(10,10),(10,30),(0,30)]
        surface=auto.pdf_surface([dict(kind='FG',xy=p,z=600) for p in polygon],polygon)
        self.assertIsNone(gg.landxml_elevation_at((15,20),surface,search_pad=0)[0])
        self.assertEqual(gg.landxml_elevation_at((5,20),surface)[0],600)

    def test_changed_pdf_and_invalid_numeric_settings_rejected(self):
        for key,value in [('spacing',0),('grid_spacing',float('nan')),('subgrade_depth',-1),('start_point',3.2)]:
            job=copy.deepcopy(self.job); job[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError): self.run_pdf(job)
        self.job['pdf_sha256']='stale'
        with self.assertRaisesRegex(ValueError,'changed'): self.run_pdf()

    def test_rotated_pdf_vectors_keep_display_coordinates_and_curve(self):
        from gradeguard_auto_ui import sheet_paths
        with fitz.open(self.plan) as doc:
            page=doc[0]; page.set_rotation(90)
            paths=sheet_paths(page)
            self.assertTrue(any(p[0]==(300,100) and p[-1]==(320,140) for p in paths))
            page.draw_bezier((50,50),(50,100),(100,100),(100,50))
            curve=max(sheet_paths(page),key=len)
            self.assertGreater(len(curve),10)
            self.assertGreater(auto.path_stations(curve)[-1],50)

    def cad_job(self):
        doc=ezdxf.new(); doc.units=21
        doc.modelspace().add_line((10,10),(40,10),dxfattribs={'layer':'P-BC'})
        doc.modelspace().add_line((10,15),(40,15),dxfattribs={'layer':'SIDEWALK'})
        cad=self.root/'drawing.dxf'; doc.saveas(cad)
        xml=self.root/'surface.xml'
        xml.write_text('<LandXML><Units><Imperial linearUnit="USSurveyFoot" elevationUnit="feet"/></Units><Surfaces><Surface name="FG"><Definition><Pnts><P id="1">0 0 600</P><P id="2">0 100 610</P><P id="3">100 0 620</P></Pnts><Faces><F>1 2 3</F></Faces></Definition></Surface></Surfaces></LandXML>')
        return dict(cad_path=str(cad),pdf_path=str(self.plan),surface_path=str(xml),out_dir=str(self.root/'cad-results'),start_point=7000,
            spacing=10,sidewalk_spacing=10,grade_grid_spacing=10,curb_height=.5,surface_interpretation='bc',create_offset=True,offset_mode='left',sidewalk_auto_width=False,grade_grid_type='SUBGRADE',grade_grid_subtract=.75)

    def test_real_cad_batch_combines_all_tools_reserves_offsets_and_keeps_sources(self):
        job=self.cad_job(); original={p:pdf.fingerprint(p) for p in [job['cad_path'],job['pdf_path'],job['surface_path']]}
        with patch.object(gg,'write_water_flow_pdf',return_value=None): result=auto.run_cad_package(job)
        self.assertEqual(len(result['manifest']['tools']),3)
        self.assertFalse([t for t in result['manifest']['tools'] if t['status']=='Failed'],result['manifest'])
        with (Path(result['folder'])/'Combined_Approved_Field_Points.csv').open(newline='') as stream: rows=list(csv.reader(stream))
        ids=[int(r[0]) for r in rows]; self.assertEqual(len(ids),len(set(ids)))
        self.assertEqual(ids[0],7000); self.assertGreater(len(rows),8)
        for point in rows:
            if point[4]=='SUBGRADE': self.assertAlmostEqual(float(point[3]),600+.1*float(point[2])+.2*float(point[1])-.75,places=3)
        self.assertTrue(any(r[4]=='SUBGRADE' for r in rows))
        self.assertEqual(original,{p:pdf.fingerprint(p) for p in original})

    def test_batch_partial_failure_retains_other_results_and_reports_failure(self):
        job=self.cad_job(); job['create_offset']=False
        with patch.object(gg,'run_generation',side_effect=ValueError('Synthetic curb failure')),patch.object(gg,'write_water_flow_pdf',return_value=None): result=auto.run_cad_package(job)
        self.assertEqual(result['manifest']['tools'][0]['status'],'Failed')
        self.assertGreater(result['manifest']['approved_total'],0)
        self.assertIn('Synthetic curb failure',Path(result['report']).read_text())

    def test_batch_rejects_unknown_units_and_missing_pdf(self):
        job=self.cad_job(); job['pdf_path']='missing.pdf'
        with self.assertRaises(ValueError): auto.run_cad_package(job)
        job=self.cad_job(); doc=ezdxf.readfile(job['cad_path']); doc.units=6; doc.saveas(job['cad_path'])
        with self.assertRaises(ValueError): auto.run_cad_package(job)


class PdfWorkspaceTests(unittest.TestCase):
    def setUp(self):
        AutoTests.setUp(self)
        from gradeguard_auto_ui import PdfCalculationWindow
        self.app=gg.GradeGuardApp(); self.app.withdraw()
        self.window=PdfCalculationWindow(self.app); self.window.withdraw()
        self.window.vars['pdf'].set(str(self.plan)); self.window.vars['page'].set('1')
        self.window.vars['output'].set(str(self.root/'ui-results'))
        self.assertTrue(self.window.load_sheet())

    def tearDown(self):
        self.window.close(); self.app.destroy(); AutoTests.tearDown(self)

    def wait_task(self):
        end=time.monotonic()+15
        while self.window.busy and time.monotonic()<end:
            self.app.update(); time.sleep(.03)
        self.assertFalse(self.window.busy,'Worker did not finish')

    def test_real_text_scan_progress_and_grade_selection(self):
        self.window.scan(); self.wait_task()
        self.assertEqual(len(self.window.observations),1)
        self.window.candidates.selection_set('0'); self.window.select_observation()
        self.assertEqual(self.window.value('z'),'600.0')
        self.assertEqual(self.window.meaning.get(),'')
        self.assertFalse(self.app._generation_running)

    def test_calculate_from_loaded_sheet_and_restore_saved_setup(self):
        w=self.window; w.paths=self.job['paths']; w.cal_points=[[100,100],[140,100]]
        w.controls=[dict(id='A',pdf=[100,100],z=600,kind='CURB_TOP',path_id='C1'),dict(id='B',pdf=[140,80],z=606,kind='CURB_TOP',path_id='C1')]
        w.vars['length'].set('40'); w.vars['reference'].set('Synthetic control'); w.units.set(True); w.vars['spacing'].set('10')
        saved=self.root/'setup.json'
        with patch('gradeguard_auto_ui.filedialog.asksaveasfilename',return_value=str(saved)): w.save_setup()
        self.assertTrue(saved.exists())
        w.paths=[]; w.controls=[]
        with patch('gradeguard_auto_ui.filedialog.askopenfilename',return_value=str(saved)): w.load_setup()
        self.assertEqual(len(w.controls),2); self.assertEqual(len(w.paths),1)
        w.calculate(); self.wait_task()
        self.assertIsNotNone(w.result)
        self.assertEqual(w.result['rows'][4]['elevation'],604)
        self.assertIn('Calculated for review',w.result_text.get('1.0','end'))


if __name__=='__main__': unittest.main()
