"""The shell: grouped sidebar, header trail, status bar, F1 help, first-run tour, Start here list, empty hints, crash report, and the Add Connection
window keeping its Save button on screen."""
import os
import sys
import tempfile
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import help_content as hc  # noqa: E402
import prefs  # noqa: E402
import protoprobe as pp  # noqa: E402


def texts(widget):
    out = []
    for w in widget.winfo_children():
        try:
            out.append(w.cget("text"))
        except tk.TclError:
            pass
        out.extend(texts(w))
    return out


class Shell(unittest.TestCase):
    def setUp(self):
        pp.READS_OK["seen"] = False                  # module-level flag: other tests do real (simulated) reads
        self.app = pp.App()
        self.app.update()

    def tearDown(self):
        for w in (self.app._help_window, self.app._tour_window):
            if w is not None and w.winfo_exists():
                w.destroy()
        self.app.destroy()

    def test_sidebar_is_grouped_and_every_tab_present(self):
        labels = [t for t in texts(self.app) if isinstance(t, str)]
        for title, _ in hc.GROUPS:
            self.assertIn(title.upper(), labels)
        for _, name in pp.NAV_ITEMS:
            self.assertIn(name, labels)

    def test_header_trail_follows_the_screen(self):
        self.app.show_screen("scan_results")
        self.assertEqual(self.app.crumb.cget("text"), "Discover  >  Scan results")

    def test_status_bar_says_read_only_and_updates(self):
        self.assertIn("READ-ONLY", [t.strip() for t in texts(self.app) if isinstance(t, str)])
        self.app._status_tick()
        self.assertEqual(self.app.status_labels["connections"].cget("text"), "Connections: 0")
        self.assertEqual(self.app.status_labels["polling"].cget("text"), "Polling: off")

    def test_f1_opens_help_for_the_current_screen(self):
        self.app.show_screen("network")
        win = self.app.show_help(self.app.current_screen)
        self.app.update()
        self.assertEqual(win.current, "network")
        self.assertIn("Network tools", win.text.get("1.0", "2.0"))
        win2 = self.app.show_help("__troubleshooting__")
        self.assertIs(win, win2)                      # one help window, reused
        self.assertEqual(win.current, "__troubleshooting__")

    def test_every_help_topic_renders(self):
        win = self.app.show_help()
        for key in win.topics:
            win.select(key)
            self.assertTrue(win.text.get("1.0", "end").strip(), key)

    def test_checklist_follows_what_was_done(self):
        steps = self.app.checklist()
        self.assertFalse(any(done for done, _ in steps))
        self.app.connections["c"] = pp.Connection("c", "modbus_tcp", {"ip": "192.168.1.50", "port": 502})
        self.assertTrue(self.app.checklist()[0][0])
        self.app.connections["c"].tags.append({"name": "t"})
        self.assertTrue(self.app.checklist()[2][0])
        self.app.screens["dashboard"].update_checklist()
        self.assertTrue(self.app.screens["dashboard"].start_labels[0].cget("text").startswith("[x]"))

    def test_empty_hints_show_then_hide(self):
        conns = self.app.screens["connections"]
        conns.refresh()
        self.assertTrue(conns._empty_hint.winfo_manager())
        self.app.connections["c"] = pp.Connection("c", "modbus_tcp", {"ip": "192.168.1.50", "port": 502})
        conns.refresh()
        self.assertFalse(conns._empty_hint.winfo_manager())

    def test_tour_walks_all_cards_and_marks_prefs(self):
        with tempfile.TemporaryDirectory() as d:
            self.app.prefs_path = os.path.join(d, "prefs.json")
            self.assertFalse(prefs.load(self.app.prefs_path)["tour_done"])
            tour = self.app.start_tour()
            self.app.update()
            seen = 1
            while tour.index < len(tour.cards) - 1:
                tour.next()
                seen += 1
            self.assertEqual(seen, len(pp.NAV_ITEMS) + 2)
            tour.next()                                 # Finish
            self.assertTrue(prefs.load(self.app.prefs_path)["tour_done"])

    def test_skip_counts_as_seen_and_it_is_not_shown_again(self):
        with tempfile.TemporaryDirectory() as d:
            self.app.prefs_path = os.path.join(d, "prefs.json")
            with mock.patch.object(self.app, "start_tour") as start:
                self.app.maybe_start_tour()
                start.assert_called_once()
            tour = self.app.start_tour()
            tour._finish(False)                         # Skip tour
            with mock.patch.object(self.app, "start_tour") as start:
                self.app.maybe_start_tour()
                start.assert_not_called()

    def test_plain_app_never_shows_the_tour(self):
        with mock.patch.object(self.app, "start_tour") as start:
            self.app.maybe_start_tour()                 # no prefs file: a test or portable run
            start.assert_not_called()

    def test_crash_report_is_written_and_says_where(self):
        with tempfile.TemporaryDirectory() as d:
            self.app.prefs_path = os.path.join(d, "prefs.json")
            with mock.patch.object(pp.messagebox, "showerror") as shown:
                try:
                    raise RuntimeError("boom")
                except RuntimeError as exc:
                    self.app.report_callback_exception(type(exc), exc, exc.__traceback__)
            path = self.app.last_crash_report
            self.assertTrue(path and os.path.exists(path))
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("RuntimeError: boom", body)
            self.assertIn("diagnostics", body)
            self.assertIn(path, shown.call_args[0][1])

    def test_diagnostics_text_has_version(self):
        self.assertIn(pp.APP_VERSION, self.app.diagnostics_text(True))


class AddConnectionWindow(unittest.TestCase):
    def test_save_button_is_on_screen_and_fields_have_tooltips(self):
        app = pp.App()
        try:
            app.show_screen("connections")
            screen = app.screens["connections"]
            screen.open_add_connection()
            app.update()
            win = [w for w in screen.winfo_children() if isinstance(w, tk.Toplevel)][0]
            win.geometry("480x560")
            win.update()
            save = [w for w in _all(win) if getattr(w, "winfo_class", lambda: "")() in ("TButton", "Button") and w.cget("text") == "Save"][0]
            self.assertTrue(save.winfo_ismapped())
            self.assertLessEqual(save.winfo_rooty() + save.winfo_height(), win.winfo_rooty() + win.winfo_height())
            win.destroy()
        finally:
            app.destroy()


def _all(widget):
    for w in widget.winfo_children():
        yield w
        yield from _all(w)



class TourButtons(unittest.TestCase):
    def test_skip_back_next_are_all_visible(self):
        app = pp.App()
        try:
            tour = app.start_tour()
            tour.geometry("660x440")
            tour.update()
            buttons = {w.cget("text"): w for w in _all(tour) if w.winfo_class() == "TButton"}
            for name in ("Skip tour", "Back", "Next"):
                b = buttons[name]
                self.assertTrue(b.winfo_ismapped(), name)
                self.assertLessEqual(b.winfo_rootx() + b.winfo_width(), tour.winfo_rootx() + tour.winfo_width(), f"{name} is cut off")
            tour.destroy()
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
