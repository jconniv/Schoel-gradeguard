"""Known-answer observations and reviewed controls; synthetic coordinates only."""
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
import gradeguard_pdf as pdf
import schoel_gradeguard as gg


class ParsingTests(unittest.TestCase):
    def parse(self, text):
        return [(a,b) for a,b,_ in pdf.parse_callouts(text)]

    def test_prefix_suffix_stacked_and_multiple_values(self):
        cases = {
            'TC 1034.50 FL 1033.50': [('TC',1034.5),('FL',1033.5)],
            '1034.50 TC 1033.50 FL': [('TC',1034.5),('FL',1033.5)],
            '141.30\nHPS': [('HPS',141.3)],
            '141.55\nLPS': [('LPS',141.55)],
            '151.02 (WALK)\nFS': [('FS',151.02)],
            'H.P. (895.50)': [('HP',895.5)],
            'L.P. T.C. 611.70': [('LP/TC',611.7)],
            'HP/TC=612.45': [('HP/TC',612.45)],
            '(895.50 TC 6")': [('TC',895.5)],
            '1059.56 TP/LP': [('TP/LP',1059.56)],
            'HIGH POINT BOC 12.35': [('HIGH POINT/BOC',12.35)],
            '0.125 FG': [('FG',.125)],
            'FG -2.25': [('FG',-2.25)],
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw): self.assertEqual(self.parse(raw),expected)

    def test_bare_spots_have_no_project_range(self):
        for value in (-20.15,0.0,9.25,141.55,891.15,1059.56,4282.23):
            self.assertEqual(self.parse(str(value)),[('SPOT',value)])

    def test_slopes_dimensions_dates_and_prose_are_not_spot_grades(self):
        for text in ('1.5%', '25.00 FT', '0.75 AC','STA 1+20.50','10/28/2026','10.00 SF','4.00"','NOTE 142.35 applies'):
            self.assertEqual(self.parse(text),[],text)

    def test_existing_and_legend_evidence_remain_visible(self):
        self.assertIn('Existing/match',pdf.parse_callouts('145.48\n(e) FS')[0][2])
        self.assertIn('legend/example',pdf.parse_callouts('LEGEND TC 625.0 DENOTES TOP OF CURB')[0][2])

    def test_abbreviation_and_high_low_are_not_engine_meanings(self):
        self.assertEqual(self.parse('BC 721.79'),[('BC',721.79)])
        self.assertIn('plan legend',pdf.parse_callouts('BC 721.79')[0][2])
        self.assertIn('swale',pdf.parse_callouts('HPS 141.30')[0][2])
        self.assertNotIn('HP_TC',[label for label,_ in self.parse('HPS 141.30')])

    def test_ocr_has_explicit_digit_review_warning(self):
        row=pdf.candidates_from_blocks([((1,2,20,30),'TC 891.50')],1,'OCR')[0]
        self.assertIn('verify every digit',row.flags)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.plan=self.root/'test.pdf'
        self.cad=self.root/'test.dxf'
        self.output=self.root/'controls.json'
        with fitz.open() as doc:
            doc.new_page().insert_text((50,50),'HPS 141.30')
            doc.new_page().insert_text((50,50),'1034.50 TC 1033.50 FL')
            doc.new_page().insert_text((50,50),'LPS 141.55')
            doc[2].set_rotation(90)
            doc.save(self.plan)
        doc=ezdxf.new('R2010'); doc.units=21
        doc.modelspace().add_line((0,0),(40,0),dxfattribs={'layer':'P-BC'})
        doc.saveas(self.cad)
        self.row=pdf.PdfGrade('known',2,(50,38,160,52),'FL',1033.5,'1033.50 FL')

    def tearDown(self):
        self.tmp.cleanup()

    def control(self,**changes):
        values=dict(candidate=self.row,elevation=1033.5,easting=20,northing=0,
                    meaning='Flowline/gutter/bottom of curb',role='High point',height=1.0,
                    reference='Synthetic C2 detail; synthetic benchmark; US survey feet',confirmed=True)
        values.update(changes)
        return pdf.reviewed_control(**values)

    def save(self,control=None):
        return pdf.save_reviewed_data(self.output,[control or self.control()],str(self.plan),str(self.cad))

    def test_scan_every_page_and_rotated_page(self):
        rows,info=pdf.scan_pdf(self.plan)
        self.assertEqual([(r.page,r.label,r.elevation) for r in rows],
                         [(1,'HPS',141.3),(2,'TC',1034.5),(2,'FL',1033.5),(3,'LPS',141.55)])
        self.assertEqual(len(info['pages']),3)
        data,rect=pdf.render_evidence(self.plan,3,rows[-1].bbox)
        pix=fitz.Pixmap(data)
        self.assertTrue(0 <= rect[0] < rect[2] <= pix.width)
        self.assertTrue(0 <= rect[1] < rect[3] <= pix.height)

    def test_automatic_pdf_scan_does_not_return_cad_controls(self):
        labels,info=gg.extract_pdf_spot_labels(str(self.plan),.5,[(0,0),(40,40)])
        self.assertEqual(labels,[])
        self.assertEqual(info['pdf_spot_labels'],4)
        self.assertEqual(info['hp_lp_observations'],2)
        self.assertIn('no verified CAD position',info['alignment']['warning'])

    def test_top_is_direct_and_local_lower_grade_conversion_is_once(self):
        top=self.control(elevation=1034.5,meaning='Top/back of curb',height=99)
        lower=self.control()
        self.assertEqual(top['elev'],1034.5)
        self.assertEqual(top['curb_rise'],0)
        self.assertEqual(lower['elev'],1034.5)
        self.save(lower)
        labels,_=gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))
        value,source,conf=gg.nearest_label_elevation((20,0),labels,.5,2)
        self.assertEqual(value,1034.5)
        self.assertIn('local rise 1.0',source)
        self.assertEqual(conf,'HELD_CONTROL')

    def test_flush_and_four_inch_rise(self):
        self.assertEqual(self.control(height=0)['elev'],1033.5)
        self.assertAlmostEqual(self.control(height=4/12)['elev'],1033.5+1/3)

    def test_no_default_meaning_location_height_or_approval(self):
        for change in ({'meaning':''},{'role':''},{'height':''},{'easting':''},{'reference':''},{'confirmed':False},
                       {'height':-.1},{'northing':float('nan')},{'elevation':float('inf')}):
            with self.subTest(change=change),self.assertRaises((ValueError,TypeError)):
                self.control(**change)

    def test_saved_review_requires_matching_pdf_and_dxf(self):
        data=self.save()
        pdf.validate_reviewed_data(data,str(self.plan),str(self.cad))
        for field in ('pdf_sha256','dxf_sha256'):
            bad=copy.deepcopy(data); bad[field]='changed'
            with self.assertRaises(ValueError): pdf.validate_reviewed_data(bad,str(self.plan),str(self.cad))
        with self.assertRaises(ValueError): gg.load_manual_anchor_file(str(self.output),str(self.cad))

    def test_changed_pdf_or_dxf_rejects_controls(self):
        self.save()
        original=self.plan.read_bytes()
        self.plan.write_bytes(original+b'\n')
        with self.assertRaises(ValueError): gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))
        self.plan.write_bytes(original)
        self.cad.write_bytes(self.cad.read_bytes()+b'\n')
        with self.assertRaises(ValueError): gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))

    def test_changed_conversion_unconfirmed_and_invalid_page_are_rejected(self):
        data=self.save()
        for key,value in [('elev',1035.0),('confirmed',False),('kind','FL'),('page',4),('units','meters'),('elev',float('nan'))]:
            bad=copy.deepcopy(data); bad['manual_spots'][0][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                pdf.validate_reviewed_data(bad,str(self.plan),str(self.cad))

    def test_control_must_be_on_selected_curb(self):
        self.save()
        labels,_=gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))
        segment=gg.CurbSegment((0,0),(40,0),'P-BC')
        gg.validate_reviewed_controls_on_curb(labels,[segment])
        labels[0].y=5
        with self.assertRaises(ValueError): gg.validate_reviewed_controls_on_curb(labels,[segment])
        with self.assertRaises(ValueError): gg.validate_reviewed_controls_on_curb(labels,[])

    def test_hp_lp_saved_controls_hold_exact_elevation(self):
        for role,desc in [('High point','BC HP'),('Low point','BC LP')]:
            self.save(self.control(role=role))
            labels,_=gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))
            points=[gg.GenPoint(6000,0,20,600)]
            self.assertEqual(gg.hard_lock_hp_lp_callout_elevations(points,labels),1)
            self.assertEqual(points[0].elevation,1034.5)
            self.assertEqual(points[0].desc,desc)
            self.assertTrue(gg.field_approval(points[0])[0])

    def test_full_curb_generation_holds_converted_pdf_control(self):
        doc=ezdxf.readfile(self.cad)
        for x,z in [(0,600),(40,602)]:
            doc.modelspace().add_line((x,-10,z),(x,10,z),dxfattribs={'layer':'P-CONTOUR'})
        doc.saveas(self.cad)
        self.save(self.control(elevation=610,height=1.0))
        try:
            with patch.object(gg,'write_water_flow_pdf',return_value=None):
                result=gg.run_generation(str(self.cad),str(self.plan),str(self.root/'output'),6000,20,.5,
                                         'P-BC','CONTOUR',1,50,.003,manual_anchor_path=str(self.output))
            with open(result[0],newline='') as stream:
                rows=list(csv.reader(stream))
            held=[r for r in rows if float(r[2])==20]
            self.assertEqual(len(held),1)
            self.assertEqual(float(held[0][3]),611)
            self.assertIn('HP',held[0][4])
        finally:
            gg.reset_generation_state()

    def test_overwrite_preserves_previous_review(self):
        original=self.save()
        pdf.save_reviewed_data(self.output,[],str(self.plan),str(self.cad))
        self.assertEqual(json.loads(Path(str(self.output)+'.bak').read_text()),original)

    def test_malformed_control_file_does_not_silently_continue(self):
        self.output.write_text('{broken')
        with self.assertRaises(ValueError): gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))

    def test_review_window_accept_save_reload_and_remove(self):
        from gradeguard_pdf_ui import PdfReviewWindow
        app=gg.GradeGuardApp()
        app.withdraw()
        app.pdf_var.set(str(self.plan)); app.cad_var.set(str(self.cad))
        app.anchor_path=str(self.root/'absent.json')
        window=None
        try:
            window=PdfReviewWindow(app)
            deadline=time.monotonic()+10
            while window.busy and time.monotonic()<deadline:
                app.update(); time.sleep(.01)
            self.assertFalse(window.busy)
            self.assertEqual(len(window.rows),4)
            row=next(r for r in window.rows if r.label=='FL')
            window.tree.selection_set(row.id); window.select()
            fields=dict(elevation='1033.50',easting='20',northing='0',meaning='Flowline/gutter/bottom of curb',
                        role='High point',height='1.0',reference='Synthetic C2 detail and benchmark; US survey feet')
            for key,value in fields.items(): window.fields[key].set(value)
            with patch('gradeguard_pdf_ui.messagebox.showerror') as error:
                window.accept()
                self.assertEqual(window.spots,{})
                error.assert_called_once()
            window.confirmed.set(True)
            window.accept()
            self.assertEqual(window.spots[row.id]['elev'],1034.5)
            with patch('gradeguard_pdf_ui.filedialog.asksaveasfilename',return_value=str(self.output)):
                window.save()
            self.assertEqual(app.anchor_path,str(self.output))
            self.assertEqual(len(gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))[0]),1)
            window.spots={}; window.restore()
            self.assertEqual(len(window.spots),1)
            window.remove()
            with patch('gradeguard_pdf_ui.filedialog.asksaveasfilename',return_value=str(self.output)):
                window.save()
            self.assertEqual(gg.load_manual_anchor_file(str(self.output),str(self.cad),str(self.plan))[0],[])
        finally:
            if window: window.destroy()
            app.destroy()


if __name__ == '__main__':
    unittest.main()
