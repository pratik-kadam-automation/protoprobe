"""discovery.py -- the engine behind a long probing session: read a PLC's memory in blocks, keep ONLY the changes (with their time), find out which
bytes really move, and read those more often.

Pure logic and a small SQLite store: no screens, no driver import. The caller gives a `reader(area, start, size) -> bytes` (for Siemens that is
`S7Driver._read_area_blocking`); this makes it testable with a fake PLC and a fake clock.

Ideas that matter:
  * Per BYTE, change-only: from the change log a bit, a 16-bit word or a 32-bit value can be rebuilt for any moment, and the file stays small.
  * Two tiers: every byte is swept slowly forever (a counter that moves once an hour must not be missed); bytes that changed in the last
    re-evaluation window are the "active set" and are read fast.
  * A byte that changes at nearly every look (clock memory, a flashing lamp) is "noisy": it is still read, but only one change per `noisy_store_s`
    is stored, so it cannot fill the disk.
  * Read-only: nothing here writes to a PLC.
"""
import sqlite3
import time


class Store:
    """Change log + marks of one session. `:memory:` for tests."""
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS changes(ts REAL, area TEXT, addr INTEGER, old INTEGER, new INTEGER);
            CREATE INDEX IF NOT EXISTS changes_addr ON changes(area, addr, ts);
            CREATE TABLE IF NOT EXISTS marks(ts REAL, label TEXT);
            CREATE TABLE IF NOT EXISTS tiers(ts REAL, area TEXT, addr INTEGER, tier TEXT);
        """)
        self.pending = 0

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, str(value)))
        self.db.commit()

    def add_change(self, ts, area, addr, old, new):
        self.db.execute("INSERT INTO changes VALUES (?, ?, ?, ?, ?)", (ts, area, addr, old, new))
        self.pending += 1
        if self.pending >= 200:
            self.flush()

    def add_mark(self, ts, label):
        self.db.execute("INSERT INTO marks VALUES (?, ?)", (ts, label))
        self.db.commit()

    def add_tier(self, ts, area, addr, tier):
        self.db.execute("INSERT INTO tiers VALUES (?, ?, ?, ?)", (ts, area, addr, tier))

    def flush(self):
        self.db.commit()
        self.pending = 0

    def changes_of(self, area, addr):
        return self.db.execute("SELECT ts, old, new FROM changes WHERE area = ? AND addr = ? ORDER BY ts", (area, addr)).fetchall()

    def change_counts(self):
        return {(a, b): n for a, b, n in self.db.execute("SELECT area, addr, COUNT(*) FROM changes GROUP BY area, addr")}

    def marks(self):
        return self.db.execute("SELECT ts, label FROM marks ORDER BY ts").fetchall()

    def close(self):
        self.flush()
        self.db.close()


def blocks_covering(addresses, block=200, gap=16, limit=None):
    """Merge sorted byte addresses into (start, size) reads: neighbours closer than `gap` share one request, a request is at most `block` bytes."""
    out = []
    for a in sorted(addresses):
        if out and a - (out[-1][0] + out[-1][1] - 1) <= gap and (a - out[-1][0] + 1) <= block:
            out[-1] = (out[-1][0], a - out[-1][0] + 1)
        else:
            out.append((a, 1))
    if limit is not None:
        out = [(s, min(n, max(0, limit - s))) for s, n in out if s < limit]
    return out


class Engine:
    def __init__(self, reader, store, areas, block=200, reeval_s=900.0, active_min_changes=1, noisy_ratio=0.5, noisy_store_s=10.0,
                 active_period_s=1.0, sweep_period_s=15.0, clock=time.time):
        """areas: {"M": (first_byte, last_byte_inclusive), "I": (...), ...}."""
        self.reader, self.store, self.areas, self.block = reader, store, dict(areas), block
        self.reeval_s, self.active_min_changes, self.noisy_ratio, self.noisy_store_s = reeval_s, active_min_changes, noisy_ratio, noisy_store_s
        self.active_period_s, self.sweep_period_s, self.clock = active_period_s, sweep_period_s, clock
        self.last = {a: {} for a in self.areas}           # area -> {addr: last value}
        self.window = {a: {} for a in self.areas}         # area -> {addr: [looks, changes]} since the last re-evaluation
        self.last_stored = {}                             # (area, addr) -> ts of the last stored change (for noisy bytes)
        self.active, self.noisy = {a: set() for a in self.areas}, {a: set() for a in self.areas}
        self.reads = self.read_errors = self.stored = self.dropped = 0
        self.started = None
        self._next_reeval = self._next_sweep = None

    # ---- one read of a block: compare, store the changes
    def _look(self, area, start, size):
        try:
            data = self.reader(area, start, size)
        except Exception:
            self.read_errors += 1
            return False
        self.reads += 1
        now = self.clock()
        last, win = self.last[area], self.window[area]
        for i, value in enumerate(data):
            addr = start + i
            seen = win.setdefault(addr, [0, 0])
            seen[0] += 1
            if addr in last and last[addr] != value:
                seen[1] += 1
                key = (area, addr)
                if addr in self.noisy[area] and now - self.last_stored.get(key, -1e18) < self.noisy_store_s:
                    self.dropped += 1
                else:
                    self.store.add_change(now, area, addr, last[addr], value)
                    self.last_stored[key] = now
                    self.stored += 1
            last[addr] = value
        return True

    def sweep_all(self):
        for area, (first, last) in self.areas.items():
            start = first
            while start <= last:
                size = min(self.block, last - start + 1)
                if not self._look(area, start, size):
                    break                                   # the rest of this area would fail the same way; try again at the next sweep
                start += size

    def sweep_active(self):
        for area, addrs in self.active.items():
            for start, size in blocks_covering(addrs, self.block, limit=self.areas[area][1] + 1):
                self._look(area, start, size)

    def reevaluate(self):
        """Which bytes moved during the last window? They become the active set; the ones that moved at nearly every look become noisy."""
        now = self.clock()
        for area in self.areas:
            active, noisy = set(), set()
            for addr, (looks, changes) in self.window[area].items():
                if changes >= self.active_min_changes:
                    active.add(addr)
                    if looks >= 4 and changes / (looks - 1) >= self.noisy_ratio:
                        noisy.add(addr)
            for addr in active - self.active[area]:
                self.store.add_tier(now, area, addr, "active")
            for addr in noisy - self.noisy[area]:
                self.store.add_tier(now, area, addr, "noisy")
            self.active[area], self.noisy[area] = active, noisy
            self.window[area] = {}
        self.store.flush()

    def tick(self):
        """One step of the schedule; call it often (about every 0.1-0.5 s). Returns what it did, for the screen."""
        now = self.clock()
        if self.started is None:
            self.started = now
            self._next_reeval, self._next_sweep = now + self.reeval_s, now
        did = []
        if now >= self._next_sweep:
            self.sweep_all()
            self._next_sweep = now + self.sweep_period_s
            did.append("sweep")
        elif any(self.active.values()):
            self.sweep_active()
            did.append("active")
        if now >= self._next_reeval:
            self.reevaluate()
            self._next_reeval = now + self.reeval_s
            did.append("reevaluate")
        return did

    def status(self):
        now = self.clock()
        return {"running_s": 0 if self.started is None else now - self.started, "reads": self.reads, "read_errors": self.read_errors,
                "stored": self.stored, "dropped_noisy": self.dropped, "active_bytes": sum(len(v) for v in self.active.values()),
                "noisy_bytes": sum(len(v) for v in self.noisy.values()),
                "next_reevaluation_s": None if self._next_reeval is None else max(0.0, self._next_reeval - now)}


def word_series(store, area, addr, size=2, points=200):
    """The values of the 16-bit (size 2) or 32-bit (size 4) big-endian word at `addr` over the session, rebuilt from the byte change logs:
    [(ts, value), ...] with one point per change of any of its bytes (at most `points`, evenly thinned)."""
    logs = {a: store.changes_of(area, a) for a in range(addr, addr + size)}
    events = sorted({ts for log in logs.values() for ts, _o, _n in log})
    if not events:
        return []
    current = {a: (log[0][1] if log else 0) for a, log in logs.items()}      # the value before the first change
    idx = {a: 0 for a in logs}
    series = []
    for ts in events:
        for a, log in logs.items():
            while idx[a] < len(log) and log[idx[a]][0] <= ts:
                current[a] = log[idx[a]][2]
                idx[a] += 1
        value = 0
        for a in range(addr, addr + size):
            value = (value << 8) | current[a]
        series.append((ts, value))
    if len(series) > points:
        step = len(series) / points
        series = [series[int(i * step)] for i in range(points)] + [series[-1]]
    return series
