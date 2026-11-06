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

**Test connection** (Questions tab, Mitsubishi) sends one read-only SLMP request to each common port over
UDP *and* TCP and says which one answered, then fills in the transport and port. An open port proves nothing and a
UDP port cannot be proven open by connecting, so it sends a real read and checks the reply. Some PLCs only have UDP
switched on (the CCM does), and the MC driver now speaks both: `transport: "udp"` (optional `local_ip` for a laptop
with two network cards; one retry for a lost datagram). TCP stays the default and is unchanged.

**Import tag list** (Readings tab, Mitsubishi) reads the programmer's GX Works3 label export (tab- or comma-separated:
name, data type, class, device). Names, addresses, data types and word order come from the programmer; the wizard then
only has to PROVE them: every reading is a verify reading, a wrong address stays 'unverified', and when an address is
right but the HMI shows the value scaled (3.2 on the HMI, 32 in the PLC) *Review > Apply suggested scales* sets the scale.
Bit devices, text and 64-bit floats are listed as skipped, not guessed. Roles and units are suggestions from the names.

**Siemens (TIA Portal)**: for an S7 PLC, *Import tag list* reads the PLC tag table exported to Excel (`.xlsx`; or the same
columns as text). Columns are found by their headers (Name, Data Type, Logical Address, Comment), so their order does not
matter. Absolute addresses of inputs, outputs, markers and non-optimized data blocks are read (`%IW64`, `%QD8`, `%MD20`,
`%DB1.DBD4`); the comment becomes the tag's meaning. Skipped, with the reason shown: bit and byte tags, text, tags with no
address (optimized blocks: S7-1200/1500 data blocks with "optimized block access" have no fixed address and cannot be read
by address; use a non-optimized block or OPC UA), and addresses that do not match their type. Names with spaces become
underscores (the dialog lists each rename). 32-bit values are read big-endian (ABCD), as S7 stores them. Reading S7-1200/1500
needs "Permit access with PUT/GET" enabled in the CPU. The `.xlsx` is read with the standard library, nothing to install.

**Practice mode** (checkbox, top right) runs the whole flow against a simulated PLC with hidden tags and
decoy values, so you can rehearse without hardware.

Names: a PLC does not serve the programmer's tag names over SLMP, S7 or Modbus; they live in the project. The names
come from that project's export, or from OPC UA browsing where the PLC has an OPC UA server. The TIA Portal importer is built; the SIMATIC Manager (symbol table) importer is not.

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
slmp_probe.py           "Is this PLC really answering SLMP, on which port and transport?" (read-only)
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

## Command line tool (`plcread.py`)

No screen, no pack, no dashboard: connect, check, read. Read-only.

```
python plcread.py probe 192.168.100.13
python plcread.py read --make mitsubishi --ip 192.168.100.13 --tags labels.tsv
python plcread.py read --make siemens --ip 10.0.0.5 --tags plc_tags.xlsx --samples 5 --interval 2 --csv
python plcread.py raw  --make mitsubishi --ip 192.168.100.13 --start D612 --count 40
```

- `probe` finds which port and transport (UDP or TCP) answer SLMP.
- `read` prints name, address, type, live value and flags for every tag in a GX Works3 label export or TIA Portal Excel tag table. Mitsubishi finds the UDP/TCP port itself unless you give `--transport` and `--port`.
- `raw` shows registers (unsigned, signed, hex, and plausible float32 readings) so you can check an address by eye.
- Flags: `NO DATA`, `BAD FLOAT`, `SUSPECT` (absurd float, usually wrong word order, try `--byte-order`), `FLAT` (unchanged over 3+ samples).
- Exit code: 0 fine, 2 some tags flagged, 1 could not connect or read.
- Two network cards? Add `--local-ip` with this computer's address on the PLC network.
