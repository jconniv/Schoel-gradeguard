"""Independent inspection answers, source integrity and real linked Tk actions."""
import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_auto_calculation as fixtures
import gradeguard_review as review
import schoel_gradeguard as gg


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AutoTests(); self.fixture.setUp()

    def tearDown(self): self.fixture.tearDown()

    def run_model(self):
        result=self.fixture.run_pdf()
        return result,review.load_review(result['manifest']['review_file'])

    def test_pdf_trace_retains_printed_grade_and_adds_local_rise_once(self):
        f=self.fixture
        f.job['controls']=[f.control('A',[100,100],599.5,'CURB_FL',rise=.5),f.control('B',[140,80],606)]
        result,model=self.run_model(); p=model['points'][3]
        self.assertEqual(p['z'],603); self.assertFalse(p['approved'])
        self.assertEqual(p['pdf'],[130,100]); self.assertEqual(p['station'],30)
        self.assertIn('603.000000',p['detail']['formula'])
        a=p['detail']['controls'][0]
        self.assertEqual(a['printed_elevation'],599.5); self.assertEqual(a['top_elevation'],600)
        self.assertIn('599.500000 + 0.500000 = 600.000000',a['conversion'])
        self.assertFalse(list(Path(result['folder']).glob('*Approved*')))

    def test_hold_high_point_and_signed_downhill_segment(self):
        f=self.fixture
        f.job['controls']=[f.control('A',[100,100],600),f.control('HP',[120,100],605),f.control('B',[140,80],599)]
        f.job['controls'][1]['role']='HP'
        _,m=self.run_model(); hp=m['points'][2]
        self.assertEqual(hp['z'],605); self.assertEqual(hp['detail']['method'],'Held design control')
        self.assertEqual(hp['detail']['controls'][0]['role'],'HP')
        edge=next(e for e in review.profile_edges(m['points']) if e['a']==hp['id'])
        self.assertEqual(edge['slope'],-15); self.assertEqual(edge['flow'],m['points'][3]['id'])

    def test_missing_grades_and_duplicate_stations_never_draw_a_bridge(self):
        points=[dict(id=str(i),path='A',station=i*10,z=z) for i,z in enumerate([600,601,None,603,604])]
        edges=review.profile_edges(points)
        self.assertEqual([(e['a'],e['b']) for e in edges],[('0','1'),('3','4')])
        self.assertEqual(edges[0]['slope'],10); self.assertEqual(edges[0]['flow'],'0')
        points[1]['station']=0
        self.assertEqual(len(review.profile_edges(points)),1)
        points[4]['z']=float('nan')
        self.assertEqual(review.profile_edges(points),[])

    def test_closed_profile_wrap_and_flat_direction(self):
        points=[dict(id='A',path='loop',station=0,z=600),dict(id='B',path='loop',station=30,z=603)]
        edges=review.profile_edges(points,40)
        self.assertEqual([e['slope'] for e in edges],[10,-30]); self.assertEqual(edges[-1]['length'],10)
        points[1]['z']=600
        self.assertTrue(all(e['flow']=='flat' for e in review.profile_edges(points,40)))

    def test_connected_geometry_splits_disconnected_runs_and_branch_junctions(self):
        segments=[gg.CurbSegment(a,b,'BC') for a,b in [((0,0),(10,0)),((10,0),(20,0)),((50,0),(60,0))]]
        paths=review.connected_paths(segments)
        self.assertEqual(sorted(len(p['points']) for p in paths),[2,3])
        segments.append(gg.CurbSegment((10,0),(10,10),'BC'))
        paths=review.connected_paths(segments); self.assertEqual(len(paths),4)
        point=dict(x=10,y=0,path='',station=None,issue='')
        review.assign_paths([point],paths)
        self.assertEqual(point['path'],''); self.assertIn('ambiguous',point['issue'])

    def test_connected_loop_and_projection_station(self):
        segments=[gg.CurbSegment(a,b,'BC') for a,b in [((0,0),(10,0)),((10,0),(10,10)),((10,10),(0,0))]]
        paths=review.connected_paths(segments); self.assertEqual(len(paths),1); self.assertTrue(paths[0]['closed'])
        point=dict(x=10,y=5,path='',station=None,issue='')
        review.assign_paths([point],paths); self.assertEqual(point['station'],15)

    def test_surface_formula_uses_triangle_weights_and_single_depth(self):
        face=((0,0,600),(100,0,610),(0,100,620))
        detail=review.surface_evidence((30,20),gg.LandXMLSurface([face]),607,.75)
        self.assertAlmostEqual(sum(detail['weights']),1)
        self.assertEqual(detail['triangle'],[list(p) for p in face])
        self.assertIn('607.000000 - 0.750000 = 606.250000',detail['formula'])
        self.assertEqual(review.surface_evidence((110,110),gg.LandXMLSurface([face]),600)['method'],'Recorded engine result')

    def test_evidence_cannot_disagree_with_engine(self):
        with self.assertRaisesRegex(ValueError,'does not match'):
            review.profile_evidence([(0,600,'A'),(10,601,'B')],5,999,[])

    def test_cad_package_review_matches_export_gate_and_keeps_offsets_separate(self):
        import gradeguard_auto
        f=self.fixture; job=f.cad_job()
        with patch.object(gg,'write_water_flow_pdf',return_value=None): result=gradeguard_auto.run_cad_package(job)
        m=review.load_review(result['manifest']['review_file'])
        self.assertEqual(m['workflow'],'CAD package')
        self.assertEqual(len({p['id'] for p in m['points']}),len(m['points']))
        offsets=[p for p in m['points'] if p['tool']=='CURB OFFSET']; self.assertTrue(offsets)
        self.assertTrue(all(p['path']=='' and p['station'] is None for p in offsets))
        sub=[p for p in m['points'] if p['tool']=='SUBGRADE']; self.assertTrue(sub)
        for p in sub:
            if p['z'] is not None: self.assertAlmostEqual(p['z'],600+.1*p['x']+.2*p['y']-.75)
        self.assertEqual(sum(p['approved'] for p in m['points']),result['manifest']['approved_total'])

    def test_invalid_review_schema_and_nonfinite_grades_are_rejected(self):
        _,m=self.run_model(); file=self.fixture.root/'invalid.json'
        m['points'][0]['z']=float('nan'); file.write_text(json.dumps(m))
        with self.assertRaisesRegex(ValueError,'Nonfinite'): review.load_review(file)
        file.write_text('{"schema":"other"}')
        with self.assertRaisesRegex(ValueError,'Calculation-Review'): review.load_review(file)


class ReviewWindowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.AutoTests(); self.fixture.setUp(); f=self.fixture
        f.job['controls']=[f.control('A',[100,100],600),f.control('B',[140,80],606)]
        self.result=f.run_pdf(); self.path=self.result['manifest']['review_file']
        self.app=gg.GradeGuardApp(); self.app.withdraw(); self.window=None

    def tearDown(self):
        if self.window: self.window.close()
        self.app.destroy(); self.fixture.tearDown()

    def open(self):
        from gradeguard_review_ui import CalculationReviewWindow
        self.app.deiconify(); self.app.update()
        self.window=CalculationReviewWindow(self.app,self.path)
        self.app.update(); return self.window

    def test_plan_profile_and_list_select_same_point_without_changing_result(self):
        before=Path(self.path).read_bytes(); csv=Path(self.result['manifest']['csv']); csv_before=csv.read_bytes()
        w=self.open(); w.select_point('6003'); self.app.update()
        self.assertEqual(w.table.selection(),('6003',)); self.assertEqual(w.path_var.get(),'C1')
        self.assertIn('603.000000',w.details.get('1.0','end'))
        x,y,ident=next(hit for hit in w.profile_hits if hit[2]=='6005')
        w.pick(SimpleNamespace(x=x,y=y),w.profile_hits); self.app.update()
        self.assertEqual(w.selected,'6005'); self.assertEqual(w.table.selection(),('6005',))
        w.table.selection_set('6001'); w.table_selected(); self.app.update()
        self.assertEqual(w.selected,'6001'); self.assertTrue(w.plan_hits)
        self.assertEqual(before,Path(self.path).read_bytes()); self.assertEqual(csv_before,csv.read_bytes())

    def test_changed_pdf_disables_evidence_but_keeps_historical_points(self):
        self.fixture.plan.write_bytes(self.fixture.plan.read_bytes()+b'\n% changed')
        w=self.open(); w.select_point('6000')
        self.assertFalse(w.pdf_verified); self.assertIsNone(w.pdf_doc)
        self.assertEqual(str(w.evidence_button.cget('state')),'disabled')
        self.assertTrue(w.source_status); self.assertIn('historical',w.details.get('1.0','end'))

    def test_new_job_does_not_reopen_previous_job_review(self):
        self.app.last_calculation_review=self.path; self.app.new_job()
        self.assertEqual(self.app.last_calculation_review,'')

    def test_simple_home_routes_pdf_and_reveals_advanced_without_changing_inputs(self):
        view=self.app._workflow
        self.assertNotIn(str(view.tools_page),view.steps.tabs())
        self.app.pdf_var.set(str(self.fixture.plan))
        before=self.app._project_data(); view.show_advanced()
        self.assertIn(str(view.tools_page),view.steps.tabs()); self.assertEqual(before,self.app._project_data())
        with patch('gradeguard_auto_ui.PdfCalculationWindow') as opener: view.quick_calculate()
        opener.assert_called_once_with(self.app)

    def test_pdf_next_requires_scale_and_controls_then_reaches_calculate(self):
        from gradeguard_auto_ui import PdfCalculationWindow
        w=PdfCalculationWindow(self.app); self.window=w; w.vars['pdf'].set(str(self.fixture.plan)); w.load_sheet()
        self.app.update(); self.assertTrue(w.local_fields.winfo_manager()); self.assertFalse(w.survey_fields.winfo_manager())
        with patch('gradeguard_auto_ui.messagebox.showerror') as error: w.next_step()
        error.assert_called_once(); self.assertEqual(w.tabs.select(),str(w.setup))
        w.cal_points=[[100,100],[140,100]]; w.vars['length'].set('40'); w.vars['reference'].set('Synthetic check'); w.units.set(True)
        w.next_step(); self.assertEqual(w.tabs.select(),str(w.sheet))
        w.next_step(); self.assertEqual(w.tabs.select(),str(w.sheet))
        w.paths=self.fixture.job['paths']; w.controls=self.fixture.job['controls']
        w.next_step(); self.assertEqual(w.tabs.select(),str(w.results))
        w.vars['frame'].set('survey')
        self.assertTrue(w.survey_fields.winfo_manager()); self.assertFalse(w.local_fields.winfo_manager())
