"""signal_hints.py -- a suggestion, from how a value behaved during a scan, of what it might be good for (OEE, predictive maintenance).

Pure functions, no screens. These are GUESSES from a few snapshots: a person must confirm them by running the machine. Three snapshots
cannot tell a slow counter from a constant, or a fast blink from a steady signal: the text always says what it saw and what it assumes.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Hint:
    kind: str                  # idle | constant | counter | countdown | flags | measurement | state | blink | steady_on | unknown
    text: str                  # one line for the table
    oee: str = ""              # what it could be for OEE: "run state", "production count" ('' = nothing)
    predictive: str = ""       # what it could be for predictive maintenance: "trend", "usage count" ('' = nothing)


def _as_ints(values):
    out = []
    for v in values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            return None
    return out


def classify_word(values):
    """A 16/32-bit value read several times in a row (oldest first)."""
    v = _as_ints(values)
    if v is None or len(v) < 2:
        return Hint("unknown", "scan again with more snapshots to get a suggestion")
    if len(set(v)) == 1:
        if v[0] == 0:
            return Hint("idle", "always 0: unused, or the machine was stopped")
        return Hint("constant", f"stayed at {v[0]}: a setting or configured value (planned time, ideal cycle ...); not flagged as a candidate, a PLC holds many")
    diffs = [b - a for a, b in zip(v, v[1:])]
    if all(d >= 0 for d in diffs):
        return Hint("counter", "only went up: a counter (parts, cycles, run time)", oee="production count", predictive="usage count")
    if all(d <= 0 for d in diffs):
        return Hint("countdown", "only went down: a countdown, timer or a level that is draining")
    distinct = sorted(set(v))
    if len(distinct) <= 3 and all(bin((a ^ b) & 0xFFFFFFFF).count("1") <= 2 for a in distinct for b in distinct):
        return Hint("flags", "a few values that differ in only 1-2 bits: status flags, not a number", oee="run state")
    return Hint("measurement", "moves up and down: an analog value (temperature, speed, current ...)", predictive="trend")


def classify_bits(series_by_bit):
    """Bits of an input/output/marker word that changed: {(byte, bit): [0, 1, 0 ...]} (oldest first)."""
    if not series_by_bit:
        return Hint("idle", "no bit changed during the scan")
    states, blinkers = [], []
    for (byte, bit), series in sorted(series_by_bit.items()):
        n = len(series)
        edges = sum(1 for a, b in zip(series, series[1:]) if a != b)
        (blinkers if n >= 3 and edges >= max(2, (n - 1) * 0.7) else states).append(f"{byte}.{bit}")
    if states:
        extra = f"; {len(blinkers)} bit(s) blink every snapshot" if blinkers else ""
        return Hint("state", f"bit {', '.join(states)} changed a few times: a state (run/stop, sensor, button){extra}", oee="run state")
    return Hint("blink", f"bit {', '.join(blinkers)} changes at every snapshot: a clock bit or a flashing lamp, ignore it")


def matches(hint, wanted):
    """For the filter: wanted is 'oee', 'predictive' or 'all'."""
    if wanted == "oee":
        return bool(hint and hint.oee)
    if wanted == "predictive":
        return bool(hint and hint.predictive)
    return True
