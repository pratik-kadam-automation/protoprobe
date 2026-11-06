"""
commissioning_screen.py -- the "Commissioning" wizard screen. Thin Tk layer over commissioning.py.

Tabs:  1 Questions  ->  2 Readings  ->  3 Review  ->  4 Export

  Readings: at each reading you type what the HMI shows right now and the PLC memory is read once.
            Readings 1-2 FIND each signal's address, readings 3-4 VERIFY it (a tag is only "confirmed" by
            fresh readings that were not used to find it). Wait at least 30 s between readings and let the
            values move: two readings of the same value prove nothing.

Practice mode runs the whole flow against a simulated PLC so an engineer can rehearse without hardware.
Only Modbus, S7 and MC/SLMP are scanned (OPC UA and BACnet expose real tag names: use Connections > Browse).
"""

import asyncio
import queue
import struct
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import commissioning as cm
import slmp_probe
from plc_reader import DriverReader, PROTOCOL_KEY   # noqa: F401  (the reader lives in plc_reader so the command line tool can use it too)

READINGS_PLAN = ["find", "find", "verify", "verify"]       # the guided sequence; more verify readings can follow
BATCH = 200


# --------------------------------------------------------------------------- where the memory comes from

class PracticePLC:
    """A simulated machine for rehearsal: noise in memory, a few hidden tags, and an HMI you read values from."""
    SPEED = [1.25, 1.8, 1.5, 0.9, 1.1, 1.4]
    MOULD = [3.2, -1.5, 2.0, 0.4, 1.0, -0.6]
    RUN = [1, 0, 1, 0, 1, 0]
    BILLETS = [41, 42, 44, 45, 47, 48]
    TEMP = [1520, 1590, 1555, 1610, 1500, 1575]

    def __init__(self, seed=7):
        import random
        rnd = random.Random(seed)
        self.base = {a: rnd.randrange(0, 65536) for a in range(0, 2000)}
        for a in range(0, 2000, 7):
            self.base[a] = rnd.randrange(0, 60)
        self.k = -1

    def advance(self):
        self.k = min(self.k + 1, len(self.SPEED) - 1)

    def hmi_text(self, k=None):
        k = max(self.k, 0) if k is None else min(max(k, 0), len(self.SPEED) - 1)
        return {"run": "ON" if self.RUN[k] else "OFF", "speed": f"{self.SPEED[k]:.2f}", "mould": f"{self.MOULD[k]:.1f}",
                "billets": str(self.BILLETS[k]), "tundish": str(self.TEMP[k])}

    def read_region(self, region, stop=None):
        if region.name != "D":
            return {}
        k = max(self.k, 0)
        w = dict(self.base)
        b = struct.pack(">f", self.SPEED[k])
        w[100], w[101] = (b[2] << 8) | b[3], (b[0] << 8) | b[1]            # float32, word-swapped (CDAB)
        w[102] = int(round(self.MOULD[k] * 10)) & 0xFFFF                    # int16 x0.1
        w[50] = (w.get(50, 0) & ~(1 << 3)) | (self.RUN[k] << 3)             # run = bit 3
        w[200] = self.BILLETS[k]                                            # counter
        w[300] = self.TEMP[k]                                               # temperature
        return w

    # the practice machine's hidden answers, for the self-test
    ANSWERS = {"speed": ("D", 100), "mould": ("D", 102), "billets": ("D", 200), "tundish": ("D", 300), "run": ("D", 50, 3)}


# --------------------------------------------------------------------------- the screen

