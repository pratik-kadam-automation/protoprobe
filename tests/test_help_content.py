"""The help text covers every screen and cannot drift from the app: a new sidebar tab without help, a field without a tooltip, or an
out-of-date docs/USER-GUIDE.md fails here."""
import os
import re
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import help_content as hc  # noqa: E402
import protoprobe as pp  # noqa: E402


class Coverage(unittest.TestCase):
    def test_every_sidebar_screen_has_help_and_a_group(self):
        keys = [k for k, _ in pp.NAV_ITEMS]
        for k in keys:
            self.assertIn(k, hc.SCREENS, f"no help for the screen '{k}'")
            self.assertTrue(hc.group_of(k), f"'{k}' is in no sidebar group")
        grouped = [k for _, ks in hc.GROUPS for k in ks]
        self.assertEqual(sorted(grouped), sorted(keys))
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_every_entry_is_complete(self):
        for k, s in hc.SCREENS.items():
            for field in ("title", "summary", "use_when", "skip"):
                self.assertTrue(s[field].strip(), f"{k}.{field} is empty")
            self.assertTrue(s["steps"], f"{k} has no steps")

    def test_every_connection_field_has_a_tooltip(self):
        for fields in pp.INTERFACE_FIELDS.values():
            for name, _ in fields:
                self.assertIn(name, hc.FIELD_HELP, f"no tooltip text for the field '{name}'")

    def test_troubleshooting_entries_have_causes_and_actions(self):
        self.assertGreaterEqual(len(hc.TROUBLESHOOTING), 8)
        for symptom, causes, todo in hc.TROUBLESHOOTING:
            self.assertTrue(symptom and causes and todo)

    def test_public_repo_rule_no_real_addresses_or_names(self):
        text = hc.user_guide_markdown()
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            self.assertIn(ip, ("192.168.1.50", "192.168.1.70", "0.0.0.0", "255.255.255.0"), f"unexpected address {ip} in the help text")

    def test_tour_has_a_card_per_screen(self):
        cards = hc.tour_cards()
        self.assertEqual(len(cards), len(pp.NAV_ITEMS) + 2)
        self.assertIn("READS", cards[0][1])

    def test_user_guide_file_is_current(self):
        path = os.path.join(HERE, "..", "docs", "USER-GUIDE.md")
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), hc.user_guide_markdown(), "run: python tools/gen_user_guide.py")


if __name__ == "__main__":
    unittest.main()
