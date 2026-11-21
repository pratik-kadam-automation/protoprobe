"""themes.py -- the colour schemes of ProtoProbe and the code that applies one to every widget.

Why a scheme engine of our own: the Modern theme library (sv-ttk) draws its buttons from fixed blue images, so its accent colour cannot be changed.
The industrial schemes below are built on Tk's built-in flat `clam` theme and styled completely from a few colours, so every scheme looks the same
everywhere and a new one is just a new dictionary.

Every colour pair that carries text is checked by tests/test_themes.py (WCAG contrast): a scheme with unreadable text cannot be added.
State is shown by colour AND by words elsewhere in the app (ui_style.state_text), so a colour-blind person still reads it.
"""
from tkinter import ttk

DEFAULT_KEY = "industrial_dark"

_COMMON = ("label", "engine", "dark", "bg", "panel", "field", "fg", "muted", "border", "accent", "accent_fg", "select_bg", "select_fg",
           "ok", "warn", "fail", "text_bg", "text_fg")

PALETTES = {
    "industrial_dark": dict(
        label="Industrial dark (graphite and amber)", engine="clam", dark=True,
        bg="#1b1f24", panel="#272d34", field="#14181c", fg="#e6e9ec", muted="#9ba6b1", border="#3b444e",
        accent="#f2a900", accent_fg="#15181b", select_bg="#5a4608", select_fg="#ffffff",
        ok="#3fb868", warn="#f2a900", fail="#ff6b5e", text_bg="#14181c", text_fg="#e6e9ec"),
    "industrial_light": dict(
        label="Industrial light (steel and amber)", engine="clam", dark=False,
        bg="#e9edf0", panel="#f7f9fa", field="#ffffff", fg="#1b2630", muted="#55626f", border="#b4bfc9",
        accent="#a85a00", accent_fg="#ffffff", select_bg="#ffd9a8", select_fg="#1b2630",
        ok="#1f7a3d", warn="#9a5b00", fail="#c0332b", text_bg="#ffffff", text_fg="#1b2630"),
    "control_room": dict(
        label="Control room (navy and cyan)", engine="clam", dark=True,
        bg="#0d1620", panel="#15212d", field="#09111a", fg="#e3edf5", muted="#8fa5b8", border="#2b3d4f",
        accent="#29b6f6", accent_fg="#06121b", select_bg="#0f4a66", select_fg="#ffffff",
        ok="#34c77b", warn="#ffb300", fail="#ff6f61", text_bg="#09111a", text_fg="#e3edf5"),
    "steel_blue": dict(
        label="Steel blue (slate and blue)", engine="clam", dark=True,
        bg="#232b34", panel="#2d3742", field="#1a2027", fg="#e8edf2", muted="#a3b0bd", border="#44505d",
        accent="#5aa9f0", accent_fg="#0d1824", select_bg="#2a5b8c", select_fg="#ffffff",
        ok="#4cc47f", warn="#f0b429", fail="#ff7a70", text_bg="#1a2027", text_fg="#e8edf2"),
    "plant_green": dict(
        label="Plant green (dark green)", engine="clam", dark=True,
        bg="#171f1a", panel="#202a23", field="#0f1511", fg="#e4ece6", muted="#9bb0a1", border="#354639",
        accent="#46c06f", accent_fg="#07130b", select_bg="#1f5a35", select_fg="#ffffff",
        ok="#46c06f", warn="#e8b923", fail="#ff7a6b", text_bg="#0f1511", text_fg="#e4ece6"),
    "high_contrast": dict(
        label="High contrast (black, white and yellow)", engine="clam", dark=True,
        bg="#000000", panel="#141414", field="#000000", fg="#ffffff", muted="#d6d6d6", border="#ffffff",
        accent="#ffd400", accent_fg="#000000", select_bg="#ffd400", select_fg="#000000",
        ok="#3dff7a", warn="#ffd400", fail="#ff5c5c", text_bg="#000000", text_fg="#ffffff"),
    # the Windows 11 look from the sv-ttk package (only offered when that package is installed); its accent is fixed blue
    "modern_dark": dict(label="Modern dark (Windows 11 style)", engine="sv", dark=True,
                        bg="#1c1c1c", panel="#2b2b2b", field="#1c1c1c", fg="#fafafa", muted="#a0a0a0", border="#454545",
                        accent="#57c8ff", accent_fg="#000000", select_bg="#0f4a66", select_fg="#ffffff",
                        ok="#3fb868", warn="#e0a100", fail="#ff6b5e", text_bg="#1e1e1e", text_fg="#e6e6e6"),
    "modern_light": dict(label="Modern light (Windows 11 style)", engine="sv", dark=False,
                         bg="#fafafa", panel="#f3f3f3", field="#ffffff", fg="#1c1c1c", muted="#5f5f5f", border="#c8c8c8",
                         accent="#005fb8", accent_fg="#ffffff", select_bg="#cce4f7", select_fg="#1c1c1c",
                         ok="#2e7d32", warn="#8a5a00", fail="#c62828", text_bg="#ffffff", text_fg="#000000"),
}

