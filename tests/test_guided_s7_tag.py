"""The guided Add Siemens tag dialog and the one-tag Test read."""
import logging
import os
import sys
import tkinter as tk
import unittest
from tkinter import ttk

HERE = os.path.dirname(__file__)
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "edge-gateway")]
import protoprobe as pp  # noqa: E402
from tkinter import messagebox  # noqa: E402


class FakeS7:
    """Answers the tag named Good; the tag Bad is logged as the real driver does and left out."""
    def __init__(self, cfg):
        self.cfg = cfg
        self.log = logging.getLogger("edge_collector.s7")

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def poll(self):
        out = {}
        for t in self.cfg["tags"]:
            if t["name"] == "Bad":
                self.log.warning("[%s] tag %s decode error: %s", "g", t["name"], "Read operation failed: Object does not exist (0x0a)")
            else:
                out[t["mqtt_key"]] = 12.5
        return out


class ReadTagOnce(unittest.TestCase):
    conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})

    def test_value_comes_back(self):
        value, error = pp.read_tag_once(self.conn, {"name": "Good", "area": "M", "start_byte": 0, "data_type": "int16"}, {"s7comm": FakeS7})
        self.assertEqual((value, error), (12.5, None))

    def test_the_reason_comes_back(self):
        value, error = pp.read_tag_once(self.conn, {"name": "Bad", "area": "DB", "db_number": 1, "start_byte": 0, "data_type": "int16"},
                                        {"s7comm": FakeS7})
        self.assertIsNone(value)
        self.assertIn("Object does not exist", error)

    def test_connection_failure_is_a_message_not_a_crash(self):
        class Refuses(FakeS7):
            async def connect(self):
                raise ConnectionError("timed out")
        value, error = pp.read_tag_once(self.conn, {"name": "Good", "area": "M", "start_byte": 0, "data_type": "int16"}, {"s7comm": Refuses})
        self.assertEqual((value, error), (None, "timed out"))


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


class GuidedDialog(unittest.TestCase):
    def setUp(self):
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")
        self._orig = messagebox.showerror
        self.errors = []
        messagebox.showerror = lambda *a, **k: self.errors.append(a)
        self.conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
        self.saved = []
        self.win = self.app.screens["connections"]._s7_guided_dialog(self.app, self.conn, lambda: self.saved.append(1))
        self.app.update()

    def tearDown(self):
        messagebox.showerror = self._orig
        self.app.destroy()
        tk._default_root = self._saved_root

    def button(self, text):
        return next(w for w in walk(self.win) if w.winfo_class() in ("TRadiobutton", "TButton") and w.cget("text") == text)

    def entries(self):
        return [w for w in walk(self.win) if w.winfo_class() == "TEntry"]      # name, db, byte, bit, scale, offset, unit

    def set_entry(self, widget, text):
        widget.delete(0, "end")
        widget.insert(0, text)
        self.win.update()

    def test_byte_buttons_are_disabled(self):
        for text in ("IB", "QB", "MB", "DBn.DBB"):
            self.assertTrue(str(self.button(text).cget("state")) == "disabled", text)

    def test_dint_in_markers(self):
        e = self.entries()
        self.set_entry(e[0], "HMI_line_speed")
        self.button("MD").invoke()
        self.set_entry(e[2], "60")
        self.win.update()
        preview = next(w for w in walk(self.win) if w.winfo_class() == "TLabel" and "MD60" in str(w.cget("text")))
        self.assertIn("MD60", str(preview.cget("text")))
        next(w for w in walk(self.win) if w.winfo_class() == "TCombobox").set("int32")
        self.button("OK").invoke()
        tag = self.conn.tags[0]
        self.assertEqual((tag["name"], tag["area"], tag["start_byte"], tag["data_type"]), ("HMI_line_speed", "M", 60, "int32"))
        self.assertEqual(self.saved, [1])

    def test_bit_in_outputs(self):
        e = self.entries()
        self.set_entry(e[0], "Tower_lamp_O")
        self.button("Q").invoke()
        self.set_entry(e[2], "0")
        self.set_entry(e[3], "5")
        self.button("OK").invoke()
        tag = self.conn.tags[0]
        self.assertEqual((tag["area"], tag["start_byte"], tag["bit_offset"], tag["data_type"]), ("Q", 0, 5, "bit"))

    def test_a_bad_bit_is_refused_with_a_message(self):
        e = self.entries()
        self.button("M").invoke()
        self.set_entry(e[3], "9")
        self.button("OK").invoke()
        self.assertEqual(self.conn.tags, [])
        self.assertTrue(self.errors)


if __name__ == "__main__":
    unittest.main()


