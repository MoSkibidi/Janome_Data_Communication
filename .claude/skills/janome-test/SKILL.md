---
name: janome-test
description: How to test the Janome JP4 program: run the unit/fault-injection suite, the database proof script, manual crash tests and real-press checks. Use when asked to test, prove a fail case, add a test, or before saying a change works.
---

# Janome JP4 - testing

## Run

```
cd C:\Apps\Janome_Data_Communication
.venv\Scripts\python.exe -W error::SyntaxWarning -m unittest discover -s tests
```

About 76 tests, ~30 s, all must pass (they use a fake press and a fake InfluxDB: no hardware, no network). One class or test:

```
.venv\Scripts\python.exe -m unittest tests.test_fail_cases.DatabaseProofCases
```

Log noise (`[ERROR] ...`, `ConnectionAbortedError` from the fake server) is normal; read the final `Ran N tests ... OK`.
Filter it: `... 2>&1 | grep -E "^(FAIL|ERROR|OK|Ran)|FAILED"`.

## What the suite covers (`tests/test_fail_cases.py`)

| Class | Proves |
|---|---|
| `SerialFailCases` | cable pulled, press power-cycle, NG/stop cycles, Q1 re-requested after reconnect |
| `DatabaseFailCases`, `DatabaseProofCases` | network down, disk full, token revoked (`DB_AUTH`), bucket deleted (`DB_BUCKET`), hanging network, buffer limit (`DATA_DROPPED`), refused record, restart with data on disk; **nothing lost and nothing sent twice** (local file vs InfluxDB) |
| `EventLogCases` | line format, case labels, one line per outage, rotation, idle/working, old-format lines |
| `CsvFileCases`, `SingleCsvCases`, `CycleReportCases`, `PastedFolderCases` | CSV columns, day start, folders, pasted paths with quotes |
| `FolderBrowserCases`, `LocalFileCases` | folder API, local SQLite file |
| `AutostartCases`, `PackagedProgramCases` | scheduled task logic with a fake runner, exe entry modes |

## Test tools (top of `tests/test_fail_cases.py`)

- `World` + `FakeSerial`: a fake press on a fake serial port. `world.plugged`, `world.powered`, `world.push_cycle(result_code=..., ok_total=..., fault_total=...)`.
- `FakeInflux`: `down`, `full` (disk full answer while down), `token`, `bucket_missing`, `blackhole` (accepts, never answers), `reject_marker`, `lines()`.
- `LoopRunner(test, config)`: runs the real `run_ingestion_loop` in a thread; `.start()`, `.finish()`, `.text()`, `.count(needle)`, `.tmp` (its config/data/logs).
  Read the event log with `event_log.read_recent(os.path.join(run.tmp, "logs", "events.log"))`.
- `wait_for(predicate, timeout, what)`: poll instead of sleeping.
- Do not `import app` in a test (side effects). Test logic in a module (`autostart.py`, `event_log.py`) with an injected runner.

Add a test for every new event code, fail case or CSV column. Assert the case label and level, not only the code.

## Proof script (read-only, no hardware)

```
.venv\Scripts\python.exe tests\prove_db_fail_cases.py
```

Runs the real reader against the fake press and a fake InfluxDB that it breaks five ways, prints the event-log lines for
each fault and recovery, then compares readings/cycles read vs held by InfluxDB. Exit code 0 = all reported, no loss.

## Manual crash checks (mock mode, throwaway panel on 8099 or the user's own on request)

- Reader dies: `taskkill /f /pid (Get-Content .janome_jp4_process.pid)` -> `CRASH / READER_RESTARTED`, reading continues.
- Whole panel dies (power-cut like): `taskkill /t /f /pid <listener of the port>` (not `stop.bat`), start again ->
  `CRASH / UNCLEAN_SHUTDOWN` then `START / RUN_RESUMED`. `stop.bat` instead leaves a marker and logs `SHUTDOWN`.
- Idle: no stroke for `event_log.idle_alert_s` (60 s) -> `IDLE`, next stroke -> `WORKING`.

## Real press (only with the user; never send control commands that move the ram)

Unplug the RS-232 cable -> `COMM ERROR / RS232_LOST`, plug back -> `RECOVERED`. Press power off -> `PRESS_NO_RESPONSE`
(indistinguishable from a cable pulled at the press), power on -> `PRESS_RECOVERED`. Real InfluxDB: stop the service ->
`DB_UNREACHABLE`; start -> `DB_RESTORED`; revoke the token -> `DB_AUTH`; rename the bucket -> `DB_BUCKET`.
A full disk cannot be triggered safely on a real server (simulated only).

## UI checks

Start a throwaway panel (see `janome-dev`), open it in the browser pane, exercise the changed control, look at a screenshot
and the console. Do not click buttons that change the user's real Windows task or settings.

## Honest reporting

State what ran (counts, which script) and what did not: E-stop frame on a real press, reboot / boot-time task, Windows
shutdown hook, a clean PC for the exe, real InfluxDB message formats.
