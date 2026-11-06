"""Drives the wizard screen end to end in practice mode (simulated PLC). Needs tkinter and a display (xvfb-run -a)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
except Exception as e:      # no tkinter / no display: the engine tests still run everywhere
    tk = None
    SKIP = str(e)

import commissioning as cm


@unittest.skipIf(tk is None, "tkinter or display not available")
class WizardFlow(unittest.TestCase):
    def setUp(self):
        from commissioning_screen import CommissioningScreen
        self.s = CommissioningScreen(root, app=None)
        self.s.pack(fill="both", expand=True)
        self.s.sync = True
        root.geometry("1100x800")
        root.update()

    def answer(self, **kw):
        base = {"client": "acme", "machine_id": "MC_CCM_001", "machine_name": "CCM strand 1", "machine_type": "ccm", "plc_make": "mitsubishi", "plc_ip": "192.168.1.10",
                "gateway_id": "GW_ACME_001_000001", "deployment_id": "DP_ACME_001", "prefix": "S1_", "counters_reset": "Billet_Count"}
        base.update(kw)
        for k, v in base.items():
            self.s.answer_vars[k].set(v)

    def type_and_record(self):
        for key, text in self.s.practice_next_values().items():
            if key in self.s.hmi_vars:
                self.s.hmi_vars[key].set(text)
        self.s.record_reading()
        root.update()

    def test_the_whole_flow_proves_every_tag(self):
        self.answer()
        self.s.practice_var.set(True)
        self.s._practice_toggled()
        self.s._questions_done()
        self.assertEqual(self.s.q_msg.cget("text"), "")
        self.assertTrue(self.s.regions)
        self.type_and_record()                       # find 1
        self.type_and_record()                       # find 2
        self.assertEqual([m.phase for m in self.s.moments], ["find", "find"])
        self.s.find_tags()
        root.update()
        self.assertEqual(set(self.s.chosen), {"run", "speed", "mould", "tundish", "billets"})
        self.type_and_record()                       # verify 1
        self.type_and_record()                       # verify 2
        self.assertEqual([m.phase for m in self.s.moments], ["find", "find", "verify", "verify"])
        tags = self.s.build_tags()
        root.update()
        by = {t["name"]: t for t in tags}
        # the machine's real addresses, found without being told
        self.assertEqual(by["Casting_Speed"]["address"], "D100"); self.assertEqual(by["Casting_Speed"]["byte_order"], "CDAB")
        self.assertEqual((by["Mould_Level"]["address"], by["Mould_Level"]["scale"]), ("D102", 0.1))
        self.assertEqual(by["Billet_Count"]["address"], "D200")
        self.assertEqual(by["Tundish_Temp"]["address"], "D300")
        self.assertEqual(by["Machine_Run"]["address"], "D50.3")
        self.assertEqual({t["confidence"] for t in tags}, {"confirmed"}, {t["name"]: t["confidence"] for t in tags})
        pack = self.s.pack_dict()
        self.assertEqual(cm.validate_pack(pack), [])
        self.assertEqual(pack["machines"][0]["plc"]["prefix"], "S1_")
        self.assertEqual(pack["machines"][0]["batch"], {"resetTags": ["Billet_Count"]})
        self.assertEqual(pack["site"]["gateway_id"], "GW_ACME_001_000001")
        out = os.path.join(os.path.dirname(__file__), "..", "_practice_pack.json")
        # saving works without a dialog when a path is given (messagebox is patched out)
        import commissioning_screen as cs
        cs.messagebox.showinfo = lambda *a, **k: None
        self.assertEqual(self.s.save(out), out)
        os.remove(out)
        root.update()
        self.s.nb.select(self.s.t_v); root.update()
        if os.environ.get("SHOT"):
            os.system(f"import -window root {os.environ['SHOT']} 2>/dev/null")

    def test_missing_answers_block_the_next_step(self):
        self.answer(client="", machine_id="")
        self.s._questions_done()
        self.assertIn("Still needed", self.s.q_msg.cget("text"))
        self.assertFalse(self.s.moments)

    def test_verify_before_find_is_refused_and_a_bad_number_is_refused(self):
        self.answer()
        self.s._questions_done()
        import commissioning_screen as cs
        shown = []
        cs.messagebox.showerror = lambda *a, **k: shown.append(a)
        cs.messagebox.showinfo = lambda *a, **k: shown.append(a)
        self.s.hmi_vars["speed"].set("fast")
        self.s.record_reading()
        self.assertTrue(shown and "not a number" in shown[0][0].lower() + shown[0][1].lower())
        self.assertEqual(self.s.moments, [])

    def test_a_signal_the_hmi_does_not_show_is_left_out(self):
        self.answer()
        self.s.practice_var.set(True); self.s._practice_toggled(); self.s._questions_done()
        for _ in range(2):
            for key, text in self.s.practice_next_values().items():
                if key != "tundish":                     # this HMI has no tundish temperature
                    self.s.hmi_vars[key].set(text)
            self.s.record_reading()
        self.s.find_tags()
        self.assertNotIn("tundish", self.s.chosen)


    def test_importing_the_programmers_list_verifies_it_and_catches_a_missing_scale(self):
        # the practice PLC holds: speed float at D100, mould level int16 x0.1 at D102, counter at D200, temperature at D300.
        # The "programmer's list" is right about every address but forgot the 0.1 scale of the mould level.
        listing = ("Casting_Speed\tFLOAT [Single Precision]\tVAR_GLOBAL\tD100\n"
                   "Mould_Level\tWord [Signed]\tVAR_GLOBAL\tD102\n"
                   "Billet_Count\tWord [Unsigned]\tVAR_GLOBAL\tD200\n"
                   "Tundish_Temp\tWord [Signed]\tVAR_GLOBAL\tD300\n"
                   "Wrong_Address_Flow\tFLOAT [Single Precision]\tVAR_GLOBAL\tD400\n")
        self.answer()
        self.s.practice_var.set(True); self.s._practice_toggled(); self.s._questions_done()
        self.s.load_tag_list(listing, "CDAB")
        self.assertEqual(self.s.mode, "verify")
        self.assertEqual(set(self.s.chosen), {"Casting_Speed", "Mould_Level", "Billet_Count", "Tundish_Temp", "Wrong_Address_Flow"})
        shown = {"Casting_Speed": "speed", "Mould_Level": "mould", "Billet_Count": "billets", "Tundish_Temp": "tundish"}
        for _ in range(3):                                   # three readings of the simulated HMI, values moving each time
            nxt = self.s.practice_next_values()
            for name, key in shown.items():
                self.s.hmi_vars[name].set(nxt[key])
            self.s.hmi_vars["Wrong_Address_Flow"].set("250")   # the HMI shows 250 here, the PLC does not hold it at D400
            self.s.record_reading(); root.update()
        self.assertEqual({m.phase for m in self.s.moments}, {"verify"}, "an imported address is proved by every reading")
        tags = {t["name"]: t for t in self.s.build_tags()}
        self.assertEqual(tags["Casting_Speed"]["confidence"], "confirmed")
        self.assertEqual(tags["Billet_Count"]["confidence"], "confirmed")
        self.assertEqual(tags["Tundish_Temp"]["confidence"], "confirmed")
        self.assertEqual(tags["Mould_Level"]["confidence"], "unverified", "right address, missing scale: the HMI shows 3.2, the PLC holds 32")
        self.assertEqual(tags["Wrong_Address_Flow"]["confidence"], "unverified", "a wrong address is caught")
        changed = self.s.apply_suggested_scales()
        self.assertEqual(changed, ["Mould_Level x0.1"])
        tags = {t["name"]: t for t in self.s.build_tags()}
        self.assertEqual((tags["Mould_Level"]["scale"], tags["Mould_Level"]["confidence"]), (0.1, "confirmed"))
        self.assertEqual(tags["Wrong_Address_Flow"]["confidence"], "unverified", "no scale explains a wrong address")
        pack = self.s.pack_dict()
        self.assertEqual(pack["machines"][0]["plc"]["transport"], "udp")
        self.assertEqual(cm.validate_pack(pack), [])

    def test_connection_test_fills_in_the_transport_and_port_it_found(self):
        self.answer(plc_ip="127.0.0.1")
        self.s.practice_var.set(False)
        import slmp_probe
        orig = slmp_probe.probe
        slmp_probe.probe = lambda ip, **k: [
            {"transport": "udp", "port": 5005, "status": "ok", "end_code": 0, "text": "", "reply_hex": ""},
            {"transport": "udp", "port": 5006, "status": "ok", "end_code": 0, "text": "", "reply_hex": ""},
            {"transport": "tcp", "port": 5006, "status": "timeout", "end_code": None, "text": "", "reply_hex": ""}]
        try:
            self.s.test_connection()
            root.update()
        finally:
            slmp_probe.probe = orig
        self.assertEqual((self.s.answer_vars["slmp_transport"].get(), self.s.answer_vars["plc_port"].get()), ("udp", "5005"))
        self.assertIn("UDP", self.s.test_msg.cget("text"))


if __name__ == "__main__":
    unittest.main()
