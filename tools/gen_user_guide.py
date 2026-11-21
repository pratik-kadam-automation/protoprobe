"""Writes docs/USER-GUIDE.md from help_content.py. Run: python tools/gen_user_guide.py   (a test fails when the file is out of date)"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import help_content  # noqa: E402

if __name__ == "__main__":
    path = os.path.join(HERE, "..", "docs", "USER-GUIDE.md")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(help_content.user_guide_markdown())
    print("wrote", os.path.normpath(path))
