# Janome JP4 Data Communication

RS-232 COM-protocol driver, telemetry panel, and diagnostic tooling for the
**JANOME Electro Press JP4**.

## Protocol notes (confirmed against real hardware)

Janome doesn't publish an RS-232 command protocol for JP4 in its standard
manual set - only electrical specs ("RS-232C, 1ch, for PC control using
optional software"). The actual protocol used here comes from
`891817107_COM_control.pdf` (a separate JP4 COM-communication manual) and
has been verified live against a real JP4 press:

- **Baud rate: 38400 bps** (not the documented 9600 default - this unit's
  actual configured rate).
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
| `database_handler.py` | SQLite/PostgreSQL/InfluxDB storage backend for the ingestion driver. |
| `run_summary.py` | In-memory statistics and samples of the current/last run (Run summary card, CSV download). |
| `app.py` | Flask-SocketIO web control panel (port 8085) - configure the serial port, start/stop real or mock ingestion, view live telemetry. |
| `quick_test.py` | Minimal standalone CLI script - opens the port, sends a few commands, prints raw TX/RX bytes. No Flask/database dependency. |
| `serial_test_webapp.py` | Full diagnostic web app (port 8086) - command picker covering all read/control commands in plain language, a Poll Set for program settings (S2), a one-shot Quality Data capture, and a **Live Cycle Monitor** that logs every completed press cycle automatically (with link keep-alive). |
| `run_service.bat`, `deploy/` | Foreground launcher plus auto-start at boot: `install_autostart.ps1` / `uninstall_autostart.ps1` (Windows scheduled task) and `janome-panel.service` (Linux systemd). |
| `tests/test_fail_cases.py` | Fault-injection tests (fake press, fake InfluxDB): cable pulled, press power-cycle, NG cycle, network outage, restart with spool, retention. |
| `config.json` | Serial port, baud rate, database, and startup settings for `read_janome_jp4.py` / `app.py`. |

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

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

- **`none` (default, "log only"):** samples are read and shown in the log only;
  nothing is stored and no database file is created. The pipeline log is
  overwritten each time ingestion is started, so it only holds the current run.
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
- **`influxdb`:** same connection check, buffering and spool as above. Two
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

The panel keeps the readings of the current or last run in memory and shows a
**Run summary** card: min, max, average, standard deviation and latest value of
actual position, programmed position and load, plus a table of every reading.

- A run is everything from **Start** (or **Start simulation**) to **Stop**.
  Statistics cover the whole run, however long it is. They stay on screen after
  Stop, until the next Start replaces them.
- Data is **memory only**. It is lost when the panel restarts and nothing is
  written to disk (the database setting is independent of this).
- The table keeps the latest 50,000 readings (`RunSummary(max_samples=...)` in
  `app.py`). Statistics still cover every reading; the card says so when older
  readings were dropped. The browser shows the latest 2,000 rows.
- **Download CSV** (`/api/run.csv`, times in UTC) gives all stored readings.
  JSON endpoints: `/api/run` (summary) and `/api/run/samples?limit=N`.
- Each `[STATS]` line from the reader ends with `time=<ISO time>`, the moment the
  reading was taken; the panel uses it for the sample timestamps.
- If the panel starts while a reader is already running, the summary begins at
  that moment.

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
