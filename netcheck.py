"""Is this PC on the same network as the PLC? Plain-words hints for a read that timed out.

Read-only: it only reads this PC's own address list (`ipconfig` on Windows, `ip` elsewhere). It never changes a network setting.
Parsing is by shape (an IPv4 followed by a valid mask), not by the words of a language, so a German or Hindi Windows works too.
"""
import ipaddress
import re
import subprocess

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")


def _valid_ip(text):
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        return None


def _valid_mask(text):
    """A real subnet mask (contiguous ones), or None. 0.0.0.0 is NOT valid for a host."""
    try:
        value = int(ipaddress.IPv4Address(text))
    except ValueError:
        return None
    if value == 0:
        return None
    inverted = (~value) & 0xFFFFFFFF
    return text if (inverted & (inverted + 1)) == 0 else None


def parse_ipconfig(text):
    """[(ip, mask)] from `ipconfig` output. Each IPv4 address is paired with the next value in the same adapter block that is a valid mask
    (the mask line follows the address line); gateway and DNS lines are ignored. A mask of 0.0.0.0 is kept as "0.0.0.0" so it can be reported."""
    adapters = []
    pending = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")):           # a new adapter heading
            pending = None
            continue
        found = _IPV4.findall(line)
        if not found:
            continue
        value = found[0]
        if pending is None:
            ip = _valid_ip(value)
            if ip and ip != "0.0.0.0":
                pending = ip
        else:
            if value == "0.0.0.0" or _valid_mask(value):
                adapters.append((pending, value))
                pending = None
            else:                                       # not a mask (gateway, DNS ...): that address was not an adapter address
                pending = None
                ip = _valid_ip(value)
                if ip and ip != "0.0.0.0":
                    pending = ip
    return adapters


def parse_ip_addr(text):
    """[(ip, mask)] from `ip -4 -o addr` output (Linux): `2: eth0    inet 192.168.1.5/24 brd ...`."""
    out = []
    for m in re.finditer(r"inet (\d{1,3}(?:\.\d{1,3}){3})/(\d{1,2})", text or ""):
        ip, bits = m.group(1), int(m.group(2))
        if ip.startswith("127.") or bits > 32:
            continue
        out.append((ip, str(ipaddress.IPv4Network(f"0.0.0.0/{bits}").netmask)))
    return out


def local_adapters(runner=None, hidden_kwargs=None, platform=None):
    """This PC's (ip, mask) pairs, [] if they cannot be read. `runner` replaces subprocess.run in tests."""
    import sys
    windows = (platform or sys.platform).startswith("win")
    cmd = ["ipconfig"] if windows else ["ip", "-4", "-o", "addr"]
    try:
        res = (runner or subprocess.run)(cmd, capture_output=True, text=True, timeout=10, **(hidden_kwargs or {}))
    except Exception:
        return []
    text = getattr(res, "stdout", "") or ""
    return parse_ipconfig(text) if windows else parse_ip_addr(text)


def same_network(a, b, mask):
    try:
        net = ipaddress.IPv4Network(f"{a}/{mask}", strict=False)
        return ipaddress.IPv4Address(b) in net
    except ValueError:
        return False


def network_hint(plc_ip, adapters):
    """One short plain-words hint, or "" when nothing useful can be said. `adapters` is [(ip, mask)] of this PC."""
    plc = _valid_ip((plc_ip or "").strip())
    if not plc or not adapters:
        return ""
    for ip, mask in adapters:
        if mask == "0.0.0.0":
            return (f"This PC has the address {ip} with the subnet mask {mask}, which is not valid (0.0.0.0 means 'no network'), so Windows "
                    f"drops the traffic. Set the mask to 255.255.255.0 in the adapter's IPv4 properties.")
    usable = [(ip, m) for ip, m in adapters if m and not ip.startswith("169.254.")]
    if not usable:
        return ("This PC has no usable address on a wired network (only 169.254.x.x, i.e. no address was given). Give the Ethernet adapter a "
                f"fixed IP in the PLC's range, e.g. {'.'.join(plc.split('.')[:3])}.xxx with mask 255.255.255.0 (xxx not the PLC's own number).")
    for ip, mask in usable:
        if same_network(ip, plc, mask):
            return (f"This PC ({ip}, mask {mask}) is in the same network as {plc}, so the subnet is not the problem: check the port, the PLC's "
                    "Ethernet parameters (written and power-cycled) and any firewall.")
    prefix = ".".join(plc.split(".")[:3])
    ips = ", ".join(f"{ip}/{mask}" for ip, mask in usable)
    return (f"This PC ({ips}) is NOT in the same network as {plc}. Set the Ethernet adapter to a fixed IP such as {prefix}.xxx "
            f"(xxx different from the PLC's last number) with mask 255.255.255.0, then try again.")
