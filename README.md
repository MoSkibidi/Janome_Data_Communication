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
