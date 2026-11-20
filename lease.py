"""lease.py -- the offline side of a signed lease (the `lease/1` contract with the dashboard backend, which signs it: industrial-backend/licence.js).

A field kit or an on-prem agent carries `lease.json` ({"lease": {...}, "sig": "..."}) and `kit.json` ({"trusted_keys": {kid: raw Ed25519 public key, base64}}).
This module checks the signature, keeps time WITHOUT the server (a clock that never goes back), and says what is allowed in each state:

    active -> warning (a day left for a field kit, a week for on-prem) -> expired -> removal_due (field kit only, after the grace days)

    field kit, expired    mode 'disable'      only: export, license, renew
    on-prem,  expired     mode 'buffer_only'  only: buffer (keep collecting and holding data locally), export, license, renew
                                              (a paying customer must never lose data to a lapsed lease: no screens, no analysis, no configuration)

No kit.json beside the program = an unmanaged copy (the owner's own PC, the public source): nothing is enforced. This is a hygiene control, not copy protection:
the source is public, so anyone can edit it out. Verification uses the `cryptography` package, imported only when a managed kit is checked.
"""
import base64
import datetime
import json
import os
import time

STATE_FILE = "lease-state.json"
WARN_S = {"field-kit": 24 * 3600, "on-prem": 7 * 24 * 3600}
ACTIONS_WHEN_EXPIRED = {"disable": {"export", "license", "renew"}, "buffer_only": {"buffer", "export", "license", "renew"}}


class LeaseError(Exception):
    pass


def canonical(value):
    """The text the backend signed: keys sorted at every level, no spaces (identical to licence.js `canonical`)."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(value[k]) for k in sorted(value)) + "}"
    if isinstance(value, list):
        return "[" + ",".join(canonical(v) for v in value) + "]"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def verify(container, trusted_keys):
    """The lease dict if its signature is good; raises LeaseError (with a plain reason) otherwise."""
    if not isinstance(container, dict) or not isinstance(container.get("lease"), dict) or not isinstance(container.get("sig"), str):
        raise LeaseError("This is not a lease file.")
    lease = container["lease"]
    raw = (trusted_keys or {}).get(lease.get("kid"))
    if not raw:
        raise LeaseError(f"The lease was signed with an unknown key ({lease.get('kid')!r}).")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise LeaseError("The 'cryptography' package is needed to check a lease: python -m pip install cryptography") from exc
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(raw)).verify(base64.b64decode(container["sig"]), canonical(lease).encode("utf-8"))
    except (InvalidSignature, ValueError):
        raise LeaseError("The lease signature is not valid: the file was changed or is not from your dashboard.")
    return lease


def _ts(text):
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class Clock:
    """Time that never goes back. It remembers the latest time it has seen (a file, saved at most once a minute). If the PC's clock is behind that, it
    keeps counting from the remembered time by the seconds that really passed while running, so setting the clock back does not extend a lease."""
    def __init__(self, state_path=None, wall=time.time, mono=time.monotonic, save_every_s=60):
        self.path, self.wall, self.mono, self.save_every = state_path, wall, mono, save_every_s
        self.high = None
        self.last_mono = None
        self.last_save = None
        if state_path and os.path.exists(state_path):
            try:
                self.high = float(json.load(open(state_path)).get("high_water"))
            except (OSError, ValueError, TypeError):
                self.high = None

    def now(self):
        w, m = self.wall(), self.mono()
        if self.high is None or w >= self.high:
            eff = w
        else:
            eff = self.high + (m - self.last_mono if self.last_mono is not None else 0.0)       # the clock went back: advance by real elapsed time
        self.high, self.last_mono = max(eff, self.high or 0.0), m
        if self.path and (self.last_save is None or m - self.last_save >= self.save_every):
            self._save(m)
        return eff

    def _save(self, m):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"high_water": self.high}, f)
            os.replace(tmp, self.path)
            self.last_save = m
        except OSError:
            pass

    def flush(self):
        if self.path:
            self._save(self.mono())


def status(lease, now_ts):
    """{state, seconds_left, mode, message}: state is active | warning | expired | removal_due."""
    kind = lease.get("kind")
    expires = _ts(lease["expires_at"])
    left = expires - now_ts
    mode = lease.get("mode_on_expiry", "disable")
    grace = float(lease.get("grace_days", 3)) * 86400
    when = datetime.datetime.fromtimestamp(expires, datetime.timezone.utc).strftime("%d %b %Y %H:%M UTC")
    if left > WARN_S.get(kind, 24 * 3600):
        state, message = "active", f"Licensed until {when}."
    elif left > 0:
        state, message = "warning", f"The license ends on {when}. Get a new kit from the dashboard before then (needs the VPN)."
    elif kind == "field-kit" and now_ts > expires + grace:
        state, message = "removal_due", f"The license expired on {when} and the grace period is over. Export your data now: the program is due to be removed."
    elif mode == "buffer_only":
        state, message = "expired", f"The license expired on {when}. Data is still collected and held on this computer; screens, analysis and configuration are off until it is renewed."
    else:
        state, message = "expired", f"The license expired on {when}. To keep using this tool, get a new kit from the dashboard (needs the VPN). You can still export your data."
    return {"state": state, "seconds_left": left, "mode": mode, "message": message}


def allowed(state_info, action):
    """May `action` (scan, read, connect, analyse, configure, script, buffer, export, license, renew ...) run in this state?"""
    if state_info["state"] in ("active", "warning"):
        return True
    if state_info["state"] == "removal_due":
        return action in ACTIONS_WHEN_EXPIRED["disable"]
    return action in ACTIONS_WHEN_EXPIRED.get(state_info["mode"], ACTIONS_WHEN_EXPIRED["disable"])


class Manager:
    """What the app asks: `current()` -> status dict, or None for an unmanaged copy. `check(action)` -> (ok, message)."""
    def __init__(self, folder, clock=None):
        self.folder = folder
        self.clock = clock or Clock(os.path.join(folder, STATE_FILE))

    def managed(self):
        return os.path.exists(os.path.join(self.folder, "kit.json"))

    def current(self):
        if not self.managed():
            return None
        try:
            keys = json.load(open(os.path.join(self.folder, "kit.json")))["trusted_keys"]
            container = json.load(open(os.path.join(self.folder, "lease.json")))
            lease = verify(container, keys)
        except (OSError, ValueError, KeyError) as exc:
            return {"state": "invalid", "seconds_left": 0, "mode": "disable", "message": f"No valid lease: {exc}. Get a new kit from the dashboard."}
        except LeaseError as exc:
            return {"state": "invalid", "seconds_left": 0, "mode": "disable", "message": str(exc)}
        info = status(lease, self.clock.now())
        info["kind"], info["entitlement"] = lease.get("kind"), lease.get("entitlement")
        return info

    def check(self, action):
        info = self.current()
        if info is None:
            return True, ""
        if info["state"] == "invalid":
            return action in ACTIONS_WHEN_EXPIRED["disable"], info["message"]
        return allowed(info, action), info["message"]
