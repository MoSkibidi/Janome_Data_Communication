---
name: janome-dev
description: How to develop the Janome JP4 panel and reader (run from source, code layout, conventions, event log, Windows pitfalls). Use before changing app.py, read_janome_jp4.py, the web UI, the event log, CSV export or the .bat/.ps1 scripts.
---

# Janome JP4 - development

Project: `C:\Apps\Janome_Data_Communication` (git remote `MoSkibidi/Janome_Data_Communication`, branch `main`).
Python: always the project's own `.venv\Scripts\python.exe` (Python 3.12). Comments in code are English only.

## Run from source

| What | Command |
|---|---|
| Panel (port 8085) | `.venv\Scripts\python.exe app.py` |
| Reader alone, simulation | `.venv\Scripts\python.exe read_janome_jp4.py --mock --config config.json` |
| Hidden background run | `run.bat` / `stop.bat` (log `logs\panel.log`; `JANOME_SHOW_WINDOW=1` shows the window) |
| COM test page (port 8086) | `.venv\Scripts\python.exe serial_test_webapp.py` |

Environment: `JANOME_PORT`, `JANOME_HOST`, `JANOME_DATA_DIR` (where config, DBs, logs live), `JANOME_EVENT_LOG`.
The press is on COM7, 9600 8N1 (see `config.json`). Flask caches templates: restart the panel to see HTML changes.

## Never disturb the user's own panel

The user's panel runs on **8085** from `run.bat`; it keeps old code until they restart it. For your own checks start a
throwaway panel on another port with its own data dir:

```
set JANOME_PORT=8099
set JANOME_DATA_DIR=<scratch folder>
.venv\Scripts\python.exe app.py
```

Kill it by its listener (`taskkill /t /f /pid <pid of port 8099>`), never with `stop.bat` (it kills every window titled
`JANOME_JP4_PANEL`, including the user's). Do not enable/disable the Windows task `JanomeJP4Panel` on this PC: it is the
user's real task. `config.json` is rewritten by the panel's Save button: do not commit its incidental changes.

## Code map

| File | Role |
|---|---|
| `app.py` | Flask + Socket.IO panel, supervises the reader process, REST API, event/heartbeat/shutdown hooks. Import has side effects (starts threads, writes files): do not import it in tests |
| `read_janome_jp4.py` | Protocol (JP4 ASCII), `PressSession` (link states), `run_ingestion_loop`, `make_cycle_handler`, `IdleWatch`, CSV hook |
| `event_log.py` | Persistent `logs/events.log`, `CASE_OF` map, `read_recent` |
| `database_handler.py` | InfluxDB/SQL writer, `BufferedDatabaseWriter`, `DiskSpool`, `classify_db_error` |
| `csv_export.py` | Single cycle CSV (columns, day start, folder cleaning) |
| `local_store.py`, `run_summary.py` | Local SQLite of every run, run statistics |
| `folder_browser.py`, `autostart.py` | Folder browse API, "Start with Windows" (scheduled task) |
| `janome_main.py` | Entry point of the packaged exe: panel / `--reader` / `--diagnostic` |
| `templates/index.html`, `static/app.js`, `static/style.css` | The panel UI |
| `installer/` | Build of the packaged program (see the `janome-build` skill) |
| `tests/` | See the `janome-test` skill |

## Conventions that matter

- **Event log**: one line `time | LEVEL | CASE | CODE | message | details`. A new event = `event_log.emit(CODE, msg, level=..., **details)`
  plus an entry in `event_log.CASE_OF` (else it shows as `INFO`), plus a row in the README table, plus a test. Cases and colours
  (CSS `case-<slug>` in `static/style.css`): START/STOP/SHUTDOWN, COMM ERROR, RECOVERED, NG / NG STOP, CRASH (strongest),
  DB ERROR, DB FULL, DATA CHECK, IDLE/WORKING, INFO. One line per outage, never one per retry.
- **Frozen vs source paths** (`app.py`): `RESOURCE_DIR` (templates/static, `sys._MEIPASS` in the exe) is separate from
  `BASE_DIR` (config/data, next to the exe). Keep writable files under `DATA_DIR`.
- **The reader must not run under eventlet monkey patching**; only the panel does. That is why `janome_main.py` imports lazily.
- **Endpoints that touch this computer** (folder browse, autostart) must call `local_request_only()`: the panel listens on all interfaces without login.
- **UI JavaScript must be null-safe**: the browser may hold cached HTML without a newly added element.
- Results 3229/3232/3233 = stopped before finishing (case `NG STOP`); Return Speed item 2030 vs 2002 is still unconfirmed.
- Automatic start must tolerate the database being unreachable (spool); a manual Start with the database unreachable exits with code 2.

## Windows pitfalls hit in this project

- Heredocs/`sed` mangle backslashes (`\n`, `\r`, `\I`): for text containing backslashes use the Edit tool or write a file and
  run a small script. After `sed` on a `.bat` re-check the file.
- `.bat` files must stay CRLF (labels and `goto`). Avoid `( ... )` blocks that expand variables set inside them.
- This PC has `NoDefaultCurrentDirectoryInExePath`: call programs as `.\JanomeJP4.exe` and `.\run.bat`, never bare names.
- PowerShell 5.1 `Set-Content -Encoding utf8` adds a BOM that breaks `json.load`: write JSON with
  `[IO.File]::WriteAllText(path, text, New-Object Text.UTF8Encoding($false))`.
- Docstrings with `C:\Users` raise a unicode-escape SyntaxError: reword. Run tests with `-W error::SyntaxWarning`.
- `Edit` fails with "modified since read" after a script touched the file: Read it again first.

## Before you say it is done

Run the whole suite (see `janome-test`), look at the UI in a browser if you changed it, and say plainly what was not
verified on real hardware (E-stop frame, reboot behaviour, scheduled task after a real boot). Commit/push only when asked;
never commit `Test/`, `config.json` drift, or anything under `installer/dist`.
