"""Every colour scheme: complete, readable (WCAG contrast), applies to the whole app, is remembered, and a hard-coded colour cannot sneak back in."""
import os
import re
import sys
import tempfile
import tkinter as tk
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import prefs  # noqa: E402
import protoprobe as pp  # noqa: E402
import themes  # noqa: E402

CUSTOM = [k for k, p in themes.PALETTES.items() if p["engine"] == "clam"]


class Data(unittest.TestCase):
    def test_every_scheme_has_every_role_and_valid_colours(self):
        self.assertGreaterEqual(len(CUSTOM), 6)
        for key, p in themes.PALETTES.items():
            for role in themes._COMMON:
                self.assertIn(role, p, f"{key} lacks {role}")
            for role, value in p.items():
                if role not in ("label", "engine", "dark"):
                    self.assertRegex(value, r"^#[0-9a-fA-F]{6}$", f"{key}.{role}")

    def test_text_is_readable_in_every_scheme(self):
        """WCAG: 4.5:1 for normal text on its own background."""
        for key, p in themes.PALETTES.items():
            pairs = [("fg", "bg"), ("fg", "panel"), ("fg", "field"), ("muted", "bg"), ("muted", "panel"), ("accent_fg", "accent"),
                     ("select_fg", "select_bg"), ("text_fg", "text_bg"), ("ok", "bg"), ("warn", "bg"), ("fail", "bg")]
            for fg, bg in pairs:
                ratio = themes.contrast(p[fg], p[bg])
                self.assertGreaterEqual(ratio, 4.5, f"{key}: {fg} on {bg} is {ratio:.2f}:1 (needs 4.5)")

    def test_the_accent_is_visible_against_the_background(self):
        for key, p in themes.PALETTES.items():
            self.assertGreaterEqual(themes.contrast(p["accent"], p["bg"]), 3.0, key)       # WCAG 3:1 for interface components

    def test_high_contrast_is_really_high(self):
        p = themes.PALETTES["high_contrast"]
        self.assertGreaterEqual(themes.contrast(p["fg"], p["bg"]), 15)
        self.assertGreaterEqual(themes.contrast(p["border"], p["bg"]), 15)

    def test_default_is_an_industrial_scheme_and_modern_needs_the_package(self):
        self.assertEqual(themes.DEFAULT_KEY, "industrial_dark")
        keys = [k for k, _ in themes.available(False)]
        self.assertNotIn("modern_dark", keys)
        self.assertIn("modern_dark", [k for k, _ in themes.available(True)])

    def test_contrast_maths(self):
        self.assertAlmostEqual(themes.contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(themes.contrast("#777777", "#777777"), 1.0)


class NoHardcodedColours(unittest.TestCase):
    def test_state_colours_come_from_the_scheme(self):
        old = ("#2e7d32", "#c62828", "#c0392b", "#e0a100", "#b9770e", "#1e8449", "#2471a3")
        for name in ("protoprobe.py", "commissioning_screen.py", "help_ui.py", "doctor.py", "capture.py"):
            with open(os.path.join(HERE, "..", name), encoding="utf-8") as f:
                text = "\n".join(l for l in f.read().lower().splitlines() if "health_colours" not in l)    # the health dots keep fixed traffic-light colours
            for hx in old:
                self.assertNotIn(f'"{hx}"', text, f"{name} hard-codes {hx}: use themes.colour(role)")
            self.assertIsNone(re.search(r'foreground=["\']gray["\']', text), f"{name} hard-codes gray: use themes.colour('muted')")


class Applied(unittest.TestCase):
    def setUp(self):
        self.app = pp.App()
        self.app.update()

    def tearDown(self):
        self.app.destroy()
        __import__('gc').collect()    # finalise Tk variables on the main thread (see test_guided_s7_tag.collect_tk_garbage)

    def test_every_available_scheme_applies_and_repaints(self):
        for key, _ in themes.available(pp.SV_TTK_AVAILABLE):
            self.app.apply_palette(key)
            self.app.update()
            self.assertEqual(self.app.palette_key, key)
            p = themes.get(key)
            self.assertEqual(themes.get().get("label"), p["label"])
            if p["engine"] == "clam":
                st = pp.ttk.Style(self.app)
                self.assertEqual(st.lookup("TFrame", "background"), p["bg"])
                self.assertEqual(st.lookup("Accent.TButton", "background"), p["accent"])
                self.assertEqual(st.lookup("Treeview", "fieldbackground"), p["field"])
            self.assertEqual(self.app.read_only_badge.cget("background"), p["ok"])

    def test_text_boxes_follow_the_scheme(self):
        self.app.show_screen("output")
        self.app.apply_palette("control_room")
        self.assertEqual(str(self.app.screens["output"].template_text.cget("bg")), themes.PALETTES["control_room"]["text_bg"])

    def test_status_colours_already_on_screen_move_to_the_new_scheme(self):
        lbl = pp.ttk.Label(self.app.screens["dashboard"], text="x", foreground=themes.colour("fail"))
        lbl.pack()
        old_fail = themes.colour("fail")
        self.app.apply_palette("industrial_light")
        self.assertNotEqual(old_fail, themes.PALETTES["industrial_light"]["fail"])
        self.assertEqual(str(lbl.cget("foreground")).lower(), themes.PALETTES["industrial_light"]["fail"])

    def test_unknown_scheme_falls_back_to_the_default(self):
        self.app.apply_palette("no_such_scheme")
        self.assertEqual(self.app.palette_key, themes.DEFAULT_KEY)

    def test_modern_scheme_without_the_package_falls_back(self):
        self.assertIs(themes.apply(self.app, "modern_dark", None), themes.PALETTES[themes.DEFAULT_KEY])

    def test_settings_screen_switches_and_remembers(self):
        with tempfile.TemporaryDirectory() as d:
            self.app.prefs_path = os.path.join(d, "prefs.json")
            settings = self.app.screens["settings"]
            settings.scheme_var.set(dict(themes.available(pp.SV_TTK_AVAILABLE))["steel_blue"])
            settings._on_scheme_change()
            self.assertEqual(self.app.palette_key, "steel_blue")
            self.assertEqual(prefs.load(self.app.prefs_path)["palette"], "steel_blue")
            self.assertEqual(settings.swatches["accent"].cget("background"), themes.PALETTES["steel_blue"]["accent"])
            settings._choose_scheme(themes.DEFAULT_KEY)
            self.assertEqual(prefs.load(self.app.prefs_path)["palette"], themes.DEFAULT_KEY)

    def test_saved_scheme_is_applied_at_start(self):
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.join(d, "workspace.json")
            prefs.save(prefs.path_for(ws), {"tour_done": True, "palette": "plant_green"})
            with unittest.mock.patch.object(pp.messagebox, "showwarning"):
                self.app.enable_workspace(ws)
            self.assertEqual(self.app.palette_key, "plant_green")

    def test_a_damaged_prefs_value_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prefs.json")
            with open(path, "w") as f:
                f.write('{"schema": 1, "tour_done": true, "palette": 7}')
            self.assertEqual(prefs.load(path)["palette"], "")


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
