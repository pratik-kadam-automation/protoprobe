"""help_ui.py -- the screens of the help system: tooltips, the Help window (F1) and the first-run tour. All text comes from help_content.py.

Tk only, no business logic. Nothing here touches a PLC or the network.
"""
import tkinter as tk
from tkinter import ttk

import help_content


class Tooltip:
    """A small text box shown after the pointer rests on a widget. `text` may be a string or None (then nothing is shown)."""

    DELAY_MS = 450

    def __init__(self, widget, text):
        self.widget, self.text, self._job, self._tip = widget, text, None, None
        if not text:
            return
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._hide()
        self._job = self.widget.after(self.DELAY_MS, self._show)

    def _show(self):
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self._tip = tk.Toplevel(self.widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self._tip, text=self.text, justify="left", wraplength=340, background="#fff8dc", foreground="#222222",
                 relief="solid", borderwidth=1, padx=6, pady=4, font=("Segoe UI", 9)).pack()

    def _hide(self, _event=None):
        if self._job:
            try:
                self.widget.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if self._tip:
            self._tip.destroy()
            self._tip = None


def _item(t, text):
    t.insert("end", text + "\n", "item")


def render_screen_help(text_widget, key):
    s = help_content.SCREENS[key]
    t = text_widget
    t.configure(state="normal")
    t.delete("1.0", "end")
    t.insert("end", s["title"] + "\n", "h1")
    t.insert("end", f"Group: {help_content.group_of(key)}\n\n", "muted")
    t.insert("end", s["summary"] + "\n\n")
    t.insert("end", "Use it: ", "b")
    t.insert("end", s["use_when"] + "\n\n")
    if s["needs"]:
        t.insert("end", "You need\n", "h2")
        for n in s["needs"]:
            _item(t, f"- {n}")
        t.insert("end", "\n")
    t.insert("end", "Steps\n", "h2")
    for i, step in enumerate(s["steps"], 1):
        _item(t, f"{i}. {step}")
    if s["watch"]:
        t.insert("end", "\nWatch out\n", "h2")
        for w in s["watch"]:
            _item(t, f"- {w}")
    t.insert("end", "\nCan I skip it? ", "b")
    t.insert("end", s["skip"] + "\n")
    t.configure(state="disabled")


def render_troubleshooting(text_widget):
    t = text_widget
    t.configure(state="normal")
    t.delete("1.0", "end")
    t.insert("end", "Troubleshooting\n", "h1")
    t.insert("end", "Find your symptom. The same list is in the user guide.\n\n", "muted")
    for symptom, causes, todo in help_content.TROUBLESHOOTING:
        t.insert("end", symptom + "\n", "h2")
        t.insert("end", "Usual causes:\n")
        for c in causes:
            _item(t, f"- {c}")
        t.insert("end", "What to do:\n")
        for d in todo:
            _item(t, f"- {d}")
        t.insert("end", "\n")
    t.configure(state="disabled")


def render_safety_and_keys(text_widget):
    t = text_widget
    t.configure(state="normal")
    t.delete("1.0", "end")
    t.insert("end", "Safety and shortcuts\n", "h1")
    t.insert("end", "\nWhat ProtoProbe does to your PLC\n", "h2")
    t.insert("end", help_content.SAFETY + "\n")
    t.insert("end", "\nKeyboard shortcuts\n", "h2")
    for k, d in help_content.SHORTCUTS:
        _item(t, f"{k}: {d}")
    t.configure(state="disabled")


