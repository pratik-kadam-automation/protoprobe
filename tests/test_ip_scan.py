"""IP scanner (Network tools). The first real scan on Windows opened a black ping.exe window for each of the 254 addresses, many at once,
and they took the focus so the app could not be used. Every ping must now start hidden."""
import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "edge-gateway"))
import protoprobe                                   # noqa: E402  (imports tkinter; needs no display)

CREATE_NO_WINDOW = 0x08000000


class Result:
    def __init__(self, code):
        self.returncode = code


class IpScan(unittest.TestCase):
    def test_on_windows_a_child_process_starts_without_a_window(self):
        kw = protoprobe.hidden_subprocess_kwargs("win32")
        self.assertEqual(kw["creationflags"] & CREATE_NO_WINDOW, CREATE_NO_WINDOW)

    def test_elsewhere_nothing_special_is_added(self):
        self.assertEqual(protoprobe.hidden_subprocess_kwargs("linux"), {})
        self.assertEqual(protoprobe.hidden_subprocess_kwargs("darwin"), {})

    def test_every_ping_is_started_hidden_with_the_windows_command(self):
        seen = []

        def runner(cmd, **kw):
            seen.append((cmd, kw))
            return Result(0)

        self.assertTrue(protoprobe.ping_once("192.0.2.7", platform="win32", runner=runner))
        cmd, kw = seen[0]
        self.assertEqual(cmd, ["ping", "-n", "1", "-w", "500", "192.0.2.7"])
        self.assertEqual(kw["creationflags"] & CREATE_NO_WINDOW, CREATE_NO_WINDOW)
        self.assertTrue(kw["capture_output"])
        self.assertEqual(kw["timeout"], 2)

    def test_the_linux_command_and_no_window_flags(self):
        seen = []
        protoprobe.ping_once("192.0.2.7", platform="linux", runner=lambda cmd, **kw: seen.append((cmd, kw)) or Result(0))
        cmd, kw = seen[0]
        self.assertEqual(cmd, ["ping", "-c", "1", "-W", "1", "192.0.2.7"])
        self.assertNotIn("creationflags", kw)

    def test_no_answer_and_errors_mean_not_reachable(self):
        self.assertFalse(protoprobe.ping_once("192.0.2.8", platform="win32", runner=lambda cmd, **kw: Result(1)))

        def broken(cmd, **kw):
            raise OSError("no ping program")

        self.assertFalse(protoprobe.ping_once("192.0.2.9", platform="win32", runner=broken))


if __name__ == "__main__":
    unittest.main()
