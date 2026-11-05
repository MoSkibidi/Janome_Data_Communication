# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe of the packaged Janome JP4 program. Run through installer/build_windows.ps1.
# One exe (JanomeJP4.exe) in one folder: the panel, the reader (--reader) and the COM test page (--diagnostic).
# English comments only

import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)  # the project folder (this file is in installer/)

hidden = []
hidden += collect_submodules("eventlet")             # the hub and DNS pieces eventlet loads by name
hidden += collect_submodules("dns")
hidden += collect_submodules("engineio.async_drivers")  # python-engineio picks its driver by name at run time
hidden += collect_submodules("socketio")
hidden += ["serial.tools.list_ports_windows", "psycopg2"]

a = Analysis(
    [os.path.join(ROOT, "janome_main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, "templates"), "templates"),
        (os.path.join(ROOT, "static"), "static"),
        (os.path.join(ROOT, "config.json"), "."),  # seed: the program writes it next to the exe when it is missing
    ],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JanomeJP4",
    console=True,     # the program prints its log; run.bat / the scheduled task keep the window hidden
    upx=False,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="JanomeJP4")
