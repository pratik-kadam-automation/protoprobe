"""evidence.py -- what a probing session hands to an analyst (a person, the dashboard's Commissioning page, an AI service): the questionnaire answers,
the candidate tags and their evidence, and the matched documentation text, with every real identifier taken out.

Pure functions. Principles:
  * Data minimisation: only statistics-level facts and short texts are included; raw recordings never are.
  * Aliases: tags are `T001`, `T002` ...; the alias -> real address map stays on this computer (returned separately, never inside the payload).
  * Redaction: IP addresses, e-mail addresses, MAC addresses, URLs and the names the person lists (client, site, machine) are replaced in every free text.
  * Never included, whatever is asked: client, gateway/deployment/machine IDs, machine name, tag prefix, IPs, ports, the free-text notes.
  * The caller shows `preview()` and a person approves it before anything leaves the computer.
"""
import json
import re

NEVER_SEND = {"client", "gateway_id", "deployment_id", "machine_id", "machine_name", "prefix", "plc_ip", "plc_port", "local_ip", "notes",
              "counters_reset", "shift_counter", "db_numbers", "unit_id", "rack", "slot", "slmp_transport", "byte_order_known"}
# What an analysis needs to know about the machine, with why (used to ask the person for what is missing).
ANALYSIS_FIELDS = [
    ("machine_type", "What kind of machine is it?", "decides which signals to look for"),
    ("production_type", "Is production discrete, batch or continuous?", "decides what a production count means"),
    ("machine_use", "What does the machine do or make?", "gives the names and comments their meaning"),
    ("machine_make", "Who made the machine?", "helps recognise typical signals"),
    ("machine_model", "Which machine model?", "helps recognise typical signals"),
    ("plc_make", "Which PLC make?", "decides how addresses are read"),
    ("plc_model", "Which PLC model?", "decides which memory areas exist"),
    ("hmi_make", "Which HMI, if any?", "an HMI shows the numbers a person can compare against"),
    ("ideal_cycle_s", "What is the ideal cycle time in seconds?", "tells whether a counter's rate is plausible"),
]
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_URL = re.compile(r"\bhttps?://\S+", re.I)


def missing_for_analysis(answers):
    """[{key, question, why}] for what an analysis would like to know and the person has not said."""
    return [{"key": k, "question": q, "why": why} for k, q, why in ANALYSIS_FIELDS if not str(answers.get(k, "")).strip()]


def redact(text, names=()):
    """Replace IPs, e-mails, MACs, URLs and the listed names (any case) in a free text."""
    text = str(text or "")
    text = _URL.sub("[url]", text)
    text = _EMAIL.sub("[email]", text)
    text = _MAC.sub("[mac]", text)
    text = _IPV4.sub("[ip]", text)
    for name in sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True):
        text = re.sub(re.escape(name), "[name]", text, flags=re.I)
    return text


def raw_excerpt(series, max_points=60):
    """{"t": [], "v": [...]}: the numbers seen during the scan, evenly thinned to at most max_points (first and last kept), or None.
    Numbers only: no text, no names. The dashboard keeps it so a person can check the claims made about the tag; it never goes into an AI prompt."""
    nums = []
    for v in series or []:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and abs(f) != float("inf"):
            nums.append(int(f) if f == int(f) and abs(f) < 1e15 else f)
    if len(nums) < 2 or max_points < 2:
        return None
    if len(nums) > max_points:
        step = (len(nums) - 1) / (max_points - 1)
        nums = [nums[round(i * step)] for i in range(max_points)]
    return {"t": [], "v": nums}


def leaks(text, names=()):
    """What a final text still shows that should have been removed: a list of plain sentences (empty = clean). A last check before saving."""
    found = []
    for rx, what in ((_URL, "a web address"), (_EMAIL, "an e-mail address"), (_MAC, "a MAC address"), (_IPV4, "an IP address")):
        if rx.search(text):
            found.append(f"the text contains {what}")
    low = text.lower()
    for name in sorted({n.strip() for n in names if n and len(n.strip()) >= 2}):
        if name.lower() in low:
            found.append(f'the text contains "{name}"')
    return found


def build(answers, candidate_rows, docs=None, hide_names=(), app_version="", raw_points=0):
    """(payload, alias_map). `candidate_rows` are candidates.rows_from_results() rows; `docs` maps an address to {"name": ..., "comment": ...}
    from the imported Excel/PDF (optional). The payload is safe to show and to send; the alias map must stay local."""
    machine = {k: redact(v, hide_names) for k, v in answers.items() if k not in NEVER_SEND and str(v).strip() and k in
               {f[0] for f in ANALYSIS_FIELDS} | {"hmi"}}
    tags, alias_map = [], {}
    for i, row in enumerate(candidate_rows, start=1):
        alias = f"T{i:03d}"
        alias_map[alias] = {"connection": row["connection"], "address": row["address"]}
        doc = (docs or {}).get(row["address"]) or {}
        tags.append({
            "alias": alias, "data_type": row["data_type"], "role_guess": row["role"], "oee_use_guess": row["oee_use"],
            "predictive_use_guess": row["predictive_use"], "behaviour": redact(row["evidence"], hide_names),
            "snapshots": row["snapshots"], "first_value": row["first_value"], "last_value": row["last_value"],
            "documented_name": redact(doc.get("name", ""), hide_names), "documented_comment": redact(doc.get("comment", ""), hide_names),
        })
        excerpt = raw_excerpt(row.get("series"), raw_points) if raw_points else None
        if excerpt:
            tags[-1]["raw"] = excerpt
    payload = {
        "schema": 1, "kind": "protoprobe-evidence", "app_version": app_version, "machine": machine, "tags": tags,
        "missing_answers": [m["key"] for m in missing_for_analysis(answers)],
        "privacy": {"aliases": True, "redacted": ["ip", "email", "mac", "url", "listed names"], "raw_recordings_included": False,
                    "raw_excerpt_points": int(raw_points or 0)},
    }
    return payload, alias_map


def preview(payload):
    """The exact text a person approves before anything is sent. Lists of numbers (the raw excerpts) are kept on one line so the text stays readable."""
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return re.sub(r"\[\s*((?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?,\s*)*-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*\]",
                  lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", text)
