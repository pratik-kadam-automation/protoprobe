"""Find tag candidates in a file the engineer already has: an Excel/CSV tag list or export, or a PDF (a printed tag list, a
programme listing, an I/O list). Reads only text; nothing here talks to a PLC or the network.

What it can and cannot do (say this in the screen too):
- It finds LINES that carry an address (Siemens %I0.0 / %MW10 / DB1.DBW4, Mitsubishi D100, Modbus 40001) plus a name and a comment.
- A PDF cannot be turned back into ladder logic, and a scanned PDF (a picture of paper) has no text at all: it is refused with a message.
- Every candidate is a GUESS to confirm on the machine. The ranking comes only from the words in the name/comment.
"""
import csv
import io
import os
import re

import s7_address

# --- address patterns -------------------------------------------------------------------------------------------
_S7_ABS = re.compile(r"^%?([IQM])([BWD]?)(\d+)(?:\.([0-7]))?$", re.I)
_S7_DB = re.compile(r"^%?DB(\d+)\.DB([XBWD])(\d+)(?:\.([0-7]))?$", re.I)
_MC = re.compile(r"^([DWRM])(\d{1,5})$")
_MODBUS = re.compile(r"^([34])(\d{4})$")
_S7_TYPES = {"int": "int16", "uint": "uint16", "word": "uint16", "dint": "int32", "udint": "uint32", "dword": "uint32",
             "real": "float32_be"}
_TYPE_WORDS = {"bool", "byte", "int", "uint", "word", "dint", "udint", "dword", "real", "lreal", "string", "char", "time", "sint", "usint"}

# --- ranking by words -------------------------------------------------------------------------------------------
_ROLES = (
    ("state", "run state / status (OEE availability)", ("run", "running", "status", "state", "auto", "manual", "fault", "alarm", "error",
                                                         "stop", "stopped", "ready", "idle", "e-stop", "estop", "emergency", "mode", "start")),
    ("production", "production count (OEE performance / quality)", ("count", "counter", "qty", "quantity", "pieces", "parts", "produced",
                                                                     "good", "reject", "scrap", "bad", "cycle", "total", "batch", "output")),
    ("predictive", "condition value (predictive)", ("temp", "temperature", "pressure", "vibration", "current", "speed", "rpm", "hours",
                                                    "torque", "voltage", "power", "flow", "level", "load", "frequency", "amps")),
)


def rank(text):
    """(role, why, score) from the words of a name/comment. Letters-only word match, so 'Machine_Running' finds 'running'."""
    words = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "").lower()))
    best = ("", "", 0)
    for role, why, keys in _ROLES:
        hit = sorted(words & set(keys))
        if hit and len(hit) > best[2]:
            best = (role, f"{why}: the text says {', '.join(hit)}", len(hit))
    return best


# --- one address -> a tag --------------------------------------------------------------------------------------
def parse_address(addr, name, type_text=""):
    """(protocol, tag dict, None) or (protocol or None, None, why_not)."""
    a = (addr or "").strip().replace(" ", "")
    dtype = _S7_TYPES.get((type_text or "").strip().lower())
    is_bool = (type_text or "").strip().lower() == "bool"
    m = _S7_DB.match(a)
    if m:
        db, w, byte, bit = int(m.group(1)), m.group(2).upper(), int(m.group(3)), m.group(4)
        return _s7(name, "DB", db, w, byte, bit, dtype)
    m = _S7_ABS.match(a)
    if m:
        area, w, byte, bit = m.group(1).upper(), m.group(2).upper() or ("X" if m.group(4) is not None else ""), int(m.group(3)), m.group(4)
        if not w:
            return "s7comm", None, "an address like I0 / M10 without B, W, D or a bit cannot say how wide it is"
        return _s7(name, area, 0, w, byte, bit, dtype)
    m = _MC.match(a.upper())
    if m:
        code, n = m.group(1), int(m.group(2))
        return "mc_protocol_3e", {"name": name, "device_code": code, "device_number": n, "data_type": "int16", "multiplier": 1.0,
                                  "offset": 0.0, "mqtt_key": name}, None
    m = _MODBUS.match(a)
    if m:
        rtype = "holding" if m.group(1) == "4" else "input"
        return "modbus_tcp", {"name": name, "address": int(m.group(2)) - 1, "register_type": rtype, "data_type": "int16",
                              "multiplier": 1.0, "offset": 0.0, "mqtt_key": name}, None
    if is_bool:
        return None, None, "not an address ProtoProbe understands"
    return None, None, "not an address ProtoProbe understands"


def _s7(name, area, db, letter, byte, bit, dtype):
    width = {"X": "bit", "B": "byte", "W": "word", "D": "dword"}[letter]
    if width == "bit" and bit is None:
        return "s7comm", None, "a bit address needs a bit number (0 to 7)"
    try:
        return "s7comm", s7_address.build_tag(name, area, width, db, byte, bit or 0, data_type=dtype if dtype in s7_address.data_types_for(width) else None), None
    except ValueError as exc:
        return "s7comm", None, str(exc)


def _clean_name(raw):
    return re.sub(r"[^A-Za-z0-9_.\-\[\]]+", "_", str(raw).strip()).strip("_")[:100]


def _is_address(tok):
    t = tok.strip().replace(" ", "")
    return bool(_S7_ABS.match(t) or _S7_DB.match(t) or _MC.match(t.upper()) or _MODBUS.match(t))


