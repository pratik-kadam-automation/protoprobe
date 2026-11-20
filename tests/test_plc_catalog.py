"""The PLC catalog (plc_catalog.py): the gateway's protocol list, with the address types seen on its dialog, and what ProtoProbe can read.
No screens and no network: these tests run anywhere."""
import ast
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import plc_catalog as pc                            # noqa: E402
from drivers.mc_driver import DEVICE_CODES          # noqa: E402


def protoprobe_protocol_ids():
    """The protocol ids of the app, read from the source (importing protoprobe.py would need Tk)."""
    tree = ast.parse(open(os.path.join(HERE, "..", "protoprobe.py"), encoding="utf-8").read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PROTOCOL_LABELS" for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError("PROTOCOL_LABELS not found")


class Catalog(unittest.TestCase):
    def test_every_gateway_protocol_is_listed_once_in_the_gateways_order(self):
        self.assertEqual([p.key for p in pc.CATALOG], list(pc.GATEWAY_PROTOCOLS))
        self.assertEqual(len(pc.GATEWAY_PROTOCOLS), 29)
        self.assertEqual(len(set(pc.GATEWAY_PROTOCOLS)), 29)

    def test_areas_are_well_formed(self):
        for p in pc.CATALOG:
            flags = [a.flag for a in p.areas]
            self.assertEqual(len(flags), len(set(flags)), f"{p.key}: duplicate flag")
            for a in p.areas:
                self.assertIn(a.width, pc.WIDTHS, f"{p.key} {a.flag}")
                if a.readable:
                    self.assertEqual(len(a.pp), 2, f"{p.key} {a.flag}: a readable area says how a ProtoProbe tag names it")

    def test_what_is_called_readable_really_exists_in_the_drivers(self):
        ids = protoprobe_protocol_ids()
        for p in pc.connectable():
            self.assertIn(p.driver, ids, p.key)
        for p in pc.CATALOG:
            for a in pc.readable_areas(p.key):
                field_name, value = a.pp
                if p.driver in ("modbus_tcp", "modbus_rtu"):
                    self.assertEqual(field_name, "register_type"); self.assertIn(value, ("holding", "input", "coil"))
                elif p.driver == "s7comm":
                    self.assertEqual(field_name, "area"); self.assertIn(value, ("DB", "I", "Q", "M"))
                elif p.driver == "mc_protocol_3e":
                    self.assertEqual(field_name, "device_code"); self.assertIn(value, DEVICE_CODES)

    def test_a_type_without_a_driver_has_no_readable_area(self):
        for p in pc.CATALOG:
            if p.driver is None:
                self.assertEqual(pc.readable_areas(p.key), [], p.key)

    def test_connectable_today(self):
        self.assertEqual([p.key for p in pc.connectable()],
                         ["Modbus_TCP", "Modbus_RTU", "FX5U_TCP", "Q_TCP", "S7_1200", "S7_1500", "S7-300", "S7-400"])

    def test_modbus_lists_match_the_gateway_dialog_and_discrete_inputs_are_not_readable_yet(self):
        flags = {a.flag: a for a in pc.get("Modbus_TCP").areas}
        self.assertEqual(list(flags), ["0", "1", "3", "4"])
        self.assertFalse(flags["1"].readable)
        self.assertEqual(flags["4"].pp, ("register_type", "holding"))
        self.assertEqual([a.flag for a in pc.get("Modbus_ASC").areas], ["0", "1", "3", "4"])

    def test_the_seen_serial_lists(self):
        self.assertEqual([a.flag for a in pc.get("DVP_RTU").areas], ["X", "Y", "M", "S", "T", "TV", "C", "CV", "D"])
        fatek = {a.flag: a for a in pc.get("FATEK_COM").areas}
        self.assertEqual((fatek["X"].width, fatek["WX"].width, fatek["DWX"].width), ("bit", "word", "dword"))
        self.assertNotIn("TMR", fatek)                                        # a timer present value has no bit form
        self.assertEqual(pc.get("FX1S_COM").areas[0].flag, "X")
        self.assertIn("CV200", [a.flag for a in pc.get("FX1S_COM").areas])
        self.assertFalse(pc.get("FX2N(1N)_COM").complete)

    def test_address_preview_as_the_gateway_shows_it(self):
        self.assertEqual(pc.address_preview("Modbus_TCP", "0", 1), "00001(bit)")           # seen on the gateway
        self.assertEqual(pc.address_preview("Modbus_RTU", "4", 12), "40012(word)")
        self.assertEqual(pc.address_preview("DVP_RTU", "X", 0), "X0(bit)")                 # seen on the gateway
        self.assertEqual(pc.address_preview("FATEK_COM", "WX", 5), "WX5(word)")
        with self.assertRaises(KeyError):
            pc.address_preview("Modbus_TCP", "9", 1)

    def test_modbus_numbering_assumption_is_explicit(self):
        self.assertEqual(pc.modbus_zero_based(1), 0)
        self.assertEqual(pc.modbus_zero_based("100"), 99)
        with self.assertRaises(ValueError):
            pc.modbus_zero_based(0)

    def test_s7_defaults_and_the_optimized_block_warning(self):
        self.assertEqual(pc.get("S7_1200").interface, {"rack": "0", "slot": "1"})
        self.assertEqual(pc.get("S7-300").interface["slot"], "2")
        self.assertTrue(any("Optimized" in n for n in pc.get("S7_1500").notes))


if __name__ == "__main__":
    unittest.main()


class DropdownHelpers(unittest.TestCase):
    def test_every_gateway_protocol_is_in_the_dropdown_in_order(self):
        import plc_catalog as pc
        self.assertEqual([k for _t, k in pc.dropdown_entries()], list(pc.GATEWAY_PROTOCOLS))

    def test_unsupported_types_say_so(self):
        import plc_catalog as pc
        texts = {k: t for t, k in pc.dropdown_entries()}
        self.assertIn("[not supported yet]", texts["FINS_TCP"])
        self.assertNotIn("[not supported yet]", texts["S7_1200"])

    def test_describe_connectable_and_not(self):
        import plc_catalog as pc
        self.assertIn("ProtoProbe can connect", pc.describe("Q_TCP"))
        self.assertIn("cannot connect to this type yet", pc.describe("FINS_TCP"))
        self.assertIn("PUT/GET", pc.describe("S7_1200"))
        for p in pc.CATALOG:                       # never raises, whatever the type
            self.assertTrue(pc.describe(p.key))


class ScanPresets(unittest.TestCase):
    def test_every_connectable_type_has_presets_and_the_others_none(self):
        import plc_catalog as pc
        for p in pc.CATALOG:
            self.assertEqual(bool(pc.scan_presets(p.key)), bool(p.driver), p.key)

    def test_s7_presets_use_the_scan_windows_field_names(self):
        import plc_catalog as pc
        for preset in pc.scan_presets("S7_1200"):
            self.assertTrue(set(preset.single) <= {"area", "db_number", "start", "end"}, preset.label)
            if preset.single:
                self.assertLessEqual(preset.single["start"], preset.single["end"])
        full = [p for p in pc.scan_presets("S7_1200") if p.full][0]
        self.assertEqual(set(full.full["include"]), {"I", "Q", "M"})

    def test_default_type_per_protocol(self):
        import plc_catalog as pc
        for protocol in ("s7comm", "mc_protocol_3e", "modbus_tcp", "modbus_rtu"):
            self.assertTrue(pc.scan_presets(pc.default_type(protocol)), protocol)
        self.assertIsNone(pc.default_type("opcua"))