_current = {"key": DEFAULT_KEY}


def available(sv_available):
    """[(key, label)] in display order; the Modern schemes only when the sv-ttk package is installed."""
    return [(k, p["label"]) for k, p in PALETTES.items() if sv_available or p["engine"] != "sv"]


def get(key=None):
    return PALETTES.get(key or _current["key"], PALETTES[DEFAULT_KEY])


def colour(name):
    """The current scheme's colour for a role: 'ok', 'warn', 'fail', 'muted', 'fg', 'accent' ... (used instead of hard-coded hex values)."""
    return get()[name]


# ---- contrast (WCAG 2.x), used by the tests and by nothing at run time --------------------------------------------------------------

def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a, b, t):
    """a moved towards b by t (0..1), as a hex colour."""
    ha, hb = a.lstrip("#"), b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        x, y = int(ha[i:i + 2], 16), int(hb[i:i + 2], 16)
        out.append(round(x + (y - x) * t))
    return "#%02x%02x%02x" % tuple(out)


def hover_of(p, base):
    """A slightly lighter (dark schemes) or darker (light schemes) shade for the pointer-over state."""
    return mix(base, "#ffffff" if p["dark"] else "#000000", 0.10)


# ---- applying a scheme -----------------------------------------------------------------------------------------------------------------

