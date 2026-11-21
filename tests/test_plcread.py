"""The command line tool against a simulated Mitsubishi PLC over UDP, and a fake Siemens reader."""
import io
import json
import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import plcread
from test_slmp import SimUdp


def cdab(v):
    b = struct.pack(">f", v)
    return (b[2] << 8) | b[3], (b[0] << 8) | b[1]


TAGS = ("Process_SP\tFLOAT [Single Precision]\tVAR_GLOBAL\tD612\n"
        "Batch_Counter\tWord [Unsigned]\tVAR_GLOBAL\tD620\n"
        "Bad_Float\tFLOAT [Single Precision]\tVAR_GLOBAL\tD630\n")


def run(argv, factory=None):
    out = io.StringIO()
    code = plcread.main(argv, out, factory)
    return code, out.getvalue()


class PlcReadMitsubishi(unittest.TestCase):
    def setUp(self):
        mem = {}
        mem[612], mem[613] = cdab(1.25)
        mem[620] = 41
        mem[630], mem[631] = 0x0000, 0x7F00        # about 1.7e38: the wrong word order looks like this
        self.sim = SimUdp(mem); self.sim.start()
        self.dir = tempfile.mkdtemp()
        self.tags = os.path.join(self.dir, "labels.tsv")
        open(self.tags, "w").write(TAGS)

    def tearDown(self):
        self.sim.close()

    def base(self, *extra):
        return ["read", "--make", "mitsubishi", "--ip", "127.0.0.1", "--port", str(self.sim.port), "--tags", self.tags, "--timeout", "0.5", *extra]

    def test_reads_and_flags(self):
        code, text = run(self.base("--transport", "udp"))
        self.assertEqual(code, 2, text)
        self.assertRegex(text, r"Process_SP\s+D612\s+float32\s+1\.250")
        self.assertRegex(text, r"Batch_Counter\s+D620\s+uint16\s+41")
        self.assertRegex(text, r"Bad_Float.*SUSPECT")
        self.assertIn("3 tags read, 2 fine, 1 flagged", text)

    def test_auto_transport_finds_udp(self):
        code, text = run(self.base())
        self.assertIn("Using UDP port", text)
        self.assertEqual(code, 2)

    def test_json(self):
        code, text = run(self.base("--transport", "udp", "--json"))
        data = json.loads(text)
        by = {t["name"]: t for t in data["tags"]}
        self.assertAlmostEqual(by["Process_SP"]["value"], 1.25, places=3)
        self.assertEqual(by["Bad_Float"]["flags"], ["SUSPECT"])

    def test_flat_flag_needs_three_samples(self):
        code, text = run(self.base("--transport", "udp", "--samples", "3", "--interval", "0", "--csv"))
        self.assertIn("Batch_Counter,D620,uint16,41,FLAT", text)

    def test_no_answer_exit_1(self):
        self.sim.silent = True
        code, text = run(self.base("--transport", "udp"))
        self.assertEqual(code, 1)
        self.assertIn("Could not read the PLC", text)

    def test_probe(self):
        code, text = run(["probe", "127.0.0.1", "--ports", str(self.sim.port), "--timeout", "0.5"])
        self.assertEqual(code, 0, text)
        self.assertIn("UDP", text)

    def test_raw(self):
        code, text = run(["raw", "--make", "mitsubishi", "--ip", "127.0.0.1", "--port", str(self.sim.port), "--transport", "udp",
                          "--start", "D612", "--count", "2", "--timeout", "0.5"])
        self.assertEqual(code, 0, text)
        self.assertIn("CDAB=1.25", text)

    def test_bad_start(self):
        code, text = run(["raw", "--make", "mitsubishi", "--ip", "127.0.0.1", "--start", "nonsense"])
        self.assertEqual(code, 1)


class FakeReader:
    def __init__(self, protocol, interface, factory=None):
        self.protocol = protocol

    def read_region(self, region, stop=None):
        b = struct.pack(">f", 12.5)
        mem = {20: (b[0] << 8) | b[1], 22: (b[2] << 8) | b[3]}
        return {a: mem.get(a, 0) for a in range(region.start, region.end + 1, region.step)}


class FakeBitReader(FakeReader):
    reads = 0

    def read_bits(self, bit_tags):
        FakeBitReader.reads += 1
        # %I0.0 stays off, %Q0.5 is on, %M0.3 changes between the samples
        return {t["name"]: (1 if t["name"] == "Lamp" else (FakeBitReader.reads % 2 if t["name"] == "Flicker" else 0)) for t in bit_tags}


class PlcReadSiemensBits(unittest.TestCase):
    def test_bool_tags_are_read_as_bits(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tags.tsv")
        open(p, "w").write("Name\tPath\tData Type\tLogical Address\tComment\n"
                           "Tank_Level\tt\tReal\t%MD20\tLevel\n"
                           "Start on\tt\tBool\t%I0.0\t\n"
                           "Lamp\tt\tBool\t%Q0.5\t\n"
                           "Flicker\tt\tBool\t%M0.3\t\n"
                           "Odd\tt\tBool\t%MB4\t\n")
        orig = plcread.DriverReader
        plcread.DriverReader = FakeBitReader
        FakeBitReader.reads = 0
        try:
            code, text = run(["read", "--make", "siemens", "--ip", "10.0.0.5", "--tags", p, "--samples", "3", "--interval", "0"])
        finally:
            plcread.DriverReader = orig
        self.assertRegex(text, r"Start_on\s+I0\.0\s+0")
        self.assertRegex(text, r"Lamp\s+Q0\.5\s+1")
        self.assertRegex(text, r"Flicker\s+M0\.3\s+\S+\s+yes")
        self.assertIn("3 bit tags read", text)
        self.assertNotIn("skipped Start on", text)       # read now, no longer skipped
        self.assertIn("skipped Odd", text)               # a byte address stays skipped


class PlcReadSiemens(unittest.TestCase):
    def test_s7_text_list(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "tags.tsv")
        open(p, "w").write("Name\tPath\tData Type\tLogical Address\tComment\nTank_Level\tt\tReal\t%MD20\tLevel\n")
        orig = plcread.DriverReader
        plcread.DriverReader = FakeReader
        try:
            code, text = run(["read", "--make", "siemens", "--ip", "10.0.0.5", "--tags", p])
        finally:
            plcread.DriverReader = orig
        self.assertEqual(code, 0, text)
        self.assertRegex(text, r"Tank_Level\s+MD20\s+float32\s+12\.500")


if __name__ == "__main__":
    unittest.main()
