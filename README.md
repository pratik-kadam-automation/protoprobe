# ProtoProbe

Multi-vendor industrial protocol diagnostics and edge gateway configuration tool.

Built for evaluating and configuring industrial IoT edge gateways (originally for an
Atreyo AG-221/702 evaluation) against real PLCs, before committing to gateway hardware
or writing production polling logic. Talks directly to PLCs over their native
protocols, discovers live tags where the protocol allows it, and exports
gateway-ready configuration.

![ProtoProbe icon](assets/protoprobe_256.png)

## What it does

- **Multi-PLC, multi-protocol connections** — Modbus TCP/RTU, S7comm (Siemens, incl.
  DB/I/Q/M areas with bit-level addressing), MC Protocol/SLMP (Mitsubishi FX5U),
  OPC UA, and BACnet/IP, all running simultaneously.
- **Live tag browsing** where the protocol actually supports it — OPC UA and BACnet
  expose real tag/object names over the wire; browse and pick, no addresses typed
  by hand.
- **Scan Mode** for protocols with no symbol table (Modbus, S7 DB/I/Q/M, MC Protocol)
  — sweeps an address range (or the whole PLC's common memory areas at once, in the
  background), flags which addresses actually changed value across snapshots, and
  suggests likely 32-bit/float pairings — only when there's real evidence of
  movement backing the guess, not bare coincidence.
- **Search-by-known-value** — type a value you know is live on the HMI right now
  (with an adjustable tolerance) and find which address(es) produced it, across
  int16/uint16/int32/uint32/float32 (both byte orders).
- **CSV tag import** for protocols where the tag list only exists inside a vendor
  project file (TIA Portal / GX Works3 export).
- **OEE tracking** — map Run Status / Total Count / Reject Count tags (or write a
  custom formula) and watch Availability/Performance/Quality/OEE% computed live
  from actual polled data.
- **JSON output editor + MQTT/mTLS publish** — edit the exact payload shape sent to
  a broker, including AWS IoT Core-compatible mTLS (CA cert, client cert, client key).
- **Network tools** — IP and port scanner for finding PLCs on site, replacing the
  need for separate tools like Advanced IP Scanner or ModScan.
- **Commissioning wizard** — answer a few questions (PLC make, machine type, IPs, which
  counters the operator resets), type what the HMI shows at four readings, and the tool finds
  each signal's address, scale and byte order, then PROVES it with fresh readings before you leave
  site. Exports a commissioning pack that the dashboard imports (machine profile, Node-RED
  CONFIG blocks, live early-warning checks). See below.
- **Dark/light theme**, a real Windows icon and taskbar identity, and a portable
  single-file `.exe` build — no install, no admin rights, hand it to anyone.

## Commissioning wizard (Commissioning tab)

Goal: no more "that tag turned out to be wrong" on a deployed gateway weeks later.

1. **Questions** — client, machine type (CCM, singeing, CBR, stenter, furnace, other), PLC make/model/IP,
   which counters reset at the end of a batch. The answers pick the protocol, the memory to scan, which
   32-bit byte order to try first (e.g. Mitsubishi word-swapped), and which signals to ask about.
2. **Readings** — at each reading, type what the HMI shows *right now* for each signal it displays and press
   *Record reading*; the PLC memory is read once. Readings 1-2 **find** the address: a candidate must fit
   *every* reading (int16/uint16/int32/uint32/float32, four byte orders, scales 1 ... 0.001 and 10/100),
   not just one, which removes almost all coincidences. Readings 3-4 **verify** the choice.
3. **Review** — each tag with its evidence. A tag is **confirmed** only by fresh *verify* readings (two, at
   least 30 s apart, with a *different* HMI value each time; setpoints/text may repeat). Readings used to
   find an address can never also confirm it. **probable** = fits but not proved yet; **unverified** = no
   matching evidence. If two readings are too close to tell candidates apart the screen says so.
4. **Export** — `<machine>-commissioning.json` (schema `commissioning-pack/1`). In the dashboard:
   *Commissioning > Import pack*. The dashboard re-runs the same confirmation rule, so a pack cannot claim
   more than its evidence supports.

**Practice mode** (checkbox, top right) runs the whole flow against a simulated PLC with hidden tags and
decoy values, so you can rehearse without hardware.

Not covered yet: OPC UA and BACnet (they expose real tag names: use Connections > Browse), serial Modbus RTU
(create the connection first), text/string tags, bit areas other than inside words and Modbus coils.
The wizard reuses the same drivers as Scan, so it is only as proven as they are (see the table below).

Tests: `python -m unittest discover -s tests` (engine, runs anywhere). The screen test needs tkinter and a
display: `xvfb-run -a python3 -m unittest tests.test_screen_practice`. `tests/fixtures/golden-pack.json` is shared
with the dashboard's tests: both sides must give every tag the same confidence.

## Supported protocols

| Protocol | Tag discovery | Status |
|---|---|---|
| MC Protocol / SLMP (Mitsubishi FX5U) | Scan Mode only (no symbol table over the wire) | **Proven against real hardware** |
| Modbus TCP/RTU | Scan Mode only | Built, untested against real hardware |
| S7comm (Siemens, DB/I/Q/M) | Scan Mode only | DB reads built; I/Q/M area codes unverified against real hardware |
| OPC UA | Live browsing | Built, untested against real hardware |
| BACnet/IP | Live browsing | Built from a verified reference implementation, untested — no BACnet device available yet |
| Profibus | Not supported | Needs dedicated master hardware; a generic RS485 adapter cannot speak it |
| CC-Link | Not supported | Not yet built |

Where a driver is marked "untested," treat the first real read as a validation
step, not a given — confirm one known value before trusting a wide scan.

## Running from source

```
pip install pymodbus asyncua paho-mqtt sv-ttk
pip install python-snap7      # only if using S7comm
pip install bacpypes3         # only if using BACnet -- heaviest dependency

python protoprobe.py
```

## Building the standalone .exe (Windows)

```
pip install pyinstaller
pyinstaller --onefile --windowed --icon=assets\protoprobe.ico --name ProtoProbe --add-data "assets;assets" --add-data "edge-gateway;edge-gateway" protoprobe.py
```

Output: `dist\ProtoProbe.exe` — a single portable file. No installer, no admin
rights, nothing else needs to accompany it.

## Project structure

```
protoprobe.py           Main application (single file -- sidebar shell,
                         all screens, scan workers, OEE engine)
commissioning.py        Commissioning wizard engine (pure logic: questionnaire,
                         scan plan, multi-reading search, evidence gate, pack)
commissioning_screen.py The wizard's Tk screen + practice-mode simulated PLC
tests/                  Engine tests, wizard flow test, golden pack fixture
assets/                 Icon (.ico + PNG variants at several sizes)
edge-gateway/            Protocol driver library, shared with the real
    drivers/            gateway's edge_collector.py -- same drivers this
                         tool uses are what a deployed AG-221/702 (or
                         similar) gateway would run in production
    config.json          Example gateway config schema
    SETUP.md              OpenWrt/gateway deployment notes (separate from
                          this tool -- for the actual field gateway)
```

## Known limitations

- No persistent storage — scan results and connection config live only for the
  running session unless explicitly saved (Save Multi-PLC Config / CSV export).
- Float32/int32 detection is a heuristic based on observed value changes, not a
  certainty. Always confirm against the HMI or known process behavior before
  relying on a suggested data type.
- BACnet and full S7 I/Q/M support are built from documentation and a verified
  reference implementation, not proven against real hardware yet.

## License

Internal tool — not currently licensed for external distribution.