# --- candidates -------------------------------------------------------------------------------------------------
def _candidate(name, address, type_text, comment, source):
    name = _clean_name(name) or _clean_name(address)
    proto, tag, why_not = parse_address(address, name, type_text)
    role, why, score = rank(f"{name} {comment}")
    return {"name": name, "address": address.strip(), "type": (type_text or "").strip(), "comment": (comment or "").strip()[:200],
            "protocol": proto, "tag": tag, "why_not": why_not, "role": role, "why": why, "score": score, "source": source}


_HEAD = {
    "name": ("name", "symbol", "tag", "tag name", "variable", "signal", "designation", "symbolic name", "label"),
    "address": ("address", "logical address", "absolute address", "operand", "addr", "device", "register", "io address"),
    "type": ("data type", "datatype", "type", "format"),
    "comment": ("comment", "description", "remark", "remarks", "note", "notes", "text", "meaning", "function"),
}


def from_table(rows, source="table"):
    """Candidates from rows of text. The header row is found by its column names (any order); without one the rows are read as text lines."""
    rows = [[str(c).strip() for c in r] for r in rows if any(str(c).strip() for c in r)]
    for i, r in enumerate(rows[:15]):
        low = [c.lower() for c in r]
        col = {k: next((low.index(h) for h in hs if h in low), None) for k, hs in _HEAD.items()}
        if col["address"] is not None and (col["name"] is not None or col["comment"] is not None):
            out = []
            for n, row in enumerate(rows[i + 1:], start=i + 2):
                get = lambda k: row[col[k]] if col[k] is not None and col[k] < len(row) else ""
                if get("address"):
                    out.append(_candidate(get("name") or get("comment"), get("address"), get("type"), get("comment"), f"{source} row {n}"))
            return out
    return from_lines([" | ".join(r) for r in rows], source)


def from_lines(lines, source="text"):
    out = []
    for n, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        toks = [t.strip() for t in re.split(r"\s{2,}|\t|\||;", line) if t.strip()]
        if len(toks) < 2:
            toks = line.split()
        a_i = next((i for i, t in enumerate(toks) if _is_address(t)), None)
        if a_i is None:
            continue
        rest = [t for i, t in enumerate(toks) if i != a_i]
        type_text = next((t for t in rest if t.lower() in _TYPE_WORDS), "")
        rest = [t for t in rest if t.lower() not in _TYPE_WORDS]
        before = [t for t in toks[:a_i] if t.lower() not in _TYPE_WORDS]
        name = before[0] if before else (rest[0] if rest else toks[a_i])
        comment = " ".join(t for t in rest if t != name)
        out.append(_candidate(name, toks[a_i], type_text, comment, f"{source} line {n}"))
    return out


def dedupe(cands):
    seen, out = set(), []
    for c in cands:
        key = (c["name"], c["address"].upper())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# --- files ------------------------------------------------------------------------------------------------------
def pdf_lines(path):
    """Text lines of a PDF, or (None, message). Needs the pypdf package; a PDF without text (a scan) says so."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "Reading a PDF needs the 'pypdf' package (it is in requirements.txt and in the installer)."
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return None, "The PDF is password protected."
        lines = []
        for page in reader.pages:
            lines.extend((page.extract_text() or "").splitlines())
    except Exception as exc:  # pypdf raises many kinds of error for damaged files
        return None, f"The PDF could not be read: {exc}"
    if not any(l.strip() for l in lines):
        return None, "This PDF has no text in it (a scan or a picture). ProtoProbe does not read pictures: export the list to Excel/CSV, or use a text PDF."
    return lines, None


def load(path):
    """-> {"candidates": [...], "error": str|None, "kind": "pdf"|"xlsx"|"csv"}"""
    import commissioning as cm
    ext = os.path.splitext(path)[1].lower()
    base = os.path.basename(path)
    try:
        if ext == ".pdf":
            lines, err = pdf_lines(path)
            if err:
                return {"candidates": [], "error": err, "kind": "pdf"}
            return {"candidates": dedupe(from_lines(lines, "PDF")), "error": None, "kind": "pdf"}
        with open(path, "rb") as fh:
            data = fh.read()
        if ext in (".xlsx", ".xlsm"):
            return {"candidates": dedupe(from_table(cm.read_xlsx_rows(data), "Excel")), "error": None, "kind": "xlsx"}
        if ext in (".csv", ".txt", ".tsv", ".asc", ".sdf"):
            return {"candidates": dedupe(from_table(cm.rows_from_text(data.decode("utf-8-sig", "replace")), "text")), "error": None, "kind": "csv"}
        return {"candidates": [], "error": f"{base}: ProtoProbe reads .xlsx, .csv, .txt and .pdf here.", "kind": ext}
    except Exception as exc:
        return {"candidates": [], "error": f"{base} could not be read: {exc}", "kind": ext}


def sort_best_first(cands):
    return sorted(cands, key=lambda c: (-c["score"], c["tag"] is None, c["name"].lower()))


def fits(conn_protocol, cand):
    """Can this candidate become a tag of a connection with this protocol?"""
    if cand["tag"] is None:
        return False
    p = cand["protocol"]
    return p == conn_protocol or (p == "modbus_tcp" and conn_protocol == "modbus_rtu")


def add_to_connection(conn, cands, output=False):
    """Add the candidates that fit conn (new names only). With output=True every kept tag is also ticked for the output JSON under its name.
    -> (added, already_there, does_not_fit)"""
    added = already = misfit = 0
    for c in cands:
        if not fits(conn.protocol, c):
            misfit += 1
            continue
        old = next((t for t in conn.tags if t["name"] == c["tag"]["name"]), None)
        if old is not None:
            already += 1
            tag = old
        else:
            tag = dict(c["tag"])
            conn.tags.append(tag)
            added += 1
        if output:
            tag["output"] = tag.get("output") or tag["name"]
    return added, already, misfit
