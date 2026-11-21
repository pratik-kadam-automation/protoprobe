"""workspace.py -- the file that remembers a person's connections and tags between sessions.

Pure functions, no screens. Rules (so the file can be trusted):
  * versioned: `schema` is checked on load; a file from a NEWER schema is never overwritten or guessed at.
  * written atomically (temp file, then replace) and the previous copy is kept as `workspace.json.bak`: a crash or a full disk cannot leave half a file.
  * a damaged file is moved aside (`.corrupt-<time>`) and the app starts empty instead of crashing; nothing is deleted.
  * no secrets: keys that look like a password, token or key are not written (a person re-enters them), and the file lives in the user's own profile,
    never in the repository.
"""
import datetime
import json
import os
import re

SCHEMA = 1
SECRET_KEY = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential", re.I)
POLL_MIN_S, POLL_MAX_S, POLL_DEFAULT_S = 0.1, 3600.0, 2.0


def default_path(env=None, platform=None):
    """%APPDATA%\\ProtoProbe\\workspace.json on Windows, ~/.protoprobe/workspace.json elsewhere; PROTOPROBE_HOME overrides (tests, portable use)."""
    env = os.environ if env is None else env
    if env.get("PROTOPROBE_HOME"):
        return os.path.join(env["PROTOPROBE_HOME"], "workspace.json")
    platform = platform or os.name
    if platform == "nt" and env.get("APPDATA"):
        return os.path.join(env["APPDATA"], "ProtoProbe", "workspace.json")
    return os.path.join(os.path.expanduser("~"), ".protoprobe", "workspace.json")


def _clean_interface(interface):
    return {k: v for k, v in (interface or {}).items() if not SECRET_KEY.search(str(k))}


def snapshot(connections, app_version="", extra=None):
    """The dict to write. `connections` are the app's Connection objects (name, protocol, interface, tags, plc_type, enabled, poll_interval_s)."""
    return {
        "schema": SCHEMA, "app": "ProtoProbe", "app_version": app_version,
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "connections": [{
            "name": c.name, "protocol": c.protocol, "plc_type": getattr(c, "plc_type", None),
            "enabled": bool(getattr(c, "enabled", True)), "poll_interval_s": float(getattr(c, "poll_interval_s", POLL_DEFAULT_S)),
            "interface": _clean_interface(c.interface),
            "tags": [{k: v for k, v in t.items() if not str(k).startswith("_")} for t in c.tags],
        } for c in connections],
        "settings": dict(extra or {}),
    }


def restore(data, known_protocols):
    """(connection dicts, warnings) from a loaded file. Raises ValueError for a schema this version does not know."""
    if not isinstance(data, dict) or "schema" not in data:
        raise ValueError("this is not a ProtoProbe workspace file")
    if data["schema"] > SCHEMA:
        raise ValueError(f"this workspace was saved by a newer ProtoProbe (file format {data['schema']}, this one reads up to {SCHEMA}): "
                         "update ProtoProbe, or the file would be damaged")
    out, warnings, seen = [], [], set()
    for raw in data.get("connections", []):
        name = str(raw.get("name", "")).strip()
        if not name or name in seen:
            warnings.append(f"a connection without a name, or named twice ({name or 'no name'}), was left out")
            continue
        if raw.get("protocol") not in known_protocols:
            warnings.append(f"connection {name}: unknown protocol {raw.get('protocol')!r}, left out")
            continue
        try:
            poll = min(POLL_MAX_S, max(POLL_MIN_S, float(raw.get("poll_interval_s", POLL_DEFAULT_S))))
        except (TypeError, ValueError):
            poll = POLL_DEFAULT_S
        tags = []
        for t in raw.get("tags", []):
            if isinstance(t, dict) and t.get("name") and all(t["name"] != x["name"] for x in tags):
                tags.append(dict(t))
            else:
                warnings.append(f"connection {name}: a tag without a name, or named twice, was left out")
        seen.add(name)
        out.append({"name": name, "protocol": raw["protocol"], "plc_type": raw.get("plc_type") or None,
                    "enabled": bool(raw.get("enabled", True)), "poll_interval_s": poll,
                    "interface": dict(raw.get("interface") or {}), "tags": tags})
    return out, warnings


def save_atomic(path, data):
    """Write the file safely and keep the previous one as .bak."""
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)
    return text


def load(path):
    """(data, message): the parsed file, or (None, why) when there is none or it is damaged (a damaged file is moved aside, never deleted)."""
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except (OSError, ValueError) as exc:
        aside = f"{path}.corrupt-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        try:
            os.replace(path, aside)
            return None, f"The saved workspace could not be read ({exc}); it was kept as {os.path.basename(aside)} and ProtoProbe started empty."
        except OSError:
            return None, f"The saved workspace could not be read ({exc})."