class CommissioningScreen(ttk.Frame):
    def __init__(self, parent, app, driver_factory=None, build_range_tags=None):
        super().__init__(parent)
        self.app = app
        self.driver_factory, self.build_range_tags = driver_factory, build_range_tags
        self.answers = {}
        self.answer_vars = {}
        self.moments = []                 # cm.Moment, in order
        self.cands = {}                   # signal key -> [Candidate] (numbers/counters) or [(region, addr, bit)] (bool)
        self.chosen = {}                  # signal key -> the chosen candidate
        self.hmi_vars = {}                # signal key -> StringVar (what the HMI shows now)
        self.pick_vars = {}               # signal key -> StringVar (chosen candidate label)
        self.regions = []
        self.mode = "search"              # "search" (find addresses from readings) or "verify" (the programmer's addresses, proved by readings)
        self.signals = []
        self.practice = None
        self.practice_var = tk.BooleanVar(value=False)
        self.sync = False                 # tests read synchronously
        self._q = queue.Queue()
        self._busy = False
        self._build()
        self.after(150, self._pump)

    # ---- layout -------------------------------------------------------------------------------
    def _build(self):
        head = ttk.Frame(self)
        head.pack(fill="x", padx=20, pady=(18, 4))
        ttk.Label(head, text="Commissioning wizard", font=("Segoe UI", 16)).pack(side="left")
        ttk.Checkbutton(head, text="Practice mode (simulated PLC, no hardware)", variable=self.practice_var,
                        command=self._practice_toggled).pack(side="right")
        ttk.Label(self, wraplength=980, foreground="gray", justify="left",
                  text="Find each signal's address and PROVE it against the HMI before the gateway goes live, so nobody has to rediscover "
                       "a wrong tag on a deployed gateway weeks later. Answer a few questions, record four readings (two to find, two to "
                       "verify), then export a commissioning pack for the dashboard.").pack(anchor="w", padx=20, pady=(0, 8))
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        self.t_q, self.t_r, self.t_v, self.t_e = (ttk.Frame(self.nb) for _ in range(4))
        for f, t in ((self.t_q, "1  Questions"), (self.t_r, "2  Readings"), (self.t_v, "3  Review"), (self.t_e, "4  Export")):
            self.nb.add(f, text=t)
        self._build_questions()
        self._build_readings()
        self._build_review()
        self._build_export()

    def _build_questions(self):
        self.q_body = ttk.Frame(self.t_q)
        self.q_body.pack(fill="both", expand=True, padx=12, pady=10)
        for q in cm.QUESTIONS:
            self.answer_vars[q.key] = tk.StringVar(value=q.default)
            self.answer_vars[q.key].trace_add("write", lambda *_: self._render_questions())
        self.q_msg = ttk.Label(self.t_q, text="", foreground="#c0392b")
        self.q_msg.pack(anchor="w", padx=14)
        bar = ttk.Frame(self.t_q)
        bar.pack(fill="x", padx=14, pady=10)
        self.btn_test = ttk.Button(bar, text="Test connection (Mitsubishi SLMP)", command=self.test_connection)
        self.btn_test.pack(side="left")
        self.test_msg = ttk.Label(bar, text="", foreground="gray", wraplength=620, justify="left")
        self.test_msg.pack(side="left", padx=12)
        ttk.Button(bar, text="Next: readings", command=self._questions_done).pack(side="right")
        self._render_questions()

    def _render_questions(self):
        for w in self.q_body.winfo_children():
            w.destroy()
        a = self.collect_answers()
        for q in cm.QUESTIONS:
            if not q.visible(a):
                continue
            row = ttk.Frame(self.q_body)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=q.text + (" *" if q.required else ""), width=62, anchor="w").pack(side="left")
            var = self.answer_vars[q.key]
            if q.kind == "choice":
                labels = [lab for _, lab in q.choices]
                cb = ttk.Combobox(row, state="readonly", width=34, values=labels)
                cur = dict(q.choices).get(var.get(), var.get())
                cb.set(cur)
                cb.bind("<<ComboboxSelected>>", lambda e, k=q.key, c=q.choices, w=cb: self._set_choice(k, c, w.get()))
                cb.pack(side="left")
            elif q.kind == "bool":
                ttk.Checkbutton(row, variable=var, onvalue="yes", offvalue="no").pack(side="left")
            else:
                ttk.Entry(row, textvariable=var, width=36).pack(side="left")
            if q.help:
                ttk.Label(row, text=q.help, foreground="gray").pack(side="left", padx=8)

    def _set_choice(self, key, choices, label):
        for value, lab in choices:
            if lab == label:
                self.answer_vars[key].set(value)
                return

    def collect_answers(self):
        return {k: v.get().strip() for k, v in self.answer_vars.items()}

    def _questions_done(self):
        self.answers = self.collect_answers()
        d = cm.derive(self.answers)
        if d["missing"]:
            labels = {q.key: q.text for q in cm.QUESTIONS}
            self.q_msg.configure(text="Still needed: " + "; ".join(labels[k].split(" (")[0] for k in d["missing"]))
            return
        self.q_msg.configure(text="")
        self.regions = cm.scan_plan(self.answers)
        self._fill_readings()
        self.nb.select(self.t_r)

    # ---- readings ----------------------------------------------------------------------------
    def _build_readings(self):
        self.r_info = ttk.Label(self.t_r, text="Answer the questions first.", wraplength=980, justify="left")
        self.r_info.pack(anchor="w", padx=14, pady=(10, 4))
        self.practice_hint = ttk.Label(self.t_r, text="", foreground="#2471a3", wraplength=980, justify="left")
        self.practice_hint.pack(anchor="w", padx=14)
        holder = ttk.Frame(self.t_r)
        holder.pack(fill="both", expand=True, padx=14, pady=6)
        self._canvas = tk.Canvas(holder, highlightthickness=0)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self.grid_frame = ttk.Frame(self._canvas)
        self._canvas.create_window((0, 0), window=self.grid_frame, anchor="nw")
        self.grid_frame.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        bar = ttk.Frame(self.t_r)
        bar.pack(fill="x", padx=14, pady=8)
        self.btn_read = ttk.Button(bar, text="Record reading", command=self.record_reading)
        self.btn_read.pack(side="left")
        self.btn_find = ttk.Button(bar, text="Find tags", command=self.find_tags, state="disabled")
        self.btn_find.pack(side="left", padx=8)
        self.btn_review = ttk.Button(bar, text="Next: review", command=self._to_review)
        self.btn_review.pack(side="right")
        ttk.Button(bar, text="Import tag list ...", command=self.import_tag_list).pack(side="left", padx=8)
        self.status = ttk.Label(bar, text="", foreground="gray")
        self.status.pack(side="left", padx=12)

    def _fill_readings(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        d = cm.derive(self.answers)
        self.mode = "search"
        self.signals = d["machine"]["signals"]
        self.chosen, self.cands, self.moments = {}, {}, []
        proto = d["protocol"]
        if proto in ("opcua", "bacnet"):
            self.r_info.configure(text="OPC UA and BACnet expose real tag names, so there is nothing to scan: use Connections > Browse to pick the nodes. "
                                       "This wizard does not cover them yet; you can still answer the questions and keep notes.")
        elif not self.regions:
            self.r_info.configure(text="No memory regions to read for this answer set.")
        self._render_grid()
        self._update_info()
        self._practice_toggled(refresh_only=True)

    def _render_grid(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        for i, h in enumerate(("Signal", "HMI shows now", "Chosen address", "Readings (HMI vs PLC)")):
            ttk.Label(self.grid_frame, text=h, font=("Segoe UI", 9, "bold")).grid(row=0, column=i, sticky="w", padx=6, pady=(0, 4))
        self.hmi_vars, self.pick_vars, self.read_labels, self.pick_boxes = {}, {}, {}, {}
        for r, s in enumerate(self.signals, start=1):
            ttk.Label(self.grid_frame, text=s["label"]).grid(row=r, column=0, sticky="w", padx=6, pady=2)
            self.hmi_vars[s["key"]] = tk.StringVar()
            ttk.Entry(self.grid_frame, textvariable=self.hmi_vars[s["key"]], width=14).grid(row=r, column=1, sticky="w", padx=6)
            self.pick_vars[s["key"]] = tk.StringVar()
            cb = ttk.Combobox(self.grid_frame, textvariable=self.pick_vars[s["key"]], state="readonly", width=34, values=[])
            cb.grid(row=r, column=2, sticky="w", padx=6)
            cb.bind("<<ComboboxSelected>>", lambda e, k=s["key"]: self._pick(k))
            self.pick_boxes[s["key"]] = cb
            if self.mode == "verify" and s["key"] in self.chosen:
                c = self.chosen[s["key"]]
                lab = (f"DB{c.region.db}." if c.region.area == "DB" else c.region.area) + f"{c.addr}  {c.datatype}" + (f" {c.byte_order}" if c.datatype.endswith("32") else "") + "  (from the list)"
                cb.configure(values=[lab]); cb.current(0)
            self.read_labels[s["key"]] = ttk.Label(self.grid_frame, text="", foreground="gray")
            self.read_labels[s["key"]].grid(row=r, column=3, sticky="w", padx=6)

    def _next_phase(self):
        if self.mode == "verify":
            return "verify"           # the address came from the programmer, not from these readings, so every reading can prove it
        n = len(self.moments)
        return READINGS_PLAN[n] if n < len(READINGS_PLAN) else "verify"

    def _update_info(self):
        n = len(self.moments)
        nxt = self._next_phase()
        if self.mode == "verify":
            txt = (f"Verifying the imported tag list ({len(self.signals)} tags). Type what the HMI shows for the signals it displays (leave the rest blank) and press Record reading. "
                   f"A tag is confirmed by two readings at least {cm.MIN_GAP_S} s apart with different values. Readings so far: {n}.")
            self.r_info.configure(text=txt)
            self.btn_find.configure(state="disabled")
            return
        if n < 2:
            txt = f"Reading {n + 1} of 2 to FIND the addresses. Type what the HMI shows for each signal you can see (leave blank if it is not shown), then press Record reading."
        elif n == 2 and not self.chosen:
            txt = "Press Find tags: every address is tried against both readings."
        elif n < 4:
            txt = f"Reading {n + 1} to VERIFY (fresh values, after the address was chosen). Wait at least {cm.MIN_GAP_S} s and let the values move first."
        else:
            txt = "Four readings are in. Record another verify reading if a tag is still 'probable', or go to Review."
        self.r_info.configure(text=txt)
        self.btn_find.configure(state="normal" if len(self.moments) >= 2 else "disabled")

    def _practice_toggled(self, refresh_only=False):
        if self.practice_var.get():
            if self.practice is None:
                self.practice = PracticePLC()
            self._practice_values()
        else:
            self.practice = None
            self.practice_hint.configure(text="")

    def practice_next_values(self):
        """What the simulated HMI will show at the NEXT reading (so a person rehearsing knows what to type)."""
        return self.practice.hmi_text(self.practice.k + 1) if self.practice is not None else {}

    def _practice_values(self):
        if self.practice is not None:
            show = ", ".join(f"{k} = {v}" for k, v in self.practice_next_values().items())
            self.practice_hint.configure(text=f"PRACTICE MODE. The simulated HMI shows: {show}.  Type those values, then press Record reading.")

    def record_reading(self):
        if self._busy or not self.regions:
            return
        hmi = {}
        for s in self.signals:
            t = self.hmi_vars[s["key"]].get().strip()
            if not t:
                continue
            try:
                hmi[s["key"]] = cm.hmi_reading(s, t)
            except ValueError:
                messagebox.showerror("Not a number", f"'{t}' for {s['label']} is not a number (use ON/OFF for on/off signals).")
                return
        if not hmi:
            messagebox.showinfo("Nothing typed", "Type at least one value the HMI shows right now.")
            return
        phase = self._next_phase()
        if phase == "verify" and not self.chosen:
            messagebox.showinfo("Find the tags first", "Press Find tags after the two finding readings, then record verify readings.")
            return
        self._busy = True
        self.btn_read.configure(state="disabled")
        self.status.configure(text="Reading the PLC ...")
        regions = [r for r in self.regions if r.enabled]
        if self.practice is not None:
            self.practice.advance()
        reader = self.practice if self.practice is not None else self._make_reader()
        if reader is None:
            self._busy = False
            self.btn_read.configure(state="normal")
            return

        def work():
            try:
                mem = {r.name: reader.read_region(r) for r in regions}
                self._q.put(("moment", (hmi, mem, phase)))
            except Exception as e:      # noqa: BLE001 -- show whatever the driver said
                self._q.put(("error", str(e)))

        if self.sync:
            work()
            self._pump()
        else:
            threading.Thread(target=work, daemon=True).start()

    def _make_reader(self):
        if self.driver_factory is None:
            messagebox.showerror("Not available", "The PLC drivers are not loaded: use Practice mode, or run ProtoProbe from the main app.")
            return None
        a, d = self.answers, cm.derive(self.answers)
        p = d["protocol"]
        if p == "modbus_rtu":
            messagebox.showinfo("Serial Modbus", "Create the serial connection in Connections first; the wizard reads TCP/IP PLCs directly.")
            return None
        iface = {"ip": a.get("plc_ip", ""), "port": d["port"]}
        if p == "mc":
            iface["transport"] = d["transport"]
            if d["local_ip"]:
                iface["local_ip"] = d["local_ip"]
        if p == "s7":
            iface = {"ip": a.get("plc_ip", ""), "rack": int(a.get("rack") or 0), "slot": int(a.get("slot") or 1)}
        if p == "modbus_tcp":
            iface["unit_id"] = int(a.get("unit_id") or 1)
        return DriverReader(p, iface, self.driver_factory, self.build_range_tags)

    def _pump(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == "moment":
                    hmi, mem, phase = data
                    at = (1793955600 + len(self.moments) * 600) if self.practice is not None else time.time()
                    self.moments.append(cm.Moment(at, hmi, mem, phase))
                    self._after_moment()
                elif kind == "probe":
                    self._probe_done(data)
                elif kind == "error":
                    messagebox.showerror("Could not read the PLC", data)
                    self._busy = False
                    self.btn_read.configure(state="normal")
                    self.status.configure(text="")
        except queue.Empty:
            pass
        if not self.sync:
            self.after(150, self._pump)

    def _after_moment(self):
        self._busy = False
        self.btn_read.configure(state="normal")
        m = self.moments[-1]
        n = len(self.moments)
        self.status.configure(text=f"Reading {n} recorded ({m.phase}).")
        for v in self.hmi_vars.values():
            v.set("")
        if m.phase == "verify":
            self._show_verify(m)
        self._update_info()
        self._practice_values()

    # ---- connection test ---------------------------------------------------------------------
    def test_connection(self):
        a = self.collect_answers()
        d = cm.derive(a)
        if d["protocol"] != "mc":
            self.test_msg.configure(text="The connection test is for Mitsubishi SLMP for now.")
            return
        if not a.get("plc_ip"):
            self.test_msg.configure(text="Type the PLC IP address first.")
            return
        self.btn_test.configure(state="disabled")
        self.test_msg.configure(text="Sending one read-only request to each port over UDP and TCP ...")

        def work():
            res = slmp_probe.probe(a["plc_ip"], local_ip=a.get("local_ip") or None, timeout=1.0)
            self._q.put(("probe", res))

        if self.practice_var.get():
            self._q.put(("probe", [{"transport": "udp", "port": 5006, "status": "ok", "end_code": 0, "text": "", "reply_hex": ""}]))
        elif self.sync:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()
        if self.sync:
            self._pump()

    def _probe_done(self, res):
        self.btn_test.configure(state="normal")
        self.test_msg.configure(text=slmp_probe.describe(res))
        b = slmp_probe.best(res)
        if b:
            self.answer_vars["slmp_transport"].set(b["transport"])
            self.answer_vars["plc_port"].set(str(b["port"]))

    # ---- the programmer's tag list ---------------------------------------------------------
    def import_tag_list(self, path=None):
        d = cm.derive(self.collect_answers())
        if d["protocol"] not in ("mc", "s7"):
            messagebox.showinfo("Not available yet", "Tag-list import works for Mitsubishi (GX Works3 label export) and Siemens (TIA Portal tag table, Excel).")
            return
        types = [("TIA Portal tag table", "*.xlsx *.csv *.txt")] if d["protocol"] == "s7" else [("GX Works3 label export", "*.tsv *.txt *.csv")]
        path = path or filedialog.askopenfilename(filetypes=types + [("All files", "*.*")])
        if not path:
            return
        self.load_imported(cm.parse_tag_file(path, d["protocol"], d["byte_orders"][0]))

    def load_tag_list(self, text, byte_order="CDAB"):
        """The GX Works3 label list as text (used by tests and paste)."""
        self.load_imported(cm.parse_gx_labels(text, byte_order))

    def load_imported(self, r):
        if r.get("error"):
            messagebox.showerror("Could not read the list", r["error"])
            return
        if not r["tags"]:
            messagebox.showerror("Nothing to import", "No usable tags found.\n\n" + "\n".join(f"{n}: {why}" for n, why in r["skipped"][:12]))
            return
        self.mode = "verify"
        self.answers = self.collect_answers()
        self.signals = [cm.imported_signal(t) for t in r["tags"]]
        self.regions = cm.import_regions(r["tags"])
        self.chosen, self.cands, self.moments = {}, {}, []
        for sig in self.signals:
            t = sig["imported"]
            region = next(rg for rg in self.regions if rg.area == t["area"] and rg.db == t.get("db") and rg.start <= t["number"] <= rg.end)
            self.chosen[sig["key"]] = cm.candidate_for_import(t, region)
        self._render_grid()
        self.nb.select(self.t_r)
        renamed = [t for t in r["tags"] if t.get("renamed_from")]
        self.status.configure(text=f"{len(self.signals)} tags imported" + (f", {len(r['skipped'])} skipped" if r["skipped"] else "")
                              + (f", {len(renamed)} renamed (spaces became _)" if renamed else "") + ".")
        notes = [f"{n}: {why}" for n, why in r["skipped"][:25]] + [f"renamed: {t['renamed_from']} -> {t['name']}" for t in renamed[:15]]
        if notes:
            messagebox.showinfo("Please check", "\n".join(notes))

    def apply_suggested_scales(self):
        """The address is right but the HMI shows the value scaled: set the scale the readings point to."""
        changed = []
        for sig in self.signals:
            c = self.chosen.get(sig["key"])
            if c is None or sig["kind"] == "bool":
                continue
            pairs = []
            for m in self.moments:
                if sig["key"] in m.hmi:
                    read = cm.decode_candidate(m.memory, c)
                    if read is not None:
                        pairs.append((m.hmi[sig["key"]][0], read / c.scale if c.scale else read))
            if pairs and all(abs(h - r * c.scale) > m.hmi[sig["key"]][1] for (h, r), m in zip(pairs, [mm for mm in self.moments if sig["key"] in mm.hmi])):
                sc = cm.suggest_scale(pairs)
                if sc:
                    c.scale = sc
                    changed.append(f"{sig['name']} x{sc:g}")
        self.build_tags()
        return changed

    # ---- finding ------------------------------------------------------------------------------
    def find_tags(self):
        finds = [m for m in self.moments if m.phase == "find"]
        if len(finds) < 2:
            messagebox.showinfo("Two readings first", "Record two finding readings (with different HMI values) first.")
            return
        d = cm.derive(self.answers)
        self.cands, self.chosen = {}, {}
        problems = []
        for s in self.signals:
            key = s["key"]
            if not any(key in m.hmi for m in finds):
                continue
            if s["kind"] == "bool":
                c = cm.find_bits(finds, key, self.regions)
                self.cands[key] = c
                labels = [f"{r.name} {a}" + (f" bit {b}" if b is not None else "") for r, a, b in c]
            else:
                c = cm.find_numeric(finds, key, self.regions, d["byte_orders"], kind=s["kind"])
                self.cands[key] = c
                labels = [x.label() + ("" if x.strong else "  (partial)") for x in c]
                if c and not c[0].discriminating:
                    problems.append(s["label"])
            box = self.pick_boxes[key]
            box.configure(values=labels or ["(nothing found)"])
            if labels:
                box.current(0)
                self._pick(key)
            else:
                self.pick_vars[key].set("(nothing found)")
        msg = "Tags found. Check each choice, then record two VERIFY readings."
        if problems:
            msg += "  Readings too close together to tell candidates apart for: " + ", ".join(problems) + ". Record another finding reading after the value has moved."
        self.status.configure(text=msg)
        self._update_info()

    def _pick(self, key):
        box = self.pick_boxes[key]
        i = box.current()
        cands = self.cands.get(key, [])
        if 0 <= i < len(cands):
            self.chosen[key] = cands[i]
        else:
            self.chosen.pop(key, None)

    # ---- verifying ----------------------------------------------------------------------------
    def _decode(self, signal, cand, memory):
        if signal["kind"] == "bool":
            region, addr, bit = cand
            return cm.decode_bit(memory, region, addr, bit)
        return cm.decode_candidate(memory, cand)

    def _show_verify(self, m):
        for s in self.signals:
            key = s["key"]
            if key in self.chosen and key in m.hmi:
                read = self._decode(s, self.chosen[key], m.memory)
                hmi = m.hmi[key][0]
                ok = read is not None and abs(read - hmi) <= m.hmi[key][1]
                old = self.read_labels[key].cget("text")
                note = "no data" if read is None else f"{hmi:g} vs {read:g} " + ("OK" if ok else "MISMATCH")
                self.read_labels[key].configure(text=(old + "   " if old else "") + note)

    def _evidence_for(self, s, cand):
        out = []
        for m in self.moments:
            if s["key"] not in m.hmi:
                continue
            read = self._decode(s, cand, m.memory)
            if read is None:
                continue
            tol = m.hmi[s["key"]][1]
            ev = cm.evidence(m.hmi[s["key"]][0], round(read, 6), m.at, m.phase, self.who())
            ev["tolerance"] = tol
            out.append(ev)
        return out

    def who(self):
        return getattr(self, "engineer_var", tk.StringVar()).get().strip()

    # ---- review -------------------------------------------------------------------------------
    def _build_review(self):
        ttk.Label(self.t_v, wraplength=980, foreground="gray", justify="left",
                  text="Every tag with the evidence behind it. 'confirmed' = two fresh readings agreed with the HMI at least 30 s apart with different values; "
                       "'probable' = it fits but is not proved yet; 'unverified' = no matching evidence (it will not be applied on the dashboard). "
                       "Double-click a tag name to rename it (it must equal the FAQTIS parameter name).").pack(anchor="w", padx=14, pady=(10, 4))
        cols = ("name", "address", "type", "scale", "role", "unit", "conf", "evidence")
        self.tree = ttk.Treeview(self.t_v, columns=cols, show="headings", height=14)
        for c, w in (("name", 170), ("address", 120), ("type", 110), ("scale", 60), ("role", 90), ("unit", 60), ("conf", 90), ("evidence", 300)):
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("confirmed", foreground="#1e8449")
        self.tree.tag_configure("probable", foreground="#b9770e")
        self.tree.tag_configure("unverified", foreground="#c0392b")
        self.tree.pack(fill="both", expand=True, padx=14, pady=6)
        self.tree.bind("<Double-1>", self._rename)
        self.rename = {}
        rb = ttk.Frame(self.t_v)
        rb.pack(fill="x", padx=14, pady=8)
        ttk.Button(rb, text="Apply suggested scales", command=self._apply_scales_clicked).pack(side="left")
        self.scale_msg = ttk.Label(rb, text="", foreground="gray")
        self.scale_msg.pack(side="left", padx=10)
        ttk.Button(rb, text="Next: export", command=lambda: (self.build_tags(), self.nb.select(self.t_e))).pack(side="right")

    def _to_review(self):
        self.build_tags()
        self.nb.select(self.t_v)

    def build_tags(self):
        d = cm.derive(self.answers)
        tags = []
        for s in self.signals:
            key = s["key"]
            if key not in self.chosen:
                continue
            cand = self.chosen[key]
            ev = self._evidence_for(s, cand)
            name = self.rename.get(key) or s["name"]
            if s["kind"] == "bool":
                region, addr, bit = cand
                address = cm.format_address(d["protocol"], region, addr, "bool", bit if bit is not None else None)
                tag = cm.make_tag(s, address, "bool", "", 1, ev, name=name)
            else:
                address = cm.format_address(d["protocol"], cand.region, cand.addr, cand.datatype)
                tag = cm.make_tag(s, address, cand.datatype, cand.byte_order if cand.datatype.endswith("32") else "", cand.scale, ev, name=name)
            tags.append(tag)
        self.tags = tags
        for i in self.tree.get_children():
            self.tree.delete(i)
        for t in tags:
            n_ev = len(t["evidence"])
            good = sum(1 for e in t["evidence"] if cm._matches(e, t))
            self.tree.insert("", "end", iid=t["name"], tags=(t["confidence"],),
                             values=(t["name"], t["address"], t["datatype"] + (f" {t['byte_order']}" if t.get("byte_order") else ""),
                                     t.get("scale", ""), t["role"], t.get("unit", ""), t["confidence"], f"{good} of {n_ev} readings match"))
        self._refresh_export()
        return tags

    def _apply_scales_clicked(self):
        changed = self.apply_suggested_scales()
        self.scale_msg.configure(text=("Scale set: " + ", ".join(changed)) if changed else "No scale to suggest: the readings do not point to a power of ten.")

    def _rename(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        old = sel[0]
        from tkinter import simpledialog
        new = simpledialog.askstring("Tag name", "Tag name (letters, digits, _ . - [ ]):", initialvalue=old, parent=self)
        if not new:
            return
        key = next((s["key"] for s in self.signals if (self.rename.get(s["key"]) or s["name"]) == old), None)
        if key:
            self.rename[key] = new.strip()
            self.build_tags()

    # ---- export -------------------------------------------------------------------------------
    def _build_export(self):
        row = ttk.Frame(self.t_e)
        row.pack(fill="x", padx=14, pady=(14, 4))
        ttk.Label(row, text="Your name (recorded with the evidence)").pack(side="left")
        self.engineer_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.engineer_var, width=24).pack(side="left", padx=8)
        self.exp_text = tk.Text(self.t_e, height=14, wrap="word")
        self.exp_text.pack(fill="both", expand=True, padx=14, pady=8)
        self.exp_text.configure(state="disabled")
        bar = ttk.Frame(self.t_e)
        bar.pack(fill="x", padx=14, pady=8)
        ttk.Button(bar, text="Save commissioning pack ...", command=self.save).pack(side="left")
        ttk.Label(bar, foreground="gray", text="Then in the dashboard: Commissioning > Import pack.").pack(side="left", padx=10)

    def pack_dict(self):
        if not hasattr(self, "tags"):
            self.build_tags()
        return cm.build_pack(self.answers, self.tags, self.who())

    def _refresh_export(self):
        pack = self.pack_dict()
        s = cm.summary(pack)
        errs = cm.validate_pack(pack)
        lines = [f"{len(self.tags)} tags:  {s['confirmed']} confirmed,  {s['probable']} probable,  {s['unverified']} unverified.", ""]
        if s["probable"] or s["unverified"]:
            lines.append("Not confirmed yet: record more VERIFY readings (values must change and be at least 30 s apart) before you leave site.")
        if errs:
            lines += ["", "Fix before exporting:"] + ["  - " + e for e in errs]
        self.exp_text.configure(state="normal")
        self.exp_text.delete("1.0", "end")
        self.exp_text.insert("1.0", "\n".join(lines))
        self.exp_text.configure(state="disabled")

    def save(self, path=None):
        pack = self.pack_dict()
        errs = cm.validate_pack(pack)
        if errs:
            messagebox.showerror("The pack has problems", "\n".join(errs))
            return None
        path = path or filedialog.asksaveasfilename(defaultextension=".json", initialfile=f"{self.answers.get('machine_id', 'machine')}-commissioning.json",
                                                    filetypes=[("Commissioning pack", "*.json")])
        if not path:
            return None
        cm.save_pack(pack, path)
        messagebox.showinfo("Saved", f"Saved {path}\n\nImport it in the dashboard: Commissioning > Import pack.")
        return path
