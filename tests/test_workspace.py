import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import workspace as ws  # noqa: E402

KNOWN = {"s7comm", "modbus_tcp", "mc_protocol_3e"}


class Conn:
    def __init__(self, name, protocol="s7comm", interface=None, tags=None, plc_type=None):
        self.name, self.protocol, self.interface, self.tags, self.plc_type = name, protocol, interface or {}, tags or [], plc_type


class Roundtrip(unittest.TestCase):
    def test_connections_tags_and_settings_come_back(self):
        c = Conn("bench", interface={"ip": "192.0.2.5", "rack": 0, "slot": 1}, plc_type="S7_1200",
                 tags=[{"name": "Run", "area": "M", "start_byte": 0, "bit_offset": 1, "data_type": "bit"}])
        c.enabled, c.poll_interval_s = False, 0.5
        data = ws.snapshot([c], app_version="9.9")
        back, warnings = ws.restore(json.loads(json.dumps(data)), KNOWN)
        self.assertEqual(warnings, [])
        self.assertEqual(back[0]["name"], "bench")
        self.assertEqual((back[0]["enabled"], back[0]["poll_interval_s"], back[0]["plc_type"]), (False, 0.5, "S7_1200"))
        self.assertEqual(back[0]["tags"][0]["bit_offset"], 1)

    def test_secrets_are_not_written(self):
        c = Conn("x", protocol="modbus_tcp", interface={"ip": "192.0.2.5", "password": "hunter2", "api_key": "k", "port": 502})
        text = json.dumps(ws.snapshot([c]))
        self.assertNotIn("hunter2", text)
        self.assertNotIn("api_key", text)
        self.assertIn("502", text)

    def test_private_fields_of_a_tag_are_not_written(self):
        c = Conn("x", tags=[{"name": "a", "_address": "M0.0", "data_type": "bit"}])
        self.assertNotIn("_address", json.dumps(ws.snapshot([c])))


class Restore(unittest.TestCase):
    def test_a_newer_schema_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            ws.restore({"schema": ws.SCHEMA + 1, "connections": []}, KNOWN)
        self.assertIn("newer ProtoProbe", str(ctx.exception))

    def test_not_a_workspace(self):
        with self.assertRaises(ValueError):
            ws.restore({"hello": 1}, KNOWN)

    def test_bad_entries_are_left_out_with_a_warning(self):
        data = {"schema": 1, "connections": [
            {"name": "ok", "protocol": "s7comm", "tags": [{"name": "a"}, {"name": "a"}, {"nope": 1}], "poll_interval_s": "fast"},
            {"name": "ok", "protocol": "s7comm"},
            {"name": "weird", "protocol": "telepathy"},
            {"protocol": "s7comm"},
        ]}
        back, warnings = ws.restore(data, KNOWN)
        self.assertEqual([c["name"] for c in back], ["ok"])
        self.assertEqual(len(back[0]["tags"]), 1)
        self.assertEqual(back[0]["poll_interval_s"], ws.POLL_DEFAULT_S)
        self.assertGreaterEqual(len(warnings), 3)

    def test_poll_interval_is_clamped(self):
        back, _ = ws.restore({"schema": 1, "connections": [{"name": "a", "protocol": "s7comm", "poll_interval_s": 0}]}, KNOWN)
        self.assertEqual(back[0]["poll_interval_s"], ws.POLL_MIN_S)


