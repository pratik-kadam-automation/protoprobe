"""ui_style.py -- the few shared visual rules (colours for state, fonts, one definition of the danger button) so every screen looks the same.

State is never shown by colour alone: use `state_text` so a colour-blind person or a black-and-white printout still reads it.
"""
from tkinter import ttk

FONT = "Segoe UI"
TITLE = (FONT, 16)
HEADING = (FONT, 11, "bold")
MUTED = "gray"

STATE_COLOURS = {"ok": "#2e9d5b", "warn": "#d98e04", "fail": "#d6453d", "idle": "#8a8f98"}
STATE_WORDS = {"ok": "OK", "warn": "Check", "fail": "Fail", "idle": "Idle"}


def state_text(state):
    return STATE_WORDS.get(state, STATE_WORDS["idle"])


def configure(style=None):
    """Create the extra named styles. Safe to call again after the theme changes."""
    style = style or ttk.Style()
    style.configure("Danger.TButton", foreground=STATE_COLOURS["fail"])
    style.configure("Group.TLabel", foreground=MUTED, font=(FONT, 8, "bold"))
    return style
