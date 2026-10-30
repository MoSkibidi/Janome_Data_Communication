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
- The tables are created as plain tables. A TimescaleDB hypertable is not
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
