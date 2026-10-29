"""Exercise the real Tk view with synthetic paths and mocked generation only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import schoel_gradeguard as gg


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.app = gg.GradeGuardApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.view = self.app._workflow

    def tearDown(self):
        self.app.destroy()

    def test_file_status_distinguishes_missing_wrong_format_and_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            cad = Path(folder) / "geometry.dxf"
            cad.touch()
            self.app.cad_var.set(str(cad))
            self.app.pdf_var.set(str(Path(folder) / "missing.pdf"))
            self.view.refresh_files()
            labels = [str(row[1].cget("text")) for row in self.view.file_status]
            self.assertIn("contents and survey references still require review", labels[0])
            self.assertIn("File not found", labels[1])
            self.assertIn("Optional", labels[2])
            self.app.cad_var.set(str(cad.with_suffix(".dwg")))
            self.view.refresh_files()
            self.assertIn(".dxf format", self.view.file_status[0][1].cget("text"))

    def test_navigation_preserves_job_settings_and_existing_roundtrip(self):
        self.app.cad_var.set("chosen.dxf")
        self.app.pdf_var.set("certified.pdf")
        self.app.curb_type_var.set("Custom PDF detail height")
        self.app.height_var.set("0.61")
        self.app.start_var.set("6100")
        self.app.create_offset_var.set(True)
        self.app.offset_distance_var.set("4.5")
        self.app.offset_mode_var.set("left")
        self.app.offset_reverse_var.set(True)
        self.app.source_mode.set("contours")
        self.app.surface_name_var.set("Proposed Grading")
        self.app.unitless_confirm_var.set(True)
        self.app.esw_width_var.set("8.25")
        self.app.esw_adjust_var.set("subtract cross slope")
        self.app.esw_auto_width_var.set(False)
        self.app.flout_search_radius_var.set("75")
        self.app.selected_sidewalk_path = "job-sidewalk.json"
        self.app.manual_flout_path = "job-structures.json"
        before = self.app._project_data()
        for tab in self.view.steps.tabs():
            self.view.steps.select(tab)
            self.app.update_idletasks()
        for tab in self.view.tools.tabs():
            self.view.tools.select(tab)
        self.assertEqual(before, self.app._project_data())

        self.app.new_job()
        self.app._load_project_data(before)
        self.assertEqual(before, self.app._project_data())

    def test_new_job_clears_surface_confirmation_and_cached_controls(self):
        self.app.surface_name_var.set("Old FG")
        self.app.unitless_confirm_var.set(True)
        self.app.esw_width_var.set("30")
        gg.CONTOUR_POINT_OVERRIDES[(0,0)] = (999,'old','BC_CONTOUR_EXACT','')
        self.app.new_job()
        self.assertEqual(self.app.surface_name_var.get(),"")
        self.assertFalse(self.app.unitless_confirm_var.get())
        self.assertEqual(self.app.esw_width_var.get(),"5.00")
        self.assertEqual(gg.CONTOUR_POINT_OVERRIDES,{})

    def test_curb_presets_and_custom_height_keep_existing_behavior(self):
        for preset, expected in [("Catch curb per PDF (+0.42)", "0.42"), ("Spill curb per PDF (+0.58)", "0.58"), ("Standard curb (+0.50)", "0.50")]:
            self.app.curb_type_var.set(preset)
            self.assertEqual(self.app.height_var.get(), expected)
        self.app.curb_type_var.set("Custom PDF detail height")
        self.app.height_var.set("0.62")
        self.assertEqual(self.app._selected_curb_height_and_note()[0], 0.62)

    def test_busy_state_blocks_job_actions_and_recovers_on_failure(self):
        self.app._generation_running = True
        self.view.set_busy(True)
        self.assertTrue(all(str(button.cget("state")) == "disabled" for button in self.view.job_buttons))
        with patch.object(gg.messagebox, "showerror") as error:
            self.app._generation_failed("Synthetic failure", "test details")
        self.assertFalse(self.app._generation_running)
        self.assertTrue(all(str(button.cget("state")) == "normal" for button in self.view.job_buttons))
        self.assertEqual(self.view.steps.select(), str(self.view.results_page))
        self.assertIn("Synthetic failure", self.app.log.get("1.0", "end"))
        error.assert_called_once()

    def test_bc_button_keeps_existing_generation_callback(self):
        with patch.object(self.app, "generate") as generate:
            self.app.generate_button.invoke()
        generate.assert_called_once_with()
        self.assertFalse(self.app._generation_running)

    def test_missing_output_folder_does_not_launch_explorer(self):
        self.app.out_var.set("")
        with patch("gradeguard_ui.os.startfile") as launch, patch("gradeguard_ui.messagebox.showinfo") as info:
            self.view.open_output()
        launch.assert_not_called()
        info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
