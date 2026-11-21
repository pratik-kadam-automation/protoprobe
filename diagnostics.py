"""diagnostics.py -- what a person sends to support instead of describing a problem in chat: versions, this PC's network addresses, the
connections' settings (no passwords, no tag values) and the last log lines. Pure functions plus a small in-memory log buffer.

Read-only: it changes nothing. IP and MAC addresses can be hidden before the text leaves the PC (the default in the app).
"""
import collections
import logging
import platform
import re
import sys
import time

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_SECRET_KEY = re.compile(r"pass(word|wd)?|secret|token|api[_-]?key|private[_-]?key|credential", re.I)


class RingHandler(logging.Handler):
    """Keeps the last `size` log lines in memory (no file, no disk growth)."""

    def __init__(self, size=400):
        super().__init__(level=logging.INFO)
        self.lines = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record):
        try:
            self.lines.append(self.format(record))
        except Exception:
            pass


def hide_addresses(text):
    return _MAC.sub("xx:xx:xx:xx:xx:xx", _IPV4.sub("x.x.x.x", text))


def build(app_name, app_version, connections, log_lines=(), adapters=(), is_admin=None, hide_ips=True):
    """The diagnostics text. `connections` are objects with name, protocol, plc_type, interface (dict), tags (list), status, enabled."""
    lines = [f"{app_name} diagnostics", f"created: {time.strftime('%Y-%m-%d %H:%M:%S')}", f"version: {app_version}",
             f"python: {sys.version.split()[0]}", f"system: {platform.platform()}",
             f"administrator: {'unknown' if is_admin is None else 'yes' if is_admin else 'no'}", "",
             "This PC's network addresses (address/mask):"]
    lines += [f"  {ip}/{mask}" for ip, mask in adapters] or ["  (could not be read)"]
    lines += ["", f"Connections ({len(connections)}):"]
    for c in connections:
        iface = {k: v for k, v in (c.interface or {}).items() if not _SECRET_KEY.search(str(k))}
        lines.append(f"  - {c.name}: protocol={c.protocol} type={getattr(c, 'plc_type', None)} enabled={getattr(c, 'enabled', True)} "
                     f"tags={len(c.tags)} status={c.status}")
        lines.append("      settings: " + ", ".join(f"{k}={v}" for k, v in iface.items()))
    recent = [f"  {l}" for l in list(log_lines)[-200:]]
    lines += ["", "Recent log lines:"] + (recent or ["  (none)"])
    text = "\n".join(lines)
    return hide_addresses(text) if hide_ips else text
