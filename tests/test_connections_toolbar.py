"""All the Connections buttons are on screen in a window of normal width (they used to run off the right edge)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import protoprobe as pp  # noqa: E402


def buttons(widget):
    out = []
    for child in widget.winfo_children():
        if isinstance(child, pp.ttk.Button):
            out.append(child)
        out.extend(buttons(child))
    return out


class Toolbar(unittest.TestCase):
    def test_capture_and_doctor_buttons_fit(self):
        app = pp.App()
        try:
            app.geometry("1000x700")
            app.show_screen("connections") if hasattr(app, "show_screen") else None
            app.update()
            screen = app.screens["connections"]
            wanted = {"Connection doctor", "Capture traffic", "Import tags from file..."}
            found = {b.cget("text"): b for b in buttons(screen) if b.cget("text") in wanted}
            self.assertEqual(set(found), wanted)
            for b in found.values():
                right = b.winfo_rootx() + b.winfo_width() - app.winfo_rootx()
                self.assertLessEqual(right, 1000)
        finally:
            app.destroy()
            __import__('gc').collect()    # finalise Tk variables on the main thread (see test_guided_s7_tag.collect_tk_garbage)


if __name__ == "__main__":
    unittest.main()
