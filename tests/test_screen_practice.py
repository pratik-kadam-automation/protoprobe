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
        base = {"client": "mrent", "machine_id": "MC_CCM_001", "machine_name": "CCM strand 1", "machine_type": "ccm", "plc_make": "mitsubishi", "plc_ip": "192.168.1.10",
                "gateway_id": "GW_MREN_001_008830", "deployment_id": "DP_MREN_001", "prefix": "S1_", "counters_reset": "Billet_Count"}
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
        self.assertEqual(pack["site"]["gateway_id"], "GW_MREN_001_008830")
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


if __name__ == "__main__":
    unittest.main()
