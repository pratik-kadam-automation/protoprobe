"""Connection doctor: why does ProtoProbe not read this Mitsubishi PLC? Runs the checks one would do by hand and says the reason in words.

READ-ONLY. Sends one ping, one SLMP read of D0 per port and transport (the same frame as slmp_probe), reads this PC's own addresses.
It changes no setting on the PC or the PLC and never writes. The report is a plain text a person can read, copy or send.
Everything that touches the network is injectable so the logic is tested without a PLC.
"""
import re
import subprocess
import sys
import time

import netcheck
import slmp_probe

EXTRA_PORTS = tuple(range(5000, 5011))


def ping(ip, runner=None, hidden_kwargs=None, platform=None):
    """True if the host answered a ping. A reply from a router saying 'unreachable' does not count (no TTL= in it)."""
    windows = (platform or sys.platform).startswith("win")
    cmd = ["ping", "-n", "2", "-w", "1000", ip] if windows else ["ping", "-c", "2", "-W", "1", ip]
    try:
        res = (runner or subprocess.run)(cmd, capture_output=True, text=True, timeout=15, **(hidden_kwargs or {}))
    except Exception:
        return None                                   # ping could not be run (None = unknown, not "down")
    out = (getattr(res, "stdout", "") or "").upper()
    return "TTL=" in out


def ports_to_test(port):
    ports = []
    for p in ((port,) if isinstance(port, int) else ()) + EXTRA_PORTS:
        if p not in ports:
            ports.append(p)
    return ports


def run(ip, port=None, transport="tcp", adapters=None, ping_fn=None, probe_fn=None, progress=None):
    """Returns {"steps": [(title, text)], "verdict": [lines], "results": [...]}."""
    steps = []
    note = progress or (lambda text: None)

    note("Reading this PC's addresses...")
    adapters = adapters if adapters is not None else netcheck.local_adapters()
    hint = netcheck.network_hint(ip, adapters)
    mine = ", ".join(f"{a}/{m}" for a, m in adapters) or "could not be read"
    steps.append(("This PC's addresses", mine + ("\n" + hint if hint else "")))

    note("Ping...")
    alive = (ping_fn or ping)(ip)
    steps.append(("Ping", {True: f"{ip} answers a ping", False: f"{ip} does not answer a ping (a PLC or firewall can also block ping)",
                           None: "ping could not be run"}[alive]))

    ports = ports_to_test(port)
    note(f"Asking for D0 on {len(ports)} ports over UDP and TCP...")
    results = (probe_fn or slmp_probe.probe)(ip, ports=tuple(ports), timeout=1.0)
    lines = []
    for r in results:
        what = {"ok": "ANSWERED, D0 readable", "answered": "answered with an SLMP error (" + (r.get("text") or "") + ")",
                "timeout": "no answer", "refused": "refused", "other": r.get("text") or "other"}.get(r["status"], r["status"])
        raw = f"  reply bytes: {r['reply_hex']}" if r.get("reply_hex") else ""
        lines.append(f"{r['transport'].upper():3} {r['port']}: {what}{raw}")
    steps.append(("SLMP read of D0 (the same request ProtoProbe sends)", "\n".join(lines)))
    steps.append(("Request sent", slmp_probe.read_frame().hex() + "  (MC 3E binary, batch read, device D0, 1 point)"))

    return {"steps": steps, "verdict": verdict(ip, port, transport, hint, alive, results), "results": results}


def verdict(ip, port, transport, hint, alive, results):
    hits = [r for r in results if r["status"] in ("ok", "answered")]
    out = []
    if hint and ("not valid" in hint or "no usable address" in hint or "NOT in the same network" in hint):
        out.append("Network: " + hint)
        if not hits:
            return out
    if hits:
        b = slmp_probe.best(results)
        out.append(f"The PLC speaks SLMP on {b['transport'].upper()} port {b['port']}.")
        if b["status"] == "answered":
            out.append(f"It refused the read of D0 ({b['text']}): that is a PLC setting (device not allowed, or protection), not the network.")
        same = (b["transport"] == (transport or "tcp")) and (port is None or b["port"] == port)
        if same:
            out.append("That is what the connection is set to, so the connection should read. If ProtoProbe still does not, send this report: "
                       "it would then be a ProtoProbe problem.")
        else:
            out.append(f"The connection is set to {str(transport or 'tcp').upper()} port {port}. Edit the connection: transport {b['transport']}, port {b['port']}.")
        return out
    refused = [r for r in results if r["status"] == "refused"]
    if alive is False and not hits:
        out.append("The PLC does not answer a ping and no port answered: check the cable, the PLC's IP and that this PC is on the same network "
                   "(if the PLC blocks ping, go on with the port results below).")
    elif alive:
        out.append("The PLC answers a ping, so the cable and the address are fine. Nothing answered SLMP on any tested port, over UDP or TCP.")
    if refused:
        out.append("A refused TCP connection means the PLC was reached and nothing listens on that port (TCP): wrong port, or the PLC's Open "
                   "settings use UDP.")
    out.append("Likely causes, in this order: (1) the Open settings (MC protocol, binary, the port) were not written to the PLC or the PLC was not "
               "powered off and on afterwards; (2) the port in ProtoProbe is not the port in Open settings; (3) a firewall on this PC drops the "
               "answer. For UDP the answer comes back to a different port, so a rule for the program may be needed (the laptop's owner decides: "
               "ProtoProbe changes no firewall setting).")
    out.append("If another program on this PC can read the PLC, compare its settings (IP, port, TCP or UDP) with ProtoProbe's and send them.")
    return out


def report(result, ip):
    lines = [f"ProtoProbe connection doctor, {time.strftime('%Y-%m-%d %H:%M:%S')}", f"PLC address: {ip}", ""]
    for title, text in result["steps"]:
        lines += [title, "  " + text.replace("\n", "\n  "), ""]
    lines += ["Result"] + ["  " + v for v in result["verdict"]]
    return "\n".join(lines)
