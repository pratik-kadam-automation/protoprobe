# CLAUDE.md: read this first (ProtoProbe)

Rules and state for any Claude session or person working in this repo. Keep it current in the same pull request as any change (see "Keep this file true").

## What this is
ProtoProbe: a Windows desktop tool (Python, Tkinter) for industrial protocol diagnostics and for **commissioning a PLC on site**: it asks a few questions, reads the PLC,
asks the engineer to confirm values against the HMI at different moments, and exports a *commissioning pack* (`commissioning-pack/1`) that the private
`industrial-backend` dashboard imports (Commissioning page: machine profile, Node-RED CONFIG, live early-warning checks). Owner: P. Kadam.

## Standing rules
1. **This repository is public. Never commit real client, site, machine, gateway or tag names, IP addresses, or a real tag export.** Fixtures are synthetic (client `acme`, `GW_ACME_001_000001`).
   A slip once leaked identifiers into the first branch; history was not rewritten (force-push is denied), a clean branch replaced it.
2. Everything goes through a pull request from a branch; no force-push. The owner merges ("merge it") and **pushes version tags himself** (cloud sessions get 403 on tag push).
3. `test/fixtures/golden-pack.json` here and in `industrial-backend` must stay identical (a contract test in each repo reads its copy).
4. Report honestly what was tested: nearly everything was tested against simulated PLCs only (a local UDP/TCP SLMP simulator, a fake S7 reader, synthetic export files). Say so.
5. Readings are read-only. Never add a write to a PLC.

## Layout
`protoprobe.py` (app, version `APP_VERSION`), `commissioning.py` (engine: questions, finder, evidence gate, pack, importers), `commissioning_screen.py` (Tk wizard, practice mode),
`slmp_probe.py` (port/transport probe), `plc_reader.py` (driver reading shared by the screen and the CLI), `plcread.py` (command line: `probe`, `read`, `raw`),
`edge-gateway/drivers/` (Modbus, S7, MC/SLMP incl. UDP, OPC UA, BACnet), `packaging/` (PyInstaller build, Inno Setup installer), `.github/workflows/release.yml`.

## Checks before a PR
    python3 -m unittest discover -s tests                                   # engine, SLMP simulators, plcread, importers (S7 tests need python-snap7==3.3.1, see Traps)
    xvfb-run -a python3.12 -m unittest tests.test_screen_practice tests.test_tia_import     # Tk screens (needs python3-tk and a virtual display)
A full-app start-up check under xvfb (every screen shows) was used by hand; there is no CI for the app tests, so run them yourself.

## How a release works
Raise `APP_VERSION`, merge, then the owner pushes the tag (`git tag v0.3.1 && git push origin v0.3.1`). The *Release (Windows)* workflow runs the tests, builds `ProtoProbe.exe`, `plcread.exe`
and the installer on a Windows runner and publishes the release. The tag must equal `v` + `APP_VERSION`. Builds are not code-signed (SmartScreen warns).