class QuickRead(unittest.TestCase):
    def setUp(self):
        self._saved_root = tk._default_root
        tk._default_root = None
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")
        self.old = pp.DRIVER_FACTORY["s7comm"]
        pp.DRIVER_FACTORY["s7comm"] = FakeS7
        self.conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})
        self.app.connections["c"] = self.conn
        self.scr = self.app.screens["connections"]
        self.scr.refresh()
        self.scr.conn_tree.selection_set("c")

    def tearDown(self):
        pp.DRIVER_FACTORY["s7comm"] = self.old
        self.app.destroy()
        tk._default_root = self._saved_root

    def pump(self, seconds):
        import time
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.02)

    def test_read_now_shows_the_value_without_making_a_tag(self):
        win = self.scr.open_quick_read()
        self.app.update()
        read = next(w for w in walk(win) if w.winfo_class() == "TButton" and w.cget("text") == "Read now")
        read.invoke()
        self.pump(1.0)
        big = [w for w in walk(win) if w.winfo_class() == "TLabel" and str(w.cget("text")) == "12.5"]
        self.assertTrue(big)
        self.assertEqual(self.conn.tags, [])

    def test_add_as_tag_keeps_it(self):
        win = self.scr.open_quick_read()
        self.app.update()
        pp.messagebox.showinfo = lambda *a, **k: None
        next(w for w in walk(win) if w.winfo_class() == "TButton" and w.cget("text") == "Add as tag").invoke()
        self.assertEqual(len(self.conn.tags), 1)

    def test_it_opens_from_inside_the_scan_window(self):
        pp.messagebox.showinfo = lambda *a, **k: None
        self.scr.open_scan_dialog()
        self.app.update()
        scan = [w for w in walk(self.app) if isinstance(w, tk.Toplevel)][0]
        next(w for w in walk(scan) if w.winfo_class() == "TButton" and str(w.cget("text")).startswith("Read one value")).invoke()
        self.app.update()
        tops = [w for w in walk(self.app) if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(tops), 2)
        self.assertTrue(any(str(w.title()).startswith("Read a value") for w in tops))


class ReadTagsOnce(unittest.TestCase):
    conn = pp.Connection("c", "s7comm", {"ip": "192.0.2.1", "rack": 0, "slot": 1})

    def test_several_tags_one_connection_each_with_its_own_answer(self):
        tags = [{"name": "Good", "area": "M", "start_byte": 0, "data_type": "int16"},
                {"name": "Bad", "area": "DB", "db_number": 1, "start_byte": 0, "data_type": "int16"}]
        out = pp.read_tags_once(self.conn, tags, {"s7comm": FakeS7})
        self.assertEqual(out["Good"], (12.5, None))
        self.assertIsNone(out["Bad"][0])
        self.assertIn("Object does not exist", out["Bad"][1])

    def test_no_connection_is_the_reason_for_every_tag(self):
        class Refuses(FakeS7):
            async def connect(self):
                raise ConnectionError("no route")
        out = pp.read_tags_once(self.conn, [{"name": "a", "area": "M", "start_byte": 0, "data_type": "int16"},
                                            {"name": "b", "area": "M", "start_byte": 2, "data_type": "int16"}], {"s7comm": Refuses})
        self.assertEqual(out, {"a": (None, "no route"), "b": (None, "no route")})


class WatchList(QuickRead):
    def open(self):
        win = self.scr.open_quick_read()
        self.app.update()
        self.win = win
        return win

    def btn(self, text):
        return next(w for w in walk(self.win) if w.winfo_class() in ("TButton", "TRadiobutton", "TCheckbutton") and w.cget("text") == text)

    def tree(self):
        return next(w for w in walk(self.win) if w.winfo_class() == "Treeview")

    def test_add_several_and_read_them_together(self):
        pp.messagebox.showinfo = lambda *a, **k: None
        self.open()
        ents = [w for w in walk(self.win) if w.winfo_class() == "TEntry"]
        for name, offset in (("A", "0"), ("B", "2")):
            ents[0].delete(0, "end")
            ents[0].insert(0, name)
            ents[2].delete(0, "end")
            ents[2].insert(0, offset)
            self.btn("MW").invoke()
            self.btn("Add to list").invoke()
        self.assertEqual(len(self.tree().get_children()), 2)
        self.btn("Read list now").invoke()
        self.pump(1.0)
        values = [self.tree().item(i)["values"][2] for i in self.tree().get_children()]
        self.assertEqual([str(v) for v in values], ["12.5", "12.5"])
        self.assertEqual(self.conn.tags, [])               # nothing became a tag yet
        self.btn("Add all as tags").invoke()
        self.assertEqual([t["name"] for t in self.conn.tags], ["A", "B"])

    def test_remove_from_the_list(self):
        self.open()
        self.btn("Add to list").invoke()
        tree = self.tree()
        tree.selection_set(tree.get_children()[0])
        self.btn("Remove selected").invoke()
        self.assertEqual(tree.get_children(), ())