def apply_clam(root, style, p):
    """Style every ttk widget class ProtoProbe uses, and the plain Tk widgets through the option database, from the scheme `p`."""
    style.theme_use("clam")
    bg, panel, field, fg, muted, border = p["bg"], p["panel"], p["field"], p["fg"], p["muted"], p["border"]
    accent, accent_fg = p["accent"], p["accent_fg"]
    hover, accent_hover = hover_of(p, panel), hover_of(p, accent)

    style.configure(".", background=bg, foreground=fg, fieldbackground=field, bordercolor=border, lightcolor=border, darkcolor=border,
                    troughcolor=field, selectbackground=p["select_bg"], selectforeground=p["select_fg"], insertcolor=fg,
                    focuscolor=accent, relief="flat")
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("TLabelframe", background=bg, bordercolor=border, relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=bg, foreground=muted, font=("Segoe UI", 9, "bold"))
    style.configure("TSeparator", background=border)
    style.configure("TSizegrip", background=bg)

    style.configure("TButton", background=panel, foreground=fg, bordercolor=border, lightcolor=panel, darkcolor=panel, padding=(10, 4),
                    relief="flat", borderwidth=1, focusthickness=1)
    style.map("TButton",
              background=[("disabled", bg), ("pressed", accent), ("active", hover)],
              foreground=[("disabled", muted), ("pressed", accent_fg)],
              bordercolor=[("focus", accent), ("pressed", accent)],
              lightcolor=[("pressed", accent), ("active", hover)], darkcolor=[("pressed", accent), ("active", hover)],
              relief=[("pressed", "flat")])
    style.configure("Accent.TButton", background=accent, foreground=accent_fg, bordercolor=accent, lightcolor=accent, darkcolor=accent,
                    padding=(12, 5), font=("Segoe UI", 9, "bold"))
    style.map("Accent.TButton", background=[("disabled", panel), ("active", accent_hover), ("pressed", accent_hover)],
              foreground=[("disabled", muted)], lightcolor=[("active", accent_hover)], darkcolor=[("active", accent_hover)])
    style.configure("Danger.TButton", foreground=p["fail"])

    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=bg, foreground=fg, indicatorbackground=field, indicatorforeground=accent_fg, indicatormargin=(0, 0, 6, 0))
        style.map(name, background=[("active", bg)], foreground=[("disabled", muted)],
                  indicatorbackground=[("selected", accent), ("pressed", accent), ("disabled", bg)],
                  indicatorforeground=[("selected", accent_fg)])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=field, background=panel, foreground=fg, insertcolor=fg, bordercolor=border,
                        lightcolor=border, darkcolor=border, arrowcolor=fg, padding=4)
        style.map(name, bordercolor=[("focus", accent)], lightcolor=[("focus", accent)], darkcolor=[("focus", accent)],
                  fieldbackground=[("readonly", field), ("disabled", bg)], foreground=[("disabled", muted)],
                  background=[("active", hover)])

    style.configure("Treeview", background=field, fieldbackground=field, foreground=fg, bordercolor=border, lightcolor=border, darkcolor=border,
                    rowheight=24, relief="flat")
    style.map("Treeview", background=[("selected", p["select_bg"])], foreground=[("selected", p["select_fg"])])
    style.configure("Treeview.Heading", background=panel, foreground=fg, bordercolor=border, lightcolor=panel, darkcolor=panel,
                    relief="flat", padding=(6, 5), font=("Segoe UI", 9, "bold"))
    style.map("Treeview.Heading", background=[("active", hover)])

    style.configure("TNotebook", background=bg, bordercolor=border, lightcolor=bg, darkcolor=bg, tabmargins=(2, 4, 2, 0))
    style.configure("TNotebook.Tab", background=panel, foreground=muted, bordercolor=border, lightcolor=panel, darkcolor=panel, padding=(14, 6))
    style.map("TNotebook.Tab", background=[("selected", bg), ("active", hover)], foreground=[("selected", fg)],
              lightcolor=[("selected", accent)], bordercolor=[("selected", accent)])

    for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
        style.configure(name, background=panel, troughcolor=field, bordercolor=border, lightcolor=panel, darkcolor=panel, arrowcolor=fg,
                        gripcount=0, relief="flat")
        style.map(name, background=[("active", hover), ("pressed", accent)])
    style.configure("Horizontal.TProgressbar", background=accent, troughcolor=field, bordercolor=border, lightcolor=accent, darkcolor=accent)
    style.configure("Vertical.TProgressbar", background=accent, troughcolor=field, bordercolor=border, lightcolor=accent, darkcolor=accent)
    style.configure("TPanedwindow", background=bg)

    # plain Tk widgets (Text, Listbox, Toplevel, Menu, the Combobox drop-down) take their colours from the option database
    root.configure(background=bg)
    for pattern, value in (("*Toplevel.background", bg), ("*Frame.background", bg),
                           ("*Text.background", p["text_bg"]), ("*Text.foreground", p["text_fg"]), ("*Text.insertBackground", p["text_fg"]),
                           ("*Text.selectBackground", p["select_bg"]), ("*Text.selectForeground", p["select_fg"]),
                           ("*Listbox.background", field), ("*Listbox.foreground", fg), ("*Listbox.selectBackground", p["select_bg"]),
                           ("*Listbox.selectForeground", p["select_fg"]), ("*Listbox.highlightThickness", 0), ("*Listbox.borderWidth", 0),
                           ("*TCombobox*Listbox.background", field), ("*TCombobox*Listbox.foreground", fg),
                           ("*TCombobox*Listbox.selectBackground", p["select_bg"]), ("*TCombobox*Listbox.selectForeground", p["select_fg"]),
                           ("*Menu.background", panel), ("*Menu.foreground", fg), ("*Menu.activeBackground", accent), ("*Menu.activeForeground", accent_fg)):
        root.option_add(pattern, value)


def apply(root, key, sv_ttk=None):
    """Apply the scheme `key`; returns the scheme dict actually used (an unknown or unavailable key falls back to the default)."""
    p = PALETTES.get(key)
    if p is None or (p["engine"] == "sv" and sv_ttk is None):
        key, p = DEFAULT_KEY, PALETTES[DEFAULT_KEY]
    style = ttk.Style(root)
    if p["engine"] == "sv":
        sv_ttk.set_theme("dark" if p["dark"] else "light")
        root.option_add("*Text.background", p["text_bg"])
        root.option_add("*Text.foreground", p["text_fg"])
    else:
        apply_clam(root, style, p)
    _current["key"] = key
    return p