## State on 2026-11-19 (evening)
- Merged to main: commissioning wizard (#1), TIA Portal importer + `plcread` (#2), SIMATIC Manager importer (#3), Windows installer/release workflow (#4), S7 area-code import fix for python-snap7 2.x/3.x (#5), this file (#6), Windows build fixes (#7, #8), release state note (#9).
- **Released:** `v0.3.0` (the *Release (Windows)* workflow ended green, reported by the owner). `v0.3.1` was tagged on 2026-11-19 (Discover DBs fix); its build result is not reported yet. The owner runs `ProtoProbe.exe` on a Windows PC and has started the first connection to a Siemens PLC (Connections > Scan for live tags). Builds are not code-signed (SmartScreen warns). Still not checked: the installer on a clean Windows machine.
- **Bug 2 found by the same use, fix on branch `fix-ip-scan-windows`, NOT merged:** Network tools > IP scanner opened a black `ping.exe` window for each of the 254 addresses, many at once, taking the focus so the app could not be used. Every ping (and any future child program) now starts hidden through `hidden_subprocess_kwargs()` (`CREATE_NO_WINDOW`), and the progress counter works (it reset on every refresh). `tests/test_ip_scan.py` (5 tests, stand-in `ping`). The same branch also fixes the "Scan for live tags" window: its fixed size hid the **Start full PLC scan** button and cut off the Discover DBs message (now on its own line, window taller); checked by rendering the window under xvfb, not tested on Windows. The owner's first look at the new message (v0.3.2 from source, 2026-11-19) showed "Done -- 0 DB(s) found ... does not exist or uses optimized block access" for a connection that did connect: DB1..DB50 not readable (PLC not identified in the report). **Not tried on Windows by the session that wrote it** (no Windows here): the owner must confirm no windows appear. `APP_VERSION` is raised to `0.3.2` on that branch; after it is merged the owner pushes tag `v0.3.2`.
- **Bug found by the first Siemens use, fixed in #10 (merged), shipped in `v0.3.1`:** "Discover DBs" showed "Probing..." for ever with the button disabled whenever the PLC connection failed (the background thread died silently). The window now shows the real error with a plain-words hint (network, rack/slot, PUT/GET, optimized block access), and a probe that finds no DB says why. `tests/test_discover_dbs.py` (6 tests, stand-in driver). An installed v0.3.0 still has the hang.
- Trap: `commissioning.TOOL` stays `"ProtoProbe 0.3.0"` on purpose: that string is inside `test/fixtures/golden-pack.json`, which must be identical in `industrial-backend`. Raise it only together with that fixture in both repos. `APP_VERSION` is independent of it.
- Trap: any new `subprocess` call in the app must pass `**hidden_subprocess_kwargs()`, or Windows opens a console window for it (the app has no console).
- Trap: `plcread probe` exits 1 when no PLC answers. The workflow's smoke-test step accepts that on purpose (`exit 0`); do not "fix" it back, a runner has no PLC.
- Trap: the S7 driver tests need `python-snap7==3.3.1` (the version this project pins). Without it `tests/test_s7_driver.py` shows 3 failures that are not a code problem. Use a virtual environment: `python3.12 -m venv v && v/bin/pip install python-snap7==3.3.1`, then run the tests with `xvfb-run -a v/bin/python -m unittest discover -s tests`.
- Not tested on real hardware: Mitsubishi FX5U over UDP in the wizard (only the SLMP frame was proven in the field with a script), a Siemens PLC beyond the first connection attempt above (result not reported yet), the GX Works3 / TIA Portal / SIMATIC Manager importers against real exports (the `.sdf`/`.asc` layouts follow the documented format).
- Not built: BACnet and OPC UA discovery in the wizard (they expose real tag names: use Connections > Browse), serial Modbus RTU in the wizard. Started: `docs/m300-protocols.md` collects the gateway's per-protocol register lists from the owner's screenshots (Modbus, DVP, FATEK, FX1S/FX2N so far) and `plc_catalog.py` turns them into data (all 29 gateway PLC types listed, 8 connectable today; no screen uses it yet; branch `plc-catalog`, not merged). Idea not started: an ARP scan (MAC addresses and vendor) next to the IP scanner: the owner mentioned an `arpscan.py` built earlier, but no such file exists in this repo or its history; ask for the file or build it fresh. Idea not started: use the gateway's own "data point" configuration (the M300 keeps the register addresses there) as a source of tags for ProtoProbe; the export format is not known yet.

## Timeline (git dates)
- 2026-08-31 licence added (all rights reserved). 2026-11-06 all the work above (PRs #1-#5), done in sessions on 2026-11-05..07.
- The wizard grew out of a field script that read an FX5U over SLMP/UDP; the frame bytes of that script are asserted unchanged in `tests/test_slmp.py`.

## Keep this file true
At the end of a session: update "State", add anything the owner still has to do, and note new traps. Decisions with a trade-off belong in the README or an ADR.