class EditTagOnLiveValues(QuickRead):
    def test_changing_the_type_updates_the_tag_and_restarts_the_reader(self):
        tag = {"name": "Speed", "area": "M", "db_number": 0, "start_byte": 60, "data_type": "int16"}
        self.conn.tags = [tag]
        live = self.app.screens["live_values"]
        restarted = []
        self.app.restart_poller = lambda conn: restarted.append(conn.name) or True
        win = live.edit_tag_dialog(self.conn, tag)
        self.app.update()
        combo = next(w for w in walk(win) if w.winfo_class() == "TCombobox")
        combo.set("int32")
        entries = [w for w in walk(win) if w.winfo_class() == "TEntry"]       # scale, offset, unit
        entries[0].delete(0, "end")
        entries[0].insert(0, "0.1")
        entries[2].insert(0, "rpm")
        self.conn.latest_values["Speed"] = 5
        next(w for w in walk(win) if w.winfo_class() == "TButton" and w.cget("text") == "Save").invoke()
        self.assertEqual((tag["data_type"], tag["multiplier"], tag["unit"]), ("int32", 0.1, "rpm"))
        self.assertNotIn("Speed", self.conn.latest_values)
        self.assertEqual(restarted, ["c"])

    def test_a_bit_tag_can_only_stay_a_bit(self):
        tag = {"name": "Lamp", "area": "Q", "db_number": 0, "start_byte": 0, "bit_offset": 5, "data_type": "bit"}
        win = self.app.screens["live_values"].edit_tag_dialog(self.conn, tag)
        combo = next(w for w in walk(win) if w.winfo_class() == "TCombobox")
        self.assertEqual(list(combo["values"]), ["bit"])

    def test_the_restart_really_replaces_a_running_poller(self):
        self.conn.tags = [{"name": "A", "area": "M", "start_byte": 0, "data_type": "int16"}]
        old = pp.PollerThread(self.conn, 0.05, self.app.out_queue)
        self.conn.thread = old
        old.start()
        self.assertTrue(self.app.restart_poller(self.conn))
        self.assertIsNot(self.conn.thread, old)
        self.conn.thread.stop()
        old.join(timeout=3)
        self.assertFalse(old.is_alive())


class FixAWrongAddress(QuickRead):
    def test_md4_dint_becomes_the_run_bit_m0_1_in_place(self):
        tag = {"name": "Run", "area": "M", "db_number": 0, "start_byte": 4, "data_type": "int32", "multiplier": 1.0, "offset": 0.0}
        self.conn.tags = [tag]
        self.conn.latest_values["Run"] = 123
        restarted = []
        self.app.restart_poller = lambda conn: restarted.append(conn.name) or True
        pp.messagebox.showinfo = lambda *a, **k: None
        live = self.app.screens["live_values"]
        live.refresh()
        live.live_tree.selection_set("c::Run")
        win = live.edit_selected_tag()
        self.app.update()
        # it starts from what the tag is now
        preview = next(w for w in walk(win) if w.winfo_class() == "TLabel" and "MD4" in str(w.cget("text")))
        self.assertIn("MD4", str(preview.cget("text")))
        next(w for w in walk(win) if w.winfo_class() == "TRadiobutton" and w.cget("text") == "M").invoke()      # M, bit
        entries = [w for w in walk(win) if w.winfo_class() == "TEntry"]                                           # name, db, byte, bit ...
        entries[2].delete(0, "end")
        entries[2].insert(0, "0")
        entries[3].delete(0, "end")
        entries[3].insert(0, "1")
        win.update()
        next(w for w in walk(win) if w.winfo_class() == "TButton" and w.cget("text") == "Save").invoke()
        same = self.conn.tags[0]
        self.assertIs(same, tag)                                                   # edited in place, not replaced
        self.assertEqual((tag["name"], tag["area"], tag["start_byte"], tag["bit_offset"], tag["data_type"]), ("Run", "M", 0, 1, "bit"))
        self.assertNotIn("Run", self.conn.latest_values)
        self.assertEqual(restarted, ["c"])


class FixInTheWatchList(WatchList):
    def test_double_click_loads_a_row_and_update_replaces_it(self):
        self.open()
        ents = [w for w in walk(self.win) if w.winfo_class() == "TEntry"]
        ents[0].delete(0, "end")
        ents[0].insert(0, "Run")
        self.btn("MD").invoke()
        ents[2].delete(0, "end")
        ents[2].insert(0, "4")
        self.btn("Add to list").invoke()
        tree = self.tree()
        self.assertEqual(tree.item("Run")["values"][1], "MD4")
        self.assertEqual(ents[2].get(), "4")
        tree.selection_set("Run")
        self.assertTrue(tree.bind("<Return>") and tree.bind("<Double-1>"))      # both the keyboard and the double-click load the row
        tree.load_selected()                                  # what they call: loads the selected row into the form (a generated key event is unreliable on Windows)
        self.win.update()
        self.btn("M").invoke()                                # now a bit in M
        ents[2].delete(0, "end")
        ents[2].insert(0, "0")
        ents[3].delete(0, "end")
        ents[3].insert(0, "1")
        self.btn("Update selected").invoke()
        self.assertEqual(tree.get_children(), ("Run",))
        self.assertEqual(tree.item("Run")["values"][1], "M0.1")
