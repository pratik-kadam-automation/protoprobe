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


def build(answers, candidate_rows, docs=None, hide_names=(), app_version=""):
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
    payload = {
        "schema": 1, "kind": "protoprobe-evidence", "app_version": app_version, "machine": machine, "tags": tags,
        "missing_answers": [m["key"] for m in missing_for_analysis(answers)],
        "privacy": {"aliases": True, "redacted": ["ip", "email", "mac", "url", "listed names"], "raw_recordings_included": False},
    }
    return payload, alias_map


def preview(payload):
    """The exact text a person approves before anything is sent."""
    return json.dumps(payload, indent=2, ensure_ascii=False)