class HelpWindow(tk.Toplevel):
    """Topics on the left, text on the right. `select(key)` jumps to a screen's help."""

    EXTRA = [("__troubleshooting__", "Troubleshooting"), ("__safety__", "Safety and shortcuts")]

    def __init__(self, parent, key=None):
        super().__init__(parent)
        self.title("ProtoProbe Help")
        self.geometry("900x600")
        self.minsize(640, 400)
        self.bind("<Escape>", lambda e: self.destroy())
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.topics = [k for _, keys in help_content.GROUPS for k in keys] + [k for k, _ in self.EXTRA]
        self.listbox = tk.Listbox(body, width=24, exportselection=False, activestyle="none")
        self.listbox.pack(side="left", fill="y", padx=(8, 0), pady=8)
        for k in self.topics:
            self.listbox.insert("end", help_content.SCREENS[k]["title"] if k in help_content.SCREENS else dict(self.EXTRA)[k])
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        self.text = tk.Text(right, wrap="word", font=("Segoe UI", 10), padx=10, pady=8, borderwidth=0)
        scroll = ttk.Scrollbar(right, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("h1", font=("Segoe UI", 15, "bold"), spacing3=4)
        self.text.tag_configure("h2", font=("Segoe UI", 11, "bold"), spacing1=6)
        self.text.tag_configure("b", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("muted", foreground="gray")
        self.text.tag_configure("item", lmargin1=14, lmargin2=30)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_pick())
        self.select(key or self.topics[0])

    def _on_pick(self):
        sel = self.listbox.curselection()
        if sel:
            self._render(self.topics[sel[0]])

    def select(self, key):
        if key not in self.topics:
            key = self.topics[0]
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(self.topics.index(key))
        self.listbox.see(self.topics.index(key))
        self._render(key)

    def _render(self, key):
        self.current = key
        if key == "__troubleshooting__":
            render_troubleshooting(self.text)
        elif key == "__safety__":
            render_safety_and_keys(self.text)
        else:
            render_screen_help(self.text, key)


class TourWindow(tk.Toplevel):
    """The first-run tour: one card at a time, Back / Next, Skip tour. `on_close(finished)` is called once: True if the person reached the end."""

    def __init__(self, parent, on_close=None):
        super().__init__(parent)
        self.title("Welcome to ProtoProbe")
        self.geometry("660x440")
        self.minsize(600, 380)
        self.transient(parent)
        self.cards = help_content.tour_cards()
        self.index, self.on_close, self._closed = 0, on_close, False
        self.step_label = ttk.Label(self, foreground="gray")
        self.step_label.pack(anchor="w", padx=20, pady=(16, 0))
        self.title_label = ttk.Label(self, font=("Segoe UI", 16, "bold"))
        self.title_label.pack(anchor="w", padx=20, pady=(2, 8))
        self.body = tk.Text(self, wrap="word", height=12, font=("Segoe UI", 10), borderwidth=0, padx=4)
        self.body.pack(fill="both", expand=True, padx=20)
        bottom = ttk.Frame(self)
        bottom.pack(side="bottom", fill="x", padx=20, pady=14)
        ttk.Label(bottom, text="Reopen: Help > Take the tour", foreground="gray").pack(side="left")
        self.next_btn = ttk.Button(bottom, text="Next", command=self.next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(bottom, text="Back", command=self.back)
        self.back_btn.pack(side="right", padx=6)
        ttk.Button(bottom, text="Skip tour", command=lambda: self._finish(False)).pack(side="right", padx=6)
        self.protocol("WM_DELETE_WINDOW", lambda: self._finish(False))
        self.bind("<Escape>", lambda e: self._finish(False))
        self._show()

    def _show(self):
        title, body = self.cards[self.index]
        self.step_label.configure(text=f"{self.index + 1} of {len(self.cards)}")
        self.title_label.configure(text=title)
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("end", body)
        self.body.configure(state="disabled")
        self.back_btn.configure(state="normal" if self.index else "disabled")
        self.next_btn.configure(text="Finish" if self.index == len(self.cards) - 1 else "Next")

    def next(self):
        if self.index >= len(self.cards) - 1:
            self._finish(True)
        else:
            self.index += 1
            self._show()

    def back(self):
        if self.index:
            self.index -= 1
            self._show()

    def _finish(self, done):
        if self._closed:
            return
        self._closed = True
        if self.on_close:
            self.on_close(done)
        self.destroy()


def make_empty_hint(widget, text):
    """A grey instruction shown over an empty list ("No connections yet. Press Add Connection."). Hide it with toggle_hint(label, False)."""
    return ttk.Label(widget, text=text, foreground="gray", justify="center", wraplength=420)


def toggle_hint(label, show):
    if show:
        label.place(relx=0.5, rely=0.35, anchor="center")
    else:
        label.place_forget()
