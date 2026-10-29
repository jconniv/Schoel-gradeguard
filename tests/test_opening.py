"""Animation lifecycle checks; no project files or calculations are involved."""
import unittest
from unittest.mock import Mock, patch

import schoel_gradeguard as gg
from gradeguard_splash import DozerScene, GradeGuardSplash


class OpeningTests(unittest.TestCase):
    def setUp(self):
        self.app = gg.GradeGuardApp()
        self.app.withdraw()
        self.app.update_idletasks()

    def tearDown(self):
        self.app.destroy()

    def test_dozer_travels_across_scene_with_bounded_canvas_items(self):
        splash = GradeGuardSplash(self.app, gg.APP_VERSION, gg.resource_path, Mock(), duration=30)
        splash.withdraw()
        splash._cancel_timer()
        scene = splash.scene
        self.assertLess(scene.position(0) + 280, 0)
        self.assertGreater(scene.position(1), scene.WIDTH)
        scene.draw_frame(0.25, 1.25)
        first = splash.canvas.bbox("dozer")
        counts = []
        for index in range(100):
            scene.draw_frame(0.5, index / 40)
            counts.append(len(splash.canvas.find_all()))
        self.assertGreater(splash.canvas.bbox("dozer")[0], first[0])
        self.assertLessEqual(max(counts) - min(counts), 1)
        splash.destroy()

    def test_skip_and_timeout_complete_once_and_cancel_timer(self):
        complete = Mock()
        splash = GradeGuardSplash(self.app, gg.APP_VERSION, gg.resource_path, complete, duration=30)
        timer = splash._timer
        splash.finish()
        splash.finish()
        self.assertNotIn(timer, self.app.tk.call("after", "info"))
        complete.assert_called_once_with()
        complete.reset_mock()
        timed = GradeGuardSplash(self.app, gg.APP_VERSION, gg.resource_path, complete, duration=30)
        timed._cancel_timer()
        timed.started -= 31
        timed._tick()
        complete.assert_called_once_with()
        self.assertFalse(timed.winfo_exists())

    def test_replay_is_single_instance_and_preserves_current_job(self):
        self.app.cad_var.set("selected-job.dxf")
        self.app.curb_type_var.set("Custom PDF detail height")
        self.app.height_var.set("0.64")
        before = self.app._project_data()
        self.app.show_opening()
        first = self.app._splash
        self.app.show_opening()
        self.assertIs(first, self.app._splash)
        first.finish()
        self.assertIsNone(self.app._splash)
        self.assertEqual(before, self.app._project_data())

    def test_failed_intro_still_opens_workspace_and_reports_error(self):
        with patch("gradeguard_splash.GradeGuardSplash", side_effect=RuntimeError("synthetic intro error")):
            self.app.show_opening()
        self.app.update_idletasks()
        self.assertNotEqual(self.app.state(), "withdrawn")
        self.assertIn("synthetic intro error", self.app.log.get("1.0", "end"))

    def test_startup_hides_workspace_until_intro_finishes(self):
        startup = gg.GradeGuardApp(show_splash=True)
        try:
            self.assertEqual(startup.state(), "withdrawn")
            self.assertIsNotNone(startup._splash)
            startup._splash.finish()
            startup.update_idletasks()
            self.assertEqual(startup.state(), "normal")
            self.assertIsNone(startup._splash)
        finally:
            startup.destroy()


if __name__ == "__main__":
    unittest.main()
