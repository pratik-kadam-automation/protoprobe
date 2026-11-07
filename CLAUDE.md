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
    python3 -m unittest discover -s tests                                   # engine, SLMP simulators, plcread, importers
    xvfb-run -a python3.12 -m unittest tests.test_screen_practice tests.test_tia_import     # Tk screens (needs python3-tk and a virtual display)
A full-app start-up check under xvfb (every screen shows) was used by hand; there is no CI for the app tests, so run them yourself.

## How a release works
Raise `APP_VERSION`, merge, then the owner pushes the tag (`git tag v0.3.1 && git push origin v0.3.1`). The *Release (Windows)* workflow runs the tests, builds `ProtoProbe.exe`, `plcread.exe`
and the installer on a Windows runner and publishes the release. The tag must equal `v` + `APP_VERSION`. Builds are not code-signed (SmartScreen warns).

## State on 2026-11-07
- Merged to main: commissioning wizard (#1), TIA Portal importer + `plcread` (#2), SIMATIC Manager importer (#3), Windows installer/release workflow (#4), S7 area-code import fix for python-snap7 2.x/3.x (#5, made in another session).
- **Open:** the first `v0.3.0` release build FAILED (the icon option was inserted inside `--name` in `packaging/build.py`, line 39 on main). The fix is on branch `fix-win-build`, **not merged**.
  To release: merge it, delete tag `v0.3.0` on GitHub (`git push origin :refs/tags/v0.3.0`), tag again on the fixed main. Later steps of the Windows build (installer, checksums, release) have never run.
- Not tested on real hardware: Mitsubishi FX5U over UDP in the wizard (only the SLMP frame was proven in the field with a script), any Siemens PLC, the GX Works3 / TIA Portal / SIMATIC Manager importers against real exports (the `.sdf`/`.asc` layouts follow the documented format).
- Not built: BACnet and OPC UA discovery in the wizard (they expose real tag names: use Connections > Browse), serial Modbus RTU in the wizard.

## Timeline (git dates)
- 2026-08-31 licence added (all rights reserved). 2026-11-06 all the work above (PRs #1-#5), done in sessions on 2026-11-05..07.
- The wizard grew out of a field script that read an FX5U over SLMP/UDP; the frame bytes of that script are asserted unchanged in `tests/test_slmp.py`.

## Keep this file true
At the end of a session: update "State", add anything the owner still has to do, and note new traps. Decisions with a trade-off belong in the README or an ADR.
