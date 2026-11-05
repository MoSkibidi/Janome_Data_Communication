# Janome JP4 Data Communication

RS-232 COM-protocol driver, telemetry panel, and diagnostic tooling for the
**JANOME Electro Press JP4**.

## Protocol notes (confirmed against real hardware)

Janome doesn't publish an RS-232 command protocol for JP4 in its standard
manual set - only electrical specs ("RS-232C, 1ch, for PC control using
optional software"). The actual protocol used here comes from
`891817107_COM_control.pdf` (a separate JP4 COM-communication manual) and
has been verified live against a real JP4 press:

- **Baud rate is set on the press, so it differs between units**: one unit
  was found at 38400 bps, another at 9600 bps (the documented default). If a
  press stays silent (`no_response`), scan the baud rates with the read-only
  `N1` command before suspecting the cable; a wrong rate gives complete silence,
  the same as a swapped TX/RX pair. Set the matching value in the panel's
  serial settings.
- Link settings: 8 data bits, no parity, 1 stop bit.
- Frame format: `$` + 2-byte address + 1-byte command + 1-byte subcommand +
  data + 2-hex-char SUM checksum + CR. Checksum covers
  address+command+subcommand+data, matching the manual's own worked
  examples exactly (B0 -> D3, N0 -> DF, N2 -> E1).
- Quality Data (`V1`) push frames use an **uppercase `V`** command code,
  breaking the general "press replies in lowercase" convention used by
  every other command (N, B, G, K, M, P all reply in lowercase as
  documented). Confirmed against live captured frames.
- `A0` (Connection Confirmation) does **not** exist in this protocol - it
  was mistakenly pulled from a different, older manual (`COM_basic.pdf`,
  which documents an unrelated fixed-block binary framing this press does
  not use) during early development. Use `B0` or any read command to check
  connectivity instead.

## Files

| File | Purpose |
|---|---|
| `read_janome_jp4.py` | Core driver: builds/parses protocol frames, reads position/load telemetry, writes to a database on an interval. Also usable standalone via CLI (`--mock` for driver-free testing, `--once` for a single read). |
| `local_store.py` | The local SQLite file with every run's readings and cycles (always written; see Database behaviour). |
| `event_log.py` | The persistent event (error) log `logs/events.log`: one line per fail case and recovery (see Event log). |
| `csv_export.py` | Writes the cycle CSV file, one file per run or per day (see Cycle CSV file). |
| `database_handler.py` | SQLite/PostgreSQL/InfluxDB storage backend for the ingestion driver. |
| `run_summary.py` | In-memory statistics and samples of the current/last run (Run summary card, CSV download). |
| `app.py` | Flask-SocketIO web control panel (port 8085) - configure the serial port, start/stop real or mock ingestion, view live telemetry. |
| `quick_test.py` | Minimal standalone CLI script - opens the port, sends a few commands, prints raw TX/RX bytes. No Flask/database dependency. |
| `serial_test_webapp.py` | Full diagnostic web app (port 8086) - command picker covering all read/control commands in plain language, a Poll Set for program settings (S2), a one-shot Quality Data capture, and a **Live Cycle Monitor** that logs every completed press cycle automatically (with link keep-alive). |
| `run_service.bat`, `deploy/` | Foreground launcher plus auto-start at boot: `install_autostart.ps1` / `uninstall_autostart.ps1` (Windows scheduled task) and `janome-panel.service` (Linux systemd). |
| `Dockerfile`, `docker-compose.yml` | Container image for the panel (`app.py`) and a local InfluxDB instance wired together - see **Docker** below. |
| `tests/test_fail_cases.py` | Fault-injection tests (fake press, fake InfluxDB): cable pulled, press power-cycle, NG cycle, network outage, restart with spool, retention. |
| `config.json` | Serial port, baud rate, database, and startup settings for `read_janome_jp4.py` / `app.py`. |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

## First-time deployment (Windows PC next to the press)

1. **Install** Python 3.12 (64-bit, tick "Add python.exe to PATH") and Git.
2. **Install the driver** of the USB-RS232 adapter, then look up its port (COMx) in Device Manager.
3. **Get the code and the libraries** (do not copy a `.venv` from another PC: it is tied to the path it was made in):
   ```bash
   git clone https://github.com/MoSkibidi/Janome_Data_Communication.git
   cd Janome_Data_Communication
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   Without the `.venv`, `run.bat` falls back to the global `python`, which has none of the libraries
   (the reason is then in `logs\panel.log`).
4. **Edit `config.json`** (or do it later in the panel's Settings): `serial.port` (and baud rate 9600),
   `csv_export.folder` (a folder this PC has; the program creates it), and `database.influx_url` / token /
   org / bucket of the InfluxDB to send to, or choose "This computer only" when there is none.
   `config.json` in the repository contains an InfluxDB token: keep the repository private and change the token for each site.
5. **Start**: `run.bat` (hidden window, `stop.bat` stops it), open `http://localhost:8085`, check that the
   port is the right one, press Start. Watch the Event log card: `START` lines and no `COMM ERROR`.
6. **Start by itself after a reboot** (optional): in an Administrator PowerShell run
   `powershell -ExecutionPolicy Bypass -File deploy\install_autostart.ps1 -Mode Startup` (or `-Mode Logon`
   without administrator rights), and tick "Start reading automatically" in Settings > Startup.
7. **Reach the panel from another PC** (optional): allow TCP port 8085 in Windows Firewall.

A new PC starts clean: databases (`*.db`), `logs/` and the state files are not in git on purpose.

## Running

**Diagnostic tool (recommended starting point):**
```bash
python serial_test_webapp.py
```
Then open `http://localhost:8086`. Use "Scan Ports" to find your adapter,
pick a command from the dropdown (grouped into safe read-only queries vs.
control commands that physically move the press, which require an explicit
confirmation checkbox before sending), and responses are automatically
translated into plain English with units.

For live production monitoring, use the **Live Cycle Monitor** panel: it
enables quality-data streaming once and logs every subsequent cycle's
Preparation Time, Work Time, end position/load, and OK/fault counters
automatically, with a keep-alive check so you can tell the link is still
alive during a long-running session.

**Minimal CLI test (no web server):**
```bash
python quick_test.py COM3
```

**Full ingestion service + panel:**
```bash
python app.py
```
Then open `http://localhost:8085`. Configure the database target and serial
port in the panel, then start real or mock ingestion.

**Windows background run:** `run.bat` starts the panel hidden (logs in
`logs\panel.log`) and `stop.bat` stops it.

## Docker (panel + local InfluxDB, one command)

```bash
docker compose up -d --build
```

This starts two containers, wired together with no manual setup:

- **`influxdb`** - an InfluxDB 2.7 instance, pre-initialized (org `janome`, bucket
  `janome_jp4`, admin user `admin`, admin token `janome-local-dev-token`) via the
  `DOCKER_INFLUXDB_INIT_*` variables in `docker-compose.yml`. UI at `http://localhost:8086`.
- **`app`** - the control panel (`app.py`), built from the `Dockerfile`, at
  `http://localhost:8085`. It is pointed at the `influxdb` container through the
  `INFLUX_URL` / `INFLUX_TOKEN` / `INFLUX_ORG` / `INFLUX_BUCKET` environment variables set
  on the `app` service; these override whatever is in `config.json`'s `database.influx_*`
  fields (see `_apply_env_overrides` in `database_handler.py`), so the image works out of
  the box without editing `config.json` inside the container.

**Change the admin password and token** in `docker-compose.yml` before exposing either
port beyond your own machine - they are checked-in development defaults.

Both containers' data survive `docker compose down` (not `down -v`): the panel's
`config.json`, local SQLite file and spool live in the `janome-data` volume
(`JANOME_DATA_DIR=/data`), and InfluxDB's own storage lives in `influxdb-data`.

**Serial port on Windows:** Docker Desktop containers cannot see a Windows COM port
directly. To reach a real press from the `app` container:
1. Install [usbipd-win](https://github.com/dorssel/usbipd-win) on the Windows host.
2. `usbipd list` to find the USB-serial adapter's bus ID, then `usbipd bind --busid=<id>`
   and `usbipd attach --wsl --busid=<id>` to share it into the WSL2 VM Docker Desktop uses.
3. Inside WSL2, check what it came up as (`ls /dev/ttyUSB*` / `ls /dev/ttyACM*`).
4. Uncomment the `devices:` line under `app` in `docker-compose.yml`, pointing it at that
   path, then `docker compose up -d` again.

Until that is done, use **Simulation** mode in the panel to exercise the rest of the stack.

## Fail cases and how the program handles them

| # | Situation | What happens |
|---|---|---|
| 1 | RS-232 cable pulled / USB adapter unplugged | The reader never exits. It logs `[LINK] state=port_unavailable`, retries opening the port every 5 s (`acquisition.reconnect_interval`) and carries on by itself when the port is back. Also covers a port that is missing at start. |
| 2 | Press reports NG or an emergency stop | Every finished cycle (OK or NG) is read from the press's Quality Data (`V1`) push frame, shown in **Press cycles**, logged (`[CYCLE]`, plus a `[NG]` warning) and stored like a reading. A non-zero result code means NG (for example 3232 "External signal stopped", 3229 "Finished mid-operation"). |
| 3 | Press powered off and on | The port stays open and the reader reports `[LINK] state=no_response` after 3 failed polls. When the press answers again it reports `state=up` and asks for cycle results again (`Q1`), because a restarted press forgets that. No manual Start. |
| 4 | The PC (MIC-770 or this laptop) is powered off or rebooted | `deploy/install_autostart.ps1` (Windows scheduled task) or `deploy/janome-panel.service` (Linux systemd) starts the panel at boot and restarts it if it exits. On start the panel resumes the reader if a run was active when it went down, or if "Start reading automatically" is ticked. Unsent data is in the disk spool. An automatic start does not give up when the database is not reachable yet: it spools and sends later. |
| 4b | The reader process itself dies while the panel is up | The panel notices within 5 s and starts it again (same run, the log is appended to). Exception: if the reader refused to start because the database was unreachable on a manual Start, it is not retried and the error stays on screen. |
| 5 | InfluxDB fills up | InfluxDB deletes old data by itself according to the bucket's retention. Set `influx_retention_days` (Settings: "Keep data for") and the panel applies it to the bucket when ingestion starts (the token must be allowed to edit buckets; otherwise a warning is logged and it can be set in the InfluxDB UI). If InfluxDB rejects writes because its disk is full, the data waits in the edge spool like any other outage. |
| 6 | Network down | Data is kept at the edge (memory, then the disk spool) and sent to InfluxDB when it is reachable again, oldest first, with the time it was read. Writes are idempotent in InfluxDB, so a batch sent twice does not create duplicates. |
| 7 | Data for each stamping | One record per press stroke (`janome_jp4_telemetry_cycles` measurement or table): OK/NG, result code and text, program number, work time, preparation time, contact and end position/load, running OK/fault/lifetime counters, sensor fault location. |

### Event log (the error log)

Every case above leaves a clear line in **`logs/events.log`** (next to `config.json`; override with the
environment variable `JANOME_EVENT_LOG`). It is also shown in the panel's **Event log** card
(newest first, "Problems only" filter, Download button, `/api/events?limit=100&level=WARNING`) and in
the normal Log. Unlike the pipeline log, which starts empty on every Start, this file is only ever
appended to (rotated at 5 MB into `events.log.1` ...), so the history survives Stop/Start, a crash and a
reboot. One line per event, one line per outage (not one per retry):

Each line starts with a plain **case** (what kind of thing happened) and then the exact event code. The panel
shows the case as the second column and colours the row: **CRASH** is white-on-red and bold, `COMM ERROR`,
`DB ERROR` and `DB FULL` are red, `NG` / `NG STOP` / `DATA CHECK` amber, `START` / `RECOVERED` green,
`STOP` / `SHUTDOWN` / `INFO` plain.

```
time                | level   | case       | code              | message                                                | details
2026-11-05 11:20:46 | ERROR   | COMM ERROR | RS232_LOST        | RS-232 port COM7 is not available (adapter unplugged or port busy). Auto-reconnect: opening it again every 5 s. | port=COM7 reason=...
2026-11-05 11:21:30 | INFO    | RECOVERED  | RS232_RECOVERED   | RS-232 port COM7 is back and the press answers; reading continues automatically (link was down 44 s). | port=COM7 down_for=44 s
```

| Case | Event codes | When |
|---|---|---|
| `START` | `SERVICE_START`, `READER_START`, `PRESS_CONNECTED`, `RUN_START`, `RUN_RESUMED` | The panel came up (a note says when it was a computer boot), the reader started (automatic or manual), the press connected, or reading resumed by itself after a crash |
| `STOP` | `RUN_STOP`, `READER_STOP` | The operator pressed Stop (a deliberate stop is never a failure) |
| `SHUTDOWN` | `SERVICE_STOP` | Normal shutdown of the panel: closing the console, Windows logoff/shutdown, SIGTERM, or `stop.bat` (which leaves a `.janome_jp4_clean_stop` note, so the next start logs "stopped with stop.bat" instead of a crash) |
| `COMM ERROR` | `RS232_LOST`, `PRESS_NO_RESPONSE` | USB/RS-232 adapter unplugged or port busy; or the port is open but the press is silent (powered off, cable pulled at the press, wrong baud rate). Powering the press off normally looks like this |
| `RECOVERED` | `RS232_RECOVERED`, `PRESS_RECOVERED`, `DB_RESTORED` | The port or the press answers again, or the database is reachable again, with how long it was down |
| `IDLE` | `PRESS_IDLE` | The press is on and answering but made no stroke for 60 s (no job running), counted from the link coming up or the last cycle. One line per quiet period, not one every 60 s. Change the time with `"event_log": {"idle_alert_s": 60}` in `config.json`; 0 turns it off. A link problem is never reported as idle |
| `WORKING` | `PRESS_WORKING` | The first stroke after an idle period, with how long it was idle |
| `NG` | `PRESS_NG` | A cycle finished NG (WARNING): result code and text, program, end position and load, fault counter |
| `NG STOP` | `PRESS_NG` | Same, but the result code is 3229 / 3232 / 3233: the stroke was stopped before it finished (emergency stop / stop signal) |
| `CRASH` | `UNCLEAN_SHUTDOWN`, `READER_RESTARTED`, `READER_ERROR` | The previous panel session was killed or lost power while a run was active (ERROR, with the time of its last sign of life), or the reader process died and the panel restarted it. Reading resumes by itself |
| `DB ERROR` | `DB_UNREACHABLE`, `DB_AUTH`, `DB_BUCKET`, `DB_REJECTED`, `DATA_DROPPED` | `DB_UNREACHABLE`: network/server down or not answering (data kept at the edge and synced later, oldest first). `DB_AUTH`: the server answers but refuses the token (revoked or changed). `DB_BUCKET`: the bucket/organization does not exist. `DB_REJECTED`: one record refused, the others still go. `DATA_DROPPED`: the edge buffer limit was hit (the local file still has everything) |
| `DB FULL` | `DB_FULL` | InfluxDB out of space ("no space left", HTTP 507) |
| `DATA CHECK` | `CYCLE_GAP`, `CYCLE_COUNTER_RESET` | The press cycle counter jumped (cycles happened while nothing was reading; the counter of the last saved cycle is remembered across restarts) or went backwards |
| `INFO` | `DB_RETENTION`, anything new | The retention period was applied/failed |

If the panel starts and the previous session ended without a `SHUTDOWN` while no run was active, the log
shows `UNCLEAN_SHUTDOWN` as `STOP` (INFO): nothing was being read, so nothing was lost.
The Windows shutdown hook has not been tried on a real reboot yet.

#### Proving the database cases

- `python tests/prove_db_fail_cases.py` runs the real reader against a fake press and a fake InfluxDB that it
  breaks on purpose (server down, disk full, token revoked, bucket deleted, a network that hangs). It prints the
  lines that appear in `events.log` for each fault and its recovery, then compares what the reader read with what
  InfluxDB holds (nothing lost, nothing doubled). Exit code 0 = all good.
- `tests/test_fail_cases.py` (`DatabaseProofCases`) checks the same with assertions.
- On the real InfluxDB: stop the InfluxDB service (expect `DB ERROR / DB_UNREACHABLE`), start it (expect
  `RECOVERED / DB_RESTORED` and the missing points filling in with their original times), delete or change the
  token in InfluxDB (`DB_AUTH`), rename the bucket (`DB_BUCKET`). A full disk is only simulated: it cannot be
  triggered safely on a real server.

### Start at boot and after a crash

- Set **Start reading automatically** (Settings > Startup, `startup.auto_start_on_startup` in
  `config.json`, mode `real`): whenever the panel starts, the reader starts too. Even without it, the panel
  resumes by itself when a run was active at the time it went down.
- `deploy/install_autostart.ps1` creates the Windows scheduled task `JanomeJP4Panel` that runs
  `run_service.bat` and restarts it if it exits. `-Mode Logon` (no administrator needed) starts it when the
  user logs in; the default `-Mode Startup` (Administrator PowerShell) starts it at boot, 30 s after
  power-on, before anyone logs in. Remove it with `deploy/uninstall_autostart.ps1`.
- A crash or hard kill of the panel or the reader was tried on a throwaway panel in simulation mode: the
  reader came back by itself and the event log showed `UNCLEAN_SHUTDOWN`, `RUN_RESUMED`, `READER_RESTARTED`.

Details worth knowing:

- Cycle times are the moment the panel received the result (within about 0.2 s of it
  arriving), not a clock inside the press. Results the press had queued before the
  panel asked for them (right after a start or reconnect) have `backlog = true`;
  their time is the time they were received, not when the stroke happened.
- Everything above is checked by `tests/test_fail_cases.py` against a fake press and
  a fake InfluxDB (`python -m unittest discover -s tests -v`). It has **not** yet been
  run against the real press or a real InfluxDB, and the auto-start scripts have not
  been tried through a real reboot.
- To confirm on site: that an emergency stop really produces a `V1` frame (if the
  press stays silent on an emergency stop, no NG row can be created for it), and which
  result codes the press uses.

## Database behaviour

The database engine is chosen in the panel (`database.db_type` in `config.json`):

- **Local file (always on):** every reading and every finished cycle is also saved
  in `janome_jp4_local.db`, a small SQLite file next to `config.json` (so in
  `JANOME_DATA_DIR`), grouped by run (Start to Stop). It is written whether or not a
  database is configured, so the data is on this computer even while the database
  server is down. Each write is committed with `synchronous=FULL` (survives a crash or
  power cut). The file is bounded: beyond `local_store.max_rows` readings (default
  2,000,000, about 23 days at one reading a second) the oldest readings are deleted.
  Optional `config.json` section: `"local_store": {"enabled": true, "path": "...",
  "max_rows": 2000000}`. If the file cannot be written, collection carries on and a
  `[LOCAL]` warning is logged. The pipeline log is still overwritten at each Start.
  This file is the history; the spool described below is the queue of what has not
  reached the database yet.
- **`none` ("This computer only"):** nothing is sent anywhere; the local file above is
  the only copy.
- **`sqlite` / `postgresql` (PostgreSQL or TimescaleDB):**
  - The connection is checked when ingestion starts. If it fails, a
    `[DB ERROR]` line is logged and ingestion does **not** start (exit code 2).
  - Serial polling never waits on the database. Samples are queued in memory
    with the time they were read and written by a background thread.
  - **Disk spool:** when the database is unreachable, queued samples are moved
    at once into `janome_jp4_spool.db` (a small SQLite file next to
    `config.json`, so it lives in `JANOME_DATA_DIR`). The writer retries every
    5 s; after reconnecting it replays the spool first, then newer samples, in
    order and with their original read timestamps. Rows leave the spool only
    after the database has committed them, so a crash, forced kill or power
    loss does not lose spooled samples.
  - **After a crash/restart:** leftover spool rows are sent on the next start.
    If the database is still down but the spool is not empty, ingestion starts
    anyway and keeps spooling. With an empty spool and no database, it refuses
    to start as described above.
  - **Limits of the guarantee:** samples still in memory at the instant of a
    crash (normally under a second; up to one reconnect attempt, about 5 s,
    while the database is down) are lost. A crash while replaying can send the
    last batch (at most 500 rows) twice, because the table has no unique key.
  - Optional keys in `database`: `max_buffer` (in-memory samples), `max_spool_rows`
    (default 1,000,000; oldest dropped beyond that), `spool_path`,
    `retry_interval` (seconds), `connect_timeout` (seconds, PostgreSQL only).
  - The `[STATS]` line reports `db_state` (`connected`/`down`/`none`),
    `db_pending` (memory + spool) and `db_dropped`; `written` counts rows
    actually committed.
- **`influxdb`** (default): same connection check, buffering and spool as above. Two
  measurements are written: telemetry (`influx_measurement`, default
  `janome_jp4_telemetry`; fields `encoder_position_mm`, `teaching_position_mm`,
  `load_n`) and cycles (`influx_cycle_measurement`, default
  `janome_jp4_telemetry_cycles`; tags `result` = `OK`/`NG` and `program`, fields as
  listed under case 7). Timestamps are written in nanoseconds. Keys in `database`:
  - `influx_url` (for example `http://192.168.1.10:8086`)
  - `influx_version`: `2` (default; also works with InfluxDB 3's v2-compatible write
    API) or `1`
  - version 2: `influx_token`, `influx_org`, `influx_bucket`
  - version 1: `influx_bucket` (the database name), `influx_user`, `influx_password`;
    the database is created if it does not exist
  - `influx_retention_days` (0 or absent leaves InfluxDB's own setting alone)
  - The connection check at start reports an unreachable server, wrong
    credentials or a missing bucket. A point InfluxDB rejects as malformed (HTTP
    400/422) is logged and dropped so it cannot block the rest of the queue.
- SQL databases get a second table, `<table_name>_cycles` (`cycles_table` to
  rename). The tables are created as plain tables. A TimescaleDB hypertable is not
  created automatically.

## Run summary (temporary data of a run)

The panel shows the current or last run in a
**Run summary** card: min, max, average, standard deviation and latest value of
actual position, programmed position and load, plus a table of every reading.

- A run is everything from **Start** (or **Start simulation**) to **Stop**.
  Statistics cover the whole run, however long it is. They stay on screen after
  Stop, until the next Start replaces them.
- The numbers are rebuilt from the local file when the panel restarts, so the last
  run is still there after a panel restart or a reboot. If the computer was switched
  off during a run, that run is closed at its last reading. An automatic start after a
  reboot begins a new run (there is a gap in the data, so it is not glued on).
- The page keeps the latest 50,000 readings of the run in memory
  (`RunSummary(max_samples=...)` in `app.py`). Statistics still cover every reading;
  the card says so when older readings were dropped. The browser shows the latest
  2,000 rows.
- **Download CSV** (`/api/run.csv`, times in UTC) gives all readings of the run,
  read from the local file (not limited to the 50,000 in memory).
  JSON endpoints: `/api/run` (summary) and `/api/run/samples?limit=N`.
- Each `[STATS]` line from the reader ends with `time=<ISO time>`, the moment the
  reading was taken; the panel uses it for the sample timestamps.
- If the panel starts while a reader is already running, the summary begins at
  that moment.

## Cycle CSV file (automatic)

Every finished press cycle, OK and NG, is also appended as one row to a plain CSV file as it
happens (`cycles_*.csv`). It is a small readable record that opens straight in Excel, next to the
local SQLite file and the optional database. Each row is flushed and synced to disk before the next
cycle is handled. This is the only CSV file the program writes.

Settings (panel: **Cycle CSV files**, or `config.json`):

```json
"csv_export": { "enabled": true, "mode": "run", "folder": "exports",
                "day_start": "00:00", "end_load_setting_kgf": 250 }
```

The **Folder** field has a **Browse...** button: it lists the folders of the computer running the
panel. It starts at your **Home** folder (the Users folder of the signed-in user; if the field already
holds a full path it starts there instead). The first entry of the drop-down is Home, followed by the
drives, and there are sub-folders, Up and a "New folder" button. It fills in the full path you pick. Browsing is
only allowed from the same computer (localhost); a panel opened from another PC must type the path.

- `mode: "run"`: a new file for every Start-Stop run, `exports/cycles_run_2026-11-05_100635.csv`
  (named after the run's start time). If the reader restarts by itself inside the same run, it
  keeps appending to that file.
- `mode: "day"`: one file per day (computer's local time), `exports/cycles_day_2026-11-05.csv`.
  Several runs on the same day share the file, and a run that crosses the change of day continues in
  the next day's file.
- `day_start` (panel: **A new day starts at**, only used with `mode: "day"`, default `"00:00"`): the
  time of day when the next file begins, for a shift that does not start at midnight. With
  `"day_start": "07:00"` (7 am) every 07:00 is the start of a new day: the cycles from 07:00 until
  06:59 the next morning go in one file, named after the day it started and with the start time:
  `exports/cycles_day_2026-11-05_0700.csv` (a 02:00 cycle on 6 Nov is still in the 5 Nov file). `00:00` keeps the plain calendar-day name. An invalid value
  logs a `[CSV]` warning and uses 00:00. Changing it mid-day starts a differently named file instead
  of mixing the two.
- `folder` is relative to `config.json` unless it is a full path. Files are never deleted
  automatically.
- The settings are read when a run starts, so a change applies from the next Start.

Columns, in this order:

| # | Column | Where it comes from |
|---|---|---|
| 1 | `pressing_speed_mm_s` | the program's pressing speed setting (read with S2) |
| 2 | `return_speed_mm_s` | the program's return speed setting (S2) |
| 3 | `start_position_mm` | the program's start position setting (S2) |
| 4 | `stroke_mm` | calculated: end position setting - start position |
| 5 | `end_position_mm` | actual end position of the cycle |
| 6 | `end_load_kgf` | actual end load in kg: `end_load_n / 9.80665` (the press screen shows kg) |
| 7 | `end_load_setting_kgf` | setting `end_load_setting_kgf` (default 250). The press does not keep this target in stop-position mode, so it is configured here |
| 8 | `end_load_diff_kgf` | actual end load difference: `end_load_kgf - end_load_setting_kgf` (negative = below the setting) |
| 9-10 | `prep_time_s`, `work_time_s` | preparation time and work time of the cycle (Quality Data frame) |
| 11-13 | `time_local`, `time_utc`, `run_id` | when the cycle was received, and the run |
| 14-17 | `program_number`, `ok` (1/0), `result_code`, `result_text` | which program and the result |
| 18-19 | `end_position_set_mm`, `end_load_n` | the end position setting (S2) and the end load in N |
| 20-21 | `contact_position_mm`, `contact_load_n` | where the ram touched the part |
| 22-24 | `ok_total`, `fault_total`, `lifetime_total` | the press counters |
| 25-27 | `sensor_fault_section`, `sensor_fault_number`, `backlog` (1/0) | sensor fault and whether the cycle was queued in the press |

Values that would only repeat an earlier column are not written again (for example the program's
start position appears once). The program settings are read once per program and refreshed every 5
minutes, so a burst of cycles does not slow the link. A value that cannot be worked out (the press
did not answer S2) is left empty, and a press that ignores S2 only logs a
`[CSV] Extra read failed` warning; the readings and the link are not affected. S2 is read-only and
cannot move the press. Which S2 item is the return speed (2030) was inferred from one press and is
worth checking against the press screen. If a file of the same name already exists with other columns
(an older version), the new rows go to a numbered file (`..._2.csv`) and the old one is untouched.

A folder that cannot be written (or an unknown mode) only logs a `[CSV]` warning; collection, the
local file and the database carry on.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `JANOME_PORT` | `8085` | Port the control panel listens on. |
| `JANOME_HOST` | `0.0.0.0` | Interface the control panel binds to. |
| `JANOME_DATA_DIR` | project folder | Where `config.json`, PID/state files, the pipeline log and the SQLite DB are written. Point it at a mounted volume in a container. If `config.json` is missing there, the bundled one is copied in on first start. |

The serial port is set in `config.json` (via the panel). In a container, pass
the device through (for example `/dev/ttyUSB0`) and set that path as the
serial port.

## Commands implemented

Read-only (safe, cannot move the press): `B0` (system info), `N0`/`N1`
(instructed/encoder position), `N2` (current load), `G1`/`G2`/`G3` (last
cycle end position/load/sensor value), `K0`/`K1` (I/O status), `Q0`/`Q1`
(stop/start quality-data streaming).

Control (physically moves the press / changes state - gated behind
confirmation in the diagnostic tool): `M0` (servo on/off), `P0`
(mechanical init/home), `P1` (change program), `P2` (return to work home),
`P3`/`P4` (start a cycle).

Deliberately **not** implemented: `K2`/`K3` (I/O output set/reset), `S1`
(data settings write), `U0` (data save). Their byte layout in the source
manual was ambiguous/incomplete, and guessing at the encoding for a WRITE
command against real hardware isn't something to do without being sure.

`S2` (Data Request - reads program settings like Pressing Speed, Start/End
Position, End Load) is implemented; item numbers are documented in
`serial_test_webapp.py`'s `POLLING_SET_ITEMS`, including two gaps (Return
Speed's item number was lost to a column-alignment issue in the source
PDF; "Prepare Time"/"Work Time" are not S2 settings at all - they only
exist in the `V1` Quality Data push frame, available via the Live Cycle
Monitor instead).