class Files(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sub", "workspace.json")

    def test_save_load_and_keep_a_backup(self):
        ws.save_atomic(self.path, {"schema": 1, "connections": [], "n": 1})
        ws.save_atomic(self.path, {"schema": 1, "connections": [], "n": 2})
        self.assertEqual(ws.load(self.path)[0]["n"], 2)
        self.assertEqual(json.load(open(self.path + ".bak"))["n"], 1)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(ws.load(self.path), (None, None))

    def test_a_damaged_file_is_moved_aside_not_deleted(self):
        os.makedirs(os.path.dirname(self.path))
        open(self.path, "w").write("{ this is not json")
        data, message = ws.load(self.path)
        self.assertIsNone(data)
        self.assertIn("started empty", message)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue([f for f in os.listdir(os.path.dirname(self.path)) if ".corrupt-" in f])

    def test_default_path(self):
        self.assertEqual(ws.default_path({"PROTOPROBE_HOME": "/x"}), os.path.join("/x", "workspace.json"))
        self.assertEqual(ws.default_path({"APPDATA": "C:/Users/u/AppData/Roaming"}, platform="nt"),
                         os.path.join("C:/Users/u/AppData/Roaming", "ProtoProbe", "workspace.json"))


if __name__ == "__main__":
    unittest.main()


class AppRoundtrip(unittest.TestCase):
    """The app side: a workspace written by one session is loaded by the next; opt-in, so a plain App() never touches a profile."""
    def setUp(self):
        import tkinter as tk
        sys.path[:0] = [os.path.join(os.path.dirname(__file__), ".."), os.path.join(os.path.dirname(__file__), "..", "edge-gateway")]
        import protoprobe as pp
        self.pp, self.tk = pp, tk
        self._saved_root = tk._default_root
        tk._default_root = None
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "workspace.json")
        self._warn = pp.messagebox.showwarning
        self.warned = []
        pp.messagebox.showwarning = lambda *a, **k: self.warned.append(a)
        try:
            self.app = pp.App()
        except tk.TclError as exc:
            tk._default_root = self._saved_root
            self.skipTest(f"no display: {exc}")

    def tearDown(self):
        self.pp.messagebox.showwarning = self._warn
        self.app.destroy()
        self.tk._default_root = self._saved_root

    def test_plain_app_does_not_write_anything(self):
        self.app._save_workspace_now()
        self.assertFalse(os.path.exists(self.path))

    def test_changes_are_saved_and_the_next_session_loads_them(self):
        self.app.enable_workspace(self.path)
        conn = self.pp.Connection("bench", "s7comm", {"ip": "192.0.2.5", "rack": 0, "slot": 1}, "S7_1200", True, 0.5)
        conn.tags = [{"name": "Run", "area": "M", "start_byte": 0, "bit_offset": 1, "data_type": "bit"}]
        self.app.connections["bench"] = conn
        self.assertTrue(self.app._save_workspace_now())
        self.assertFalse(self.app._save_workspace_now())            # nothing changed: no second write
        data = json.load(open(self.path))
        self.assertEqual(data["connections"][0]["poll_interval_s"], 0.5)
        self.app.connections.clear()
        self.app._workspace_last = None
        self.app.enable_workspace(self.path)                          # "next session"
        loaded = self.app.connections["bench"]
        self.assertEqual((loaded.plc_type, loaded.poll_interval_s, loaded.tags[0]["bit_offset"]), ("S7_1200", 0.5, 1))

    def test_a_newer_file_is_never_overwritten(self):
        json.dump({"schema": 99, "connections": []}, open(self.path, "w"))
        self.app.enable_workspace(self.path)
        self.assertTrue(self.warned)
        self.app.connections["x"] = self.pp.Connection("x", "s7comm", {})
        self.assertFalse(self.app._save_workspace_now())
        self.assertEqual(json.load(open(self.path))["schema"], 99)

    def test_a_disabled_connection_is_not_started(self):
        conn = self.pp.Connection("off", "s7comm", {"ip": "192.0.2.5", "rack": 0, "slot": 1}, None, False, 2.0)
        conn.tags = [{"name": "a", "area": "M", "start_byte": 0, "data_type": "int16"}]
        self.app.connections["off"] = conn
        self.pp.messagebox.showinfo = lambda *a, **k: None
        self.app.screens["connections"].start_all()
        self.assertIsNone(conn.thread)
