#!/usr/bin/env python3
"""Builds the standalone programs with PyInstaller:  dist/ProtoProbe(.exe)  and  dist/plcread(.exe).

    pip install -r requirements.txt pyinstaller
    python packaging/build.py

Run it on the system you are building for (a Windows .exe has to be built on Windows: the GitHub workflow does that).
The driver libraries are imported lazily by edge-gateway/drivers, which PyInstaller cannot see by itself, so they are
named here; python-snap7 brings its own DLL (collect-all) and sv-ttk its theme files (collect-data).
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = os.pathsep
COMMON = ["--noconfirm", "--clean", "--paths", "edge-gateway",
          "--hidden-import", "drivers.modbus_driver", "--hidden-import", "drivers.s7_driver", "--hidden-import", "drivers.mc_driver",
          "--hidden-import", "drivers.opcua_driver", "--hidden-import", "drivers.bacnet_driver",
          "--collect-all", "snap7", "--collect-submodules", "pymodbus", "--collect-submodules", "asyncua",
          "--collect-submodules", "bacpypes3", "--collect-data", "sv_ttk"]


def app_version():
    text = open(os.path.join(ROOT, "protoprobe.py"), encoding="utf-8").read()
    return re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M).group(1)


def run(args):
    print(">", " ".join(args), flush=True)
    subprocess.check_call([sys.executable, "-m", "PyInstaller", *args], cwd=ROOT)


def main():
    icon = ["--icon", os.path.join("assets", "protoprobe.ico")] if sys.platform == "win32" else []
    gui = ["--onefile", "--windowed", "--name", "ProtoProbe", *icon, "--add-data", f"assets{SEP}assets",
           "--add-data", f"edge-gateway{SEP}edge-gateway", "protoprobe.py"]
    run(COMMON + gui)
    run(COMMON + ["--onefile", "--console", "--name", "plcread", "plcread.py"])
    print("built version", app_version())


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(app_version())
    else:
        main()
