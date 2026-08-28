import unittest

import schoel_gradeguard as gg


class HpLpParsingTests(unittest.TestCase):
    def test_supported_pdf_callout_forms_are_unique(self):
        cases = {
            "HP 612.34": [("HP", 612.34)],
            "LP 611.80": [("LP", 611.80)],
            "HP/TC=612.45": [("HP_TC", 612.45)],
            "L.P. T.C. 611.70": [("LP_TC", 611.70)],
            "HIGH POINT BOC 613.25": [("HP_BOC_AMBIG", 613.25)],
            "LOW POINT BOC 610.95": [("LP_BOC_AMBIG", 610.95)],
            "HIGH POINT BACK OF CURB 613.25": [("HP_TC", 613.25)],
            "LOW POINT BOTTOM OF CURB 610.45": [("LP_BOTTOM_CURB", 610.45)],
            "LOW POINT BC 610.95": [("LP_TC", 610.95)],
            "HP EL. 612.34": [("HP", 612.34)],
            "LP ELEV 611.80": [("LP", 611.80)],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(gg.parse_grade_text(text), expected)

    def test_hard_hold_preserves_exact_hp_and_lp(self):
        points = [
            gg.GenPoint(6000, 100.0, 200.0, 600.0),
            gg.GenPoint(6001, 110.0, 210.0, 600.0),
        ]
        labels = [
            gg.GradeLabel(200.0, 100.0, "HP_TC", 612.345, "HP/TC 612.345", "PDF leader"),
            gg.GradeLabel(210.0, 110.0, "LP", 610.125, "LP 610.125", "PDF leader"),
        ]
        self.assertEqual(gg.hard_lock_hp_lp_callout_elevations(points, labels), 2)
        self.assertEqual((points[0].desc, points[0].elevation), ("BC HP", 612.345))
        self.assertEqual((points[1].desc, points[1].elevation), ("BC LP", 610.125))
        self.assertIn("HP_HARD_HOLD", points[0].confidence)
        self.assertIn("LP_HARD_HOLD", points[1].confidence)

    def test_automatic_pdf_hard_hold_requires_controlled_alignment_and_vector_tip(self):
        vector_label = gg.GradeLabel(
            200.0, 100.0, "HP", 612.345, "HP 612.345",
            "PDF grading spot page 6 at leader/arrow/tick endpoint",
        )
        text_center_label = gg.GradeLabel(
            200.0, 100.0, "HP", 612.345, "HP 612.345",
            "PDF grading spot page 6 at text box center fallback",
        )
        self.assertTrue(gg.pdf_hp_lp_hard_hold_is_trusted(vector_label, {"rmse_ft": 0.75, "warning": ""}))
        self.assertFalse(gg.pdf_hp_lp_hard_hold_is_trusted(vector_label, {"rmse_ft": 1.01, "warning": ""}))
        self.assertFalse(gg.pdf_hp_lp_hard_hold_is_trusted(vector_label, {"rmse_ft": 0.25, "warning": "approximate"}))
        self.assertFalse(gg.pdf_hp_lp_hard_hold_is_trusted(text_center_label, {"rmse_ft": 0.25, "warning": ""}))

    def test_ambiguous_boc_is_not_a_hard_hold_kind(self):
        self.assertFalse(gg._is_hp_lp_label_kind("HP_BOC_AMBIG"))
        self.assertFalse(gg._is_hp_lp_label_kind("LP_BOC_AMBIG"))
        self.assertFalse(gg._is_hp_lp_label_kind("HP_BOTTOM_CURB"))


if __name__ == "__main__":
    unittest.main()
