import csv
import tempfile
import unittest
from pathlib import Path

import schoel_gradeguard as gg


class SafeExportTests(unittest.TestCase):
    def test_review_point_is_withheld_instead_of_zeroed(self):
        point = gg.GenPoint(6000, 100.0, 200.0, 612.34, source="nearby contour fallback", confidence="LOW_CONF")
        self.assertEqual(gg.sanitize_field_elevations([point]), 1)
        self.assertIsNone(point.elevation)
        self.assertIn("withheld from stake-ready export", point.issue)

    def test_trusted_manual_hold_remains_stake_ready(self):
        point = gg.GenPoint(6000, 100.0, 200.0, 612.34, source="Manual PDF tick hold", confidence="PDF_TICK")
        self.assertEqual(gg.sanitize_field_elevations([point]), 0)
        self.assertEqual(point.elevation, 612.34)

    def test_field_csv_omits_withheld_points(self):
        points = [
            gg.GenPoint(6000, 100.0, 200.0, 612.34, source="Manual PDF tick hold", confidence="PDF_TICK"),
            gg.GenPoint(6001, 101.0, 201.0, None, issue="Review required"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "stake.csv"
            self.assertEqual(gg.write_csv(points, str(output)), 1)
            with output.open(encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual(rows, [["6000", "100.000", "200.000", "612.340", "BC"]])

    def test_do_not_stake_report_is_explicit(self):
        points = [gg.GenPoint(6001, 101.0, 201.0, None, issue="Review required")]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "do_not_stake.csv"
            self.assertEqual(gg.write_do_not_stake_csv(points, str(output)), 1)
            with output.open(encoding="utf-8") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual(rows[0][0], "DO_NOT_STAKE")
        self.assertEqual(rows[1][0], "DO_NOT_STAKE")
        self.assertIn("Review required", rows[1][-1])


if __name__ == "__main__":
    unittest.main()
