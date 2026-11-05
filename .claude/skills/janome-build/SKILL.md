---
name: janome-build
description: How to build, check and hand over the packaged Janome JP4 program (JanomeJP4.exe, no Python needed on the target PC) with PyInstaller. Use when asked to build, package, rebuild, install or deploy the exe, or when changing installer/, run.bat, janome_main.py or anything the exe must contain.
---

# Janome JP4 - build and package

Everything for the build lives in `installer/`. The source tree is not modified by a build.

## Build

Needs: Windows x64, Python 3.12 x64 (`py -3.12` or `python`), internet the first time (pip).

```
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_windows.ps1
```

It creates `installer\.build-venv`, installs `requirements.txt` + `pyinstaller==6.16.0`, runs
`installer\JanomeJP4.spec`, then copies into the output folder: `run.bat`, `stop.bat`, `run_service.bat`, `config.json`
(project root), `install_autostart.ps1`, `uninstall_autostart.ps1` (`deploy\`), and `installer\package\*`
(`install.bat`, `uninstall.bat`, `README_INSTALL.txt`). Result (about 1 minute on a rebuild):

- `installer\dist\JanomeJP4\` - the program folder (~32 MB)
- `installer\dist\JanomeJP4-Windows-x64.zip` (~16 MB; `-NoZip` skips it)

`installer/dist`, `installer/build`, `installer/.build-venv` are git-ignored: **the build is never committed or pushed.**
Rebuild after changing any file in the list above (the `.bat`/`.ps1` are copies).

## How the exe is organised

- One exe, onedir (`_internal\` must stay next to it). `console=True` on purpose: the program prints its log; a
  windowless exe has no stdout and would crash on `print`. `run.bat` / the scheduled task keep the window hidden.
- `janome_main.py` is the entry: no argument = panel (port 8085), `--reader ...` = the reader (the panel starts the
  same exe this way when you press Start), `--diagnostic` = COM test page (8086).
- `app.py` takes templates/static from the bundle (`sys._MEIPASS`) and keeps `config.json`, DB files and `logs\` next to the exe.
- Hidden imports (eventlet, dns, engineio async drivers, socketio, psycopg2, pyserial Windows backend) are listed in the
  spec. A new dependency that is imported by name at run time must be added there; a new data folder goes in `datas`.

## Check a build (always do this, on a throwaway port)

1. Copy `installer\dist\JanomeJP4` into the scratch directory (keep `dist` clean).
2. Set `database.db_type` to `none` in the copy's `config.json`; use a strict PATH without Python
   (`$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\WindowsPowerShell\v1.0"`).
3. `$env:JANOME_PORT='8099'`, then `cmd /c ".\run.bat"` in the copy. Expect `Using the packaged program JanomeJP4.exe`,
   HTTP 200 on `http://localhost:8099/`, `logs\panel.log` without a traceback.
4. Press Start (simulation) over Socket.IO or the browser: `READER_START` / `PRESS_CONNECTED` in the Event log and
   `GET /api/autostart` answers. Stop it again.
5. Kill by listener: `taskkill /t /f /pid <pid listening on 8099>`. Never run the copy's `stop.bat` while the user's
   panel (8085) is up: it kills every `JANOME_JP4_PANEL` window.
6. Optional proof that no local Python is used: the process's loaded modules must all come from the program folder or
   `System32`.

## Hand-over to a target PC

Copy the whole `JanomeJP4` folder (or the zip) to e.g. `C:\JanomeJP4`, install the USB/RS-232 driver, double-click
`install.bat` (one UAC "Yes": scheduled task at boot, desktop shortcut, opens the panel), then set COM port, database,
CSV folder in Settings. No Python and no internet are needed. InfluxDB is **not** included: use another PC's InfluxDB,
install one separately, or choose "This computer only".

## Known limits (say them when reporting)

- `config.json` in the package is a copy of the project's and contains the InfluxDB token: keep the folder private.
- Verified: runs without Python on PATH, no Python files loaded, hidden launch, reader mode, Stop.
  **Not** verified: a really clean PC, `install.bat` end to end with UAC, start after a real reboot, the Windows shutdown hook in exe mode.
- Some antivirus products flag PyInstaller exes: ask for a whitelist if blocked. No code signing.
