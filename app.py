# app.py
# Janome JP4 Electro Press Web Control Panel (Port 8085)
# English comments only

try:
    import eventlet
    eventlet.monkey_patch()
    import eventlet.tpool  # used by the log tail thread; not loaded by "import eventlet" alone
except Exception as _e:
    print(f"[WARNING] Eventlet initialization warning: {_e}")

import os
import sys
import json
import subprocess
import threading
import re
import shutil
import time
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO, emit
import atexit
import local_store
import folder_browser
import event_log
import autostart
from run_summary import RunSummary, restore_from_store

# Built with PyInstaller (installer/): the program is JanomeJP4.exe, its templates and static files are unpacked
# next to it in _internal (sys._MEIPASS), and the writable files (config, data, logs) stay beside the exe.
FROZEN = bool(getattr(sys, 'frozen', False))
RESOURCE_DIR = getattr(sys, '_MEIPASS', None) or os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, template_folder=os.path.join(RESOURCE_DIR, 'templates'), static_folder=os.path.join(RESOURCE_DIR, 'static'))
socketio = SocketIO(app, cors_allowed_origins="*")

# Writable runtime files (config, PID, state, log, SQLite DB) live in DATA_DIR.
# Defaults to the project folder; set JANOME_DATA_DIR to a mounted volume in a container.
DATA_DIR = os.path.abspath(os.environ.get('JANOME_DATA_DIR', BASE_DIR))
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
EVENTS_PATH = event_log.default_path(CONFIG_PATH)  # the persistent error/event log
event_log.configure(EVENTS_PATH)
PID_PATH = os.path.join(DATA_DIR, '.janome_jp4_process.pid')
MODE_PATH = os.path.join(DATA_DIR, '.janome_jp4_process.mode')
DESIRED_STATE_PATH = os.path.join(DATA_DIR, '.janome_jp4_desired_state.json')
LOG_PATH = os.path.join(DATA_DIR, 'janome_jp4_pipeline.log')
HEARTBEAT_PATH = os.path.join(DATA_DIR, '.janome_jp4_heartbeat.json')
STOP_MARKER_PATH = os.path.join(DATA_DIR, '.janome_jp4_clean_stop')  # written by stop.bat: the stop is deliberate

# Seed an empty data dir with the bundled config (its serial settings differ from the built-in defaults).
_SEED_CONFIG = os.path.join(RESOURCE_DIR, 'config.json')
if not os.path.exists(CONFIG_PATH) and os.path.exists(_SEED_CONFIG):
    shutil.copyfile(_SEED_CONFIG, CONFIG_PATH)

tail_thread = None
stop_tail_event = threading.Event()
last_stats = {}
last_link = {}  # latest "[LINK] state=..." report from the reader: is the press answering?
reader_proc = None  # Popen of the reader this panel started (None after a panel restart)
launch_lock = threading.Lock()
SUPERVISE_EVERY_S = 5
run_summary = RunSummary()  # statistics and samples of the current/last run (rebuilt from the local file after a restart)
store = None           # local SQLite file holding every run (see local_store.py)
current_run_id = None  # run in the local file that the reader is adding to

def read_config():
    """Reads configuration parameters from config.json."""
    default_port = "COM1" if sys.platform == "win32" else "/dev/cu.usbserial-jp4"
    default_config = {
        "startup": {
            "auto_start_on_startup": False,
            "auto_start_mode": "mockup"
        },
        "serial": {
            "port": default_port,
            "baudrate": 9600,
            "timeout": 2.0
        },
        "database": {
            "db_type": "influxdb",
            "db_name": "janome_jp4_data.db",
            "table_name": "janome_jp4_telemetry",
            "host": "localhost",
            "port": 5432,
            "user": "admin",
            "password": "admin",
            "sqlite_path": "janome_jp4_data.db",
            "influx_url": "http://localhost:8086",
            "influx_version": "2",
            "influx_token": "janome-local-dev-token",
            "influx_org": "janome",
            "influx_bucket": "janome_jp4",
            "influx_measurement": "janome_jp4_telemetry",
            "influx_cycle_measurement": "janome_jp4_telemetry_cycles",
            "influx_retention_days": 30,
            "description": "Database storage for JANOME Electro Press JP4 telemetry data"
        },
        "acquisition": {
            "interval_time": 1
        }
    }
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                for key in default_config:
                    if key not in cfg:
                        cfg[key] = default_config[key]
                return cfg
    except Exception as e:
        print(f"[ERROR] Reading config.json: {e}")
    return default_config

def write_config(config_data):
    """Writes configuration parameters to config.json."""
    try:
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(config_data, f, indent=2)
        return True
    except Exception as e:
        print(f"[ERROR] Writing config.json: {e}")
        return False

def open_store():
    """Opens the local file the reader saves into; the panel reads it to rebuild the last run after a restart."""
    global store
    local_cfg = read_config().get('local_store', {})
    if not local_cfg.get('enabled', True):
        return None
    try:
        store = local_store.LocalStore(local_store.resolve_path(local_cfg, CONFIG_PATH),
                                       max_rows=int(local_cfg.get('max_rows', 2000000)))
    except Exception as e:
        print(f"[ERROR] Cannot open the local file: {e}")
        store = None
    return store

def finish_run():
    """Marks the current run as ended, in memory and in the local file."""
    run_summary.finish()
    if store is not None and current_run_id is not None:
        try:
            store.end_run(current_run_id)
        except Exception as e:
            print(f"[ERROR] Ending the run in the local file: {e}")

def restore_last_run():
    """After a panel restart: show the last run again, rebuilt from the local file."""
    global current_run_id
    if store is None:
        return
    try:
        run = store.latest_run()
        if run is None:
            return
        current_run_id = run['id']
        restore_from_store(run_summary, store, run['id'])
        pid, _ = get_running_process()
        if run['ended_at'] is None and pid is None:
            # The reader is gone (the computer was switched off, for example): close the run at its last reading
            when = store.last_activity(run['id'])
            ended = datetime.fromisoformat(when) if when else None
            store.end_run(run['id'], ended)
            run_summary.finish(ended)
    except Exception as e:
        print(f"[ERROR] Restoring the last run: {e}")

def read_desired_state():
    """Reads persistent desired state metadata across reboots."""
    try:
        if os.path.exists(DESIRED_STATE_PATH):
            with open(DESIRED_STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[ERROR] Reading desired state: {e}")
    return {"is_running": False, "mode": "mockup"}

def write_desired_state(is_running, mode="mockup"):
    """Writes persistent desired state metadata across system reboots."""
    try:
        with open(DESIRED_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({"is_running": is_running, "mode": mode}, f, indent=2)
    except Exception as e:
        print(f"[ERROR] Writing desired state: {e}")

def is_pid_running(pid):
    if sys.platform == "win32":
        try:
            output = subprocess.check_output(f'tasklist /fi "PID eq {pid}"', shell=True)
            return str(pid) in str(output)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

def get_running_process():
    if os.path.exists(PID_PATH) and os.path.exists(MODE_PATH):
        try:
            with open(PID_PATH, 'r') as f:
                pid = int(f.read().strip())
            with open(MODE_PATH, 'r') as f:
                mode = f.read().strip()

            if is_pid_running(pid):
                return pid, mode
        except Exception as e:
            print(f"[ERROR] Checking active PID file: {e}")
    return None, None

def terminate_pid(pid):
    if sys.platform == "win32":
        try:
            subprocess.run(f"taskkill /pid {pid} /t /f", shell=True)
        except Exception as e:
            print(f"[ERROR] Terminating Windows PID {pid}: {e}")
    else:
        try:
            os.kill(pid, 15)  # SIGTERM
            for _ in range(30):
                if not is_pid_running(pid):
                    return
                time.sleep(0.1)
            os.kill(pid, 9)   # SIGKILL
        except (ProcessLookupError, OSError):
            pass

# E.g.: [STATS] polled=1 | written=1 | db_errors=0 | encoder_position_mm=41.5 | teaching_position_mm=41.5 | load_n=512.3
STATS_REGEX = re.compile(
    r"\[STATS\] polled=(?P<polled>[0-9,]+) \| written=(?P<written>[0-9,]+) \| db_errors=(?P<errors>[0-9]+) \| encoder_position_mm=(?P<encoder>-?[0-9\.]+) \| teaching_position_mm=(?P<teach>-?[0-9\.]+) \| load_n=(?P<load>-?[0-9\.]+)(?: \| db_state=(?P<db_state>\w+) \| db_pending=(?P<db_pending>[0-9]+) \| db_dropped=(?P<db_dropped>[0-9]+))?(?: \| time=(?P<time>\S+))?"
)

# E.g.: [LINK] state=no_response detail=Timeout waiting for response to command 'N1'. ...
LINK_REGEX = re.compile(r"\[LINK\] state=(?P<state>\w+)(?: detail=(?P<detail>.*))?")
# E.g.: [CYCLE] {"program_number": 1, "ok": false, "result_code": 3232, ..., "time": "2026-11-05T01:02:03.456+00:00"}
CYCLE_REGEX = re.compile(r"\[CYCLE\] (?P<json>\{.*\})")

def parse_and_emit_stats(line):
    """Parses stats, link-state and finished-cycle lines and updates global caches."""
    global last_stats, last_link
    link = LINK_REGEX.search(line)
    if link:
        last_link = {'state': link.group('state'), 'detail': (link.group('detail') or '').strip()}
        socketio.emit('link_update', last_link)
        return
    cycle_match = CYCLE_REGEX.search(line)
    if cycle_match:
        try:
            cycle = json.loads(cycle_match.group('json'))
            when = datetime.fromisoformat(cycle['time'])
        except (ValueError, KeyError):
            return
        run_summary.add_cycle(when, cycle)
        socketio.emit('cycle_update', cycle)
        socketio.emit('run_update', run_summary.snapshot())
        return
    match = STATS_REGEX.search(line)
    if match:
        try:
            read_at = datetime.fromisoformat(match.group('time')) if match.group('time') else datetime.now(timezone.utc)
        except ValueError:
            read_at = datetime.now(timezone.utc)
        run_summary.add(
            read_at,
            float(match.group('encoder')),
            float(match.group('teach')),
            float(match.group('load')),
        )
        socketio.emit('run_update', run_summary.snapshot())
        last_stats = {
            'polled': match.group('polled'),
            'written': match.group('written'),
            'errors': match.group('errors'),
            'encoder_position_mm': match.group('encoder'),
            'teaching_position_mm': match.group('teach'),
            'load_n': match.group('load'),
            'db_state': match.group('db_state'),
            'db_pending': match.group('db_pending'),
            'db_dropped': match.group('db_dropped'),
        }
        socketio.emit('stats_update', last_stats)

def tail_log_file(from_start=False, offset=None):
    """Background loop tailing the physical log file to feed sockets.

    from_start=True reads the file from its beginning; used for a run this panel just
    started, so no early sample is missed. offset starts at a given byte position (used when
    the reader is restarted and appends to the same log). Otherwise only new lines are followed.
    """
    print("[SYSTEM] Janome JP4 Log tailing thread started.")

    while not os.path.exists(LOG_PATH) and not stop_tail_event.is_set():
        eventlet.sleep(0.2)

    try:
        with open(LOG_PATH, 'r', errors='replace') as f:
            if offset is not None:
                f.seek(offset)
            else:
                f.seek(0, os.SEEK_SET if from_start else os.SEEK_END)

            while not stop_tail_event.is_set():
                # is_pid_running() shells out to tasklist on Windows, which
                # is a real blocking subprocess call - route it through
                # eventlet's thread pool for the same reason as the read
                # below (see comment there).
                pid, _ = eventlet.tpool.execute(get_running_process)
                if pid is None:
                    socketio.emit('status_change', {'is_running': False})
                    socketio.emit('link_update', {'state': 'idle', 'detail': ''})
                    finish_run()
                    socketio.emit('run_update', run_summary.snapshot())
                    if os.path.exists(PID_PATH):
                        try: os.remove(PID_PATH)
                        except: pass
                    if os.path.exists(MODE_PATH):
                        try: os.remove(MODE_PATH)
                        except: pass
                    break

                # Run the actual file read in eventlet's native thread pool.
                # Plain file I/O isn't truly cooperative under eventlet on
                # Windows - a blocking readline() here (e.g. while waiting
                # several seconds for the next line during slow real-hardware
                # polling) can stall the whole eventlet hub, making the
                # Flask-SocketIO server stop responding to new HTTP requests.
                line = eventlet.tpool.execute(f.readline)
                if not line:
                    eventlet.sleep(0.1)
                    continue

                decoded_line = line.strip()
                socketio.emit('log_update', {'log': decoded_line})
                parse_and_emit_stats(decoded_line)

    except Exception as e:
        print(f"[ERROR] Error tailing log file: {e}")
    finally:
        print("[SYSTEM] Janome JP4 Log tailing thread finished.")

def start_tailing(from_start=False, offset=None):
    """Starts a new background tailing thread if not active."""
    global tail_thread, stop_tail_event
    stop_tail_event.clear()
    if tail_thread is None or not tail_thread.is_alive():
        tail_thread = threading.Thread(target=tail_log_file, args=(from_start, offset), daemon=True)
        tail_thread.start()

def get_last_logs(count=50):
    """Retrieves last few log lines for newly connected clients."""
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, 'r', errors='replace') as f:
            lines = f.readlines()
            return [line.strip() for line in lines[-count:]]
    except Exception as e:
        print(f"[ERROR] Reading historical logs: {e}")
        return []

def scan_host_serial_devices():
    """Scans host PC for connected USB-serial ports and COM interfaces."""
    detected = []
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = p.description if p.description else p.device
            mfg = getattr(p, 'manufacturer', None) or 'USB Serial Device'
            detected.append({
                'id': p.device,
                'name': f"{p.device} ({desc})",
                'type': 'RS-232 / USB Serial Port',
                'port': p.device,
                'vendor': mfg,
                'hwid': getattr(p, 'hwid', '')
            })
    except Exception as e:
        print(f"[SCAN] Serial port enumeration note: {e}")

    if sys.platform != "win32":
        detected.append({
            'id': '/dev/cu.usbserial-jp4',
            'name': 'Default JP4 RS-232 Port (/dev/cu.usbserial-jp4)',
            'type': 'Configured POSIX Device',
            'port': '/dev/cu.usbserial-jp4',
            'vendor': 'FTDI'
        })
        detected.append({
            'id': '/dev/ttyUSB0',
            'name': 'Linux USB Serial Port (/dev/ttyUSB0)',
            'type': 'Linux TTY Serial',
            'port': '/dev/ttyUSB0',
            'vendor': 'Generic Serial'
        })
    else:
        detected.append({
            'id': 'COM1',
            'name': 'COM1 (Standard Windows Serial Port)',
            'type': 'Windows COM Port',
            'port': 'COM1',
            'vendor': 'System Port'
        })

    detected.append({
        'id': 'MOCK',
        'name': 'MOCK Virtual Hardware (Synthetic Simulation)',
        'type': 'Mockup / Driverless',
        'port': 'MOCK',
        'vendor': 'Software Mock'
    })

    seen = set()
    unique = []
    for d in detected:
        if d['id'] not in seen:
            seen.add(d['id'])
            unique.append(d)

    return unique

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(read_config())

@app.route('/api/config', methods=['POST'])
def save_config():
    config_data = request.json
    if not config_data:
        return jsonify({'status': 'error', 'message': 'Invalid payload'}), 400
    # Merge into what is on disk so settings the form does not show (spool path, reconnect interval ...) survive a save
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    except Exception:
        existing = {}
    for key, value in config_data.items():
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            existing[key].update(value)
        else:
            existing[key] = value
    config_data = existing
    if write_config(config_data):
        return jsonify({'status': 'success', 'config': config_data})
    return jsonify({'status': 'error', 'message': 'Failed to save configuration.'}), 500

LOCAL_ADDRESSES = ('127.0.0.1', '::1', '::ffff:127.0.0.1')

def local_request_only():
    """Folder browsing and Start with Windows touch this computer, so they are only served to a browser on this computer."""
    if request.remote_addr in LOCAL_ADDRESSES:
        return None
    return jsonify({'status': 'error', 'message': 'This can only be done from the computer the panel runs on.'}), 403

@app.route('/api/events')
def api_events():
    """The persistent event (error) log, newest last. ?limit=100&level=WARNING (WARNING or ERROR hides INFO)."""
    level = request.args.get('level', '').upper() or None
    try:
        limit = min(1000, max(1, int(request.args.get('limit', 100))))
    except ValueError:
        limit = 100
    events = event_log.read_recent(EVENTS_PATH, limit, level if level in event_log.LEVELS else None)
    return jsonify({'status': 'success', 'events': events})

@app.route('/api/events/download')
def api_events_download():
    try:
        with open(EVENTS_PATH, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        text = ''
    return Response(text, mimetype='text/plain', headers={'Content-Disposition': 'attachment; filename=events.log'})

@app.route('/api/browse', methods=['GET'])
def api_browse():
    """Sub-folders of a folder (relative paths are taken from the folder holding config.json)."""
    refused = local_request_only()
    if refused:
        return refused
    try:
        result = folder_browser.browse(request.args.get('path', '').strip(), DATA_DIR)
    except folder_browser.BrowseError as err:
        return jsonify({'status': 'error', 'message': str(err)}), 400
    return jsonify(dict(result, status='success'))

@app.route('/api/browse/mkdir', methods=['POST'])
def api_browse_mkdir():
    """Creates a new folder inside the folder being browsed."""
    refused = local_request_only()
    if refused:
        return refused
    body = request.get_json(silent=True) or {}
    try:
        path = folder_browser.make_folder(body.get('parent', ''), body.get('name', ''), DATA_DIR)
    except folder_browser.BrowseError as err:
        return jsonify({'status': 'error', 'message': str(err)}), 400
    return jsonify({'status': 'success', 'path': path})

@app.route('/api/autostart', methods=['GET'])
def api_autostart_status():
    """Is the 'Start with Windows' scheduled task installed, and as which kind (Logon / Startup)?"""
    info = autostart.status()
    info['this_computer'] = local_request_only() is None  # the buttons only work from the PC that runs the panel
    return jsonify(info)

@app.route('/api/autostart', methods=['POST'])
def api_autostart_change():
    """Turns 'Start with Windows' on ({'action': 'enable', 'mode': 'Logon'|'Startup'}) or off ({'action': 'disable'})."""
    refused = local_request_only()
    if refused:
        return refused
    body = request.get_json(silent=True) or {}
    if body.get('action') == 'disable':
        result = autostart.disable(BASE_DIR)
        panel_event('AUTOSTART', 'Start with Windows turned off.' if result['ok'] and not result['pending']
                    else f"Turning off Start with Windows: {result['message']}", level='INFO' if result['ok'] else 'WARNING')
    elif body.get('action') == 'enable':
        result = autostart.enable(str(body.get('mode', '')), BASE_DIR)
        panel_event('AUTOSTART', f"Start with Windows ({body.get('mode')}): {result['message']}",
                    level='INFO' if result['ok'] else 'WARNING')
    else:
        return jsonify({'status': 'error', 'message': "action must be 'enable' or 'disable'"}), 400
    return jsonify(dict(result, status='success' if result['ok'] else 'error')), (200 if result['ok'] else 400)

@app.route('/api/status', methods=['GET'])
def get_status():
    pid, mode = get_running_process()
    cfg = read_config()
    return jsonify({
        'is_running': pid is not None,
        'run_mode': mode or 'mockup',
        'serial_port': cfg.get('serial', {}).get('port', 'N/A'),
        'db_type': cfg.get('database', {}).get('db_type', 'none')
    })

@app.route('/api/run', methods=['GET'])
def api_run():
    """Statistics of the current/last run (whole run, Start to Stop)."""
    return jsonify(run_summary.snapshot())

@app.route('/api/run/samples', methods=['GET'])
def api_run_samples():
    """Latest samples of the current/last run, oldest first (default 2000)."""
    limit = request.args.get('limit', default=2000, type=int)
    snap = run_summary.snapshot()
    return jsonify({'samples': run_summary.samples(limit), 'count': snap['count'], 'stored': snap['stored']})

@app.route('/api/cycles', methods=['GET'])
def api_cycles():
    """Finished press cycles (OK and NG) of the current/last run, oldest first (default latest 500)."""
    limit = request.args.get('limit', default=500, type=int)
    return jsonify({'cycles': run_summary.cycles(limit), 'totals': run_summary.snapshot()['cycles']})

@app.route('/api/link', methods=['GET'])
def api_link():
    """Whether the press is answering (from the reader's [LINK] reports)."""
    pid, _ = get_running_process()
    return jsonify(last_link if pid is not None and last_link else {'state': 'idle' if pid is None else 'connecting', 'detail': ''})

@app.route('/api/run.csv', methods=['GET'])
def api_run_csv():
    """Every reading of the current/last run as a CSV download (all of them, from the local file when there is one)."""
    started = run_summary.snapshot()['started_at'] or ''
    stamp = re.sub(r'[^0-9]', '', started)[:14] or 'run'
    text = None
    if store is not None and current_run_id is not None:
        try:
            text = store.csv_text(current_run_id)
        except Exception as e:
            print(f"[ERROR] Reading the local file for the CSV: {e}")
    return Response(
        text if text is not None else run_summary.csv_text(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=janome_run_{stamp}.csv'}
    )

@app.route('/api/scan_serial', methods=['GET'])
def api_scan_serial():
    """Returns JSON list of detected serial interfaces on host PC."""
    devices = scan_host_serial_devices()
    return jsonify({
        'status': 'success',
        'count': len(devices),
        'devices': devices
    })

@app.route('/api/test_db', methods=['POST'])
def test_db():
    req = request.get_json() or {}
    db_cfg = req.get('database') or read_config().get('database', {})
    db_type = db_cfg.get('db_type', 'none')

    if db_type == 'none':
        return jsonify({'success': True, 'message': 'No database selected - readings are kept in the local SQLite file on this computer only.'})

    if db_type == 'influxdb':
        try:
            from database_handler import DatabaseHandler
            DatabaseHandler(db_cfg)  # connecting runs the reachability, credential and bucket checks
            target = db_cfg.get('influx_bucket') or db_cfg.get('influx_database')
            return jsonify({'success': True, 'message': f'InfluxDB at {db_cfg.get("influx_url") or "http://localhost:8086"} is reachable and accepts writes to "{target}".'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'InfluxDB connection error: {str(e)}'})

    if db_type in ('postgresql', 'timescaledb'):
        try:
            import psycopg2
            dsn = f"postgresql://{db_cfg.get('user', 'admin')}:{db_cfg.get('password', 'admin')}@{db_cfg.get('host', 'localhost')}:{db_cfg.get('port', 5432)}/{db_cfg.get('db_name', 'janome_jp4_data')}"
            conn = psycopg2.connect(dsn, connect_timeout=3)
            conn.close()
            return jsonify({'success': True, 'message': f'{db_type.upper()} connection to {db_cfg.get("host")}:{db_cfg.get("port")}/{db_cfg.get("db_name")} successful!'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'{db_type.upper()} connection error: {str(e)}'})
    else:
        try:
            import sqlite3
            db_name = db_cfg.get('sqlite_path') or db_cfg.get('db_name', 'janome_jp4_data.db')
            db_path = db_name if os.path.isabs(db_name) else os.path.join(DATA_DIR, db_name)
            conn = sqlite3.connect(db_path)
            conn.close()
            return jsonify({'success': True, 'message': f'SQLite connection to {db_name} successful!'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'SQLite connection error: {str(e)}'})

@app.route('/api/test_serial', methods=['POST'])
def test_serial():
    req = request.get_json() or {}
    serial_cfg = req.get('serial') or read_config().get('serial', {})
    port = serial_cfg.get('port', 'MOCK')
    baudrate = int(serial_cfg.get('baudrate', 9600))

    if port == 'MOCK':
        return jsonify({'success': True, 'message': 'MOCK serial mode test passed (synthetic hardware).'})

    try:
        import serial
        ser = serial.Serial(port=port, baudrate=baudrate, timeout=1.0)
        ser.close()
        return jsonify({'success': True, 'message': f'Serial port {port} opened successfully at {baudrate} bps!'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'Serial port {port} test error: {str(e)}'})

@socketio.on('connect')
def handle_connect():
    """Fires when browser client opens or refreshes the page."""
    pid, mode = get_running_process()
    is_active = pid is not None
    cfg = read_config()

    emit('status_change', {
        'is_running': is_active,
        'mode': mode or 'mockup',
        'port': cfg.get('serial', {}).get('port', 'N/A')
    })

    if is_active and last_stats:
        emit('stats_update', last_stats)

    emit('run_update', run_summary.snapshot())
    emit('link_update', last_link if is_active and last_link else {'state': 'connecting' if is_active else 'idle', 'detail': ''})

    logs = get_last_logs(50)
    for log_line in logs:
        emit('log_update', {'log': log_line})

    if is_active:
        start_tailing()

def launch_reader(run_mode, tolerate_db_down=False, fresh=True):
    """
    Spawns the reader process (read_janome_jp4.py) and starts following its log.

    fresh=True begins a new run (new log, new run summary). fresh=False is an automatic
    restart of a run that should still be going: the log is appended to and the run summary
    continues. tolerate_db_down lets the reader start while the database is unreachable and
    spool the data, which is what an unattended restart needs.
    """
    global reader_proc, last_link
    write_desired_state(True, run_mode)

    if FROZEN:
        cmd = [sys.executable, "--reader", "--config", CONFIG_PATH]  # the same exe, in reader mode (see janome_main.py)
    else:
        cmd = [sys.executable, os.path.join(BASE_DIR, "read_janome_jp4.py"), "--config", CONFIG_PATH]
    if run_mode == "mockup" or run_mode == "mock":
        cmd.append("--mock")
    if tolerate_db_down:
        cmd.append("--tolerate-db-down")

    global current_run_id
    if store is not None:
        try:
            if fresh or current_run_id is None:
                current_run_id = store.start_run(run_mode)
            else:
                store.reopen_run(current_run_id)  # automatic restart: same run
            cmd += ["--run-id", str(current_run_id)]
        except Exception as e:
            print(f"[ERROR] Starting a run in the local file: {e}")

    offset = None
    if fresh:
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            f.write(f"[SYSTEM] Log session initialized for JANOME JP4 mode={run_mode.upper()}\n")
    else:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(f"[SYSTEM] Reader process restarted automatically mode={run_mode.upper()}\n")
        offset = os.path.getsize(LOG_PATH)

    log_file = open(LOG_PATH, 'a', encoding='utf-8')

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    popen_kwargs = dict(
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=DATA_DIR,
    )
    if sys.platform != "win32":
        popen_kwargs["close_fds"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(cmd, **popen_kwargs)
    log_file.close()
    reader_proc = proc

    with open(PID_PATH, 'w') as f:
        f.write(str(proc.pid))
    with open(MODE_PATH, 'w') as f:
        f.write(run_mode)

    socketio.emit('status_change', {'is_running': True, 'mode': run_mode})
    socketio.emit('log_update', {'log': f'[SYSTEM] Spawning Janome JP4 process (PID: {proc.pid}) mode={run_mode.upper()}'})

    last_link = {}
    socketio.emit('link_update', {'state': 'connecting', 'detail': ''})
    if fresh:
        run_summary.start(run_mode)  # a new run replaces the previous run's summary
        panel_event('RUN_START', f"Reading started in {run_mode.upper()} mode (reader PID {proc.pid}).", mode=run_mode, run_id=current_run_id)
    else:
        run_summary.resume()
    socketio.emit('run_update', run_summary.snapshot())

    if fresh:
        start_tailing(from_start=True)
    else:
        start_tailing(offset=offset)
    return proc.pid

@socketio.on('start_janome')
def handle_start(data=None):
    """Spawns Janome JP4 ingestion process in background."""
    data = data or {}
    with launch_lock:
        pid, mode = get_running_process()
        if pid is not None:
            emit('log_update', {'log': '[SYSTEM] Warning: Janome JP4 ingestion process is already running.'})
            return
        try:
            launch_reader(data.get('mode', 'mockup'))
        except Exception as e:
            socketio.emit('log_update', {'log': f'[SYSTEM] Failed to spawn Janome JP4 process: {e}'})

def panel_event(code, message, level='INFO', **details):
    """Writes an event to the persistent event log and shows it in the panel log."""
    line = event_log.emit(code, message, level=level, **details)
    try:
        socketio.emit('log_update', {'log': '[EVENT] ' + line})
        socketio.emit('event_update', {'line': line})
    except Exception:
        pass

def read_heartbeat():
    try:
        with open(HEARTBEAT_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None

def write_heartbeat(clean=False):
    """Marks that the panel is alive. A file without clean=true at the next start means it was killed or lost power."""
    try:
        with open(HEARTBEAT_PATH, 'w', encoding='utf-8') as f:
            json.dump({'time': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(), 'clean': clean}, f)
    except Exception as e:
        print(f"[ERROR] Writing heartbeat: {e}")

def note_service_start():
    """Fail case 4: tells in the event log how the panel came up (boot, crash recovery, plain start)."""
    previous = read_heartbeat()
    was_running = bool(read_desired_state().get('is_running'))
    up = event_log.uptime_seconds()
    stop_requested = os.path.exists(STOP_MARKER_PATH)  # stop.bat leaves this before it kills the panel
    if stop_requested:
        try:
            os.remove(STOP_MARKER_PATH)
        except OSError:
            pass
    if previous and not previous.get('clean'):
        try:
            last = datetime.fromisoformat(previous['time'])
            ago = event_log.describe_duration((datetime.now(timezone.utc) - last).total_seconds())
            seen = f"last sign of life {last.astimezone().strftime('%Y-%m-%d %H:%M:%S')} ({ago} ago)"
        except Exception:
            seen = 'time of the last sign of life unknown'
        if stop_requested:
            panel_event('SERVICE_STOP', f"The panel was stopped with stop.bat; {seen}. (Recorded now, at the next start.)"
                        + (" A run was active, so it is resumed below." if was_running else ""), case='SHUTDOWN')
        elif was_running:
            panel_event('UNCLEAN_SHUTDOWN',
                        f"CRASH: the previous session ended without a clean shutdown (program crash, killed, power cut "
                        f"or reboot) while reading was active; {seen}. Reading is resumed below.",
                        level='ERROR', run_was_active='yes')
        else:
            panel_event('UNCLEAN_SHUTDOWN',
                        f"The previous session ended without a clean shutdown ({seen}); no run was active, so nothing was lost.",
                        case='STOP', run_was_active='no')
    booted = f"; the computer started {event_log.describe_duration(up)} ago" if up is not None and up < 900 else ''
    panel_event('SERVICE_START', f"Panel service started (port {os.environ.get('JANOME_PORT', '8085')}){booted}.",
                pid=os.getpid())
    write_heartbeat()

_stop_noted = [False]

def note_service_stop(reason):
    """A normal end of the panel (closed, Ctrl+C, computer shutdown/logoff): one SHUTDOWN line, heartbeat marked clean."""
    if _stop_noted[0]:
        return
    _stop_noted[0] = True
    event_log.emit('SERVICE_STOP', f"Panel stopped normally ({reason}). A run that was active resumes by itself at the next start.",
                   pid=os.getpid())
    write_heartbeat(clean=True)

def install_stop_hooks():
    """Catches the ways a normal shutdown reaches the program, so it is not mistaken for a crash at the next start."""
    atexit.register(lambda: note_service_stop('program closed'))
    try:
        if sys.platform == 'win32':
            import ctypes
            reasons = {2: 'console window closed', 5: 'user logoff', 6: 'computer shutdown'}
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def on_console_event(event):
                if event in reasons:
                    note_service_stop(reasons[event])
                return 0  # let Windows end the process as usual
            install_stop_hooks.keep = handler_type(on_console_event)  # must stay referenced
            ctypes.windll.kernel32.SetConsoleCtrlHandler(install_stop_hooks.keep, True)
        else:
            import signal

            def on_sigterm(signum, frame):
                note_service_stop('stop signal (SIGTERM)')
                sys.exit(0)
            signal.signal(signal.SIGTERM, on_sigterm)
    except Exception as e:
        print(f"[WARNING] Could not install the shutdown hook: {e}")

install_stop_hooks()

def supervise_reader():
    """
    Restarts the reader if it disappears while the operator wants it running (crash, killed,
    out of memory ...). The press link itself is kept alive inside the reader; this only covers
    the reader process. A reader that refuses to start because the database is unreachable
    (exit code 2) is not retried, so the error stays visible until the setting is fixed.
    """
    while True:
        eventlet.sleep(SUPERVISE_EVERY_S)
        try:
            write_heartbeat()
            desired = read_desired_state()
            if not desired.get('is_running'):
                continue
            with launch_lock:
                pid, _ = eventlet.tpool.execute(get_running_process)
                if pid is not None:
                    continue
                code = reader_proc.poll() if reader_proc is not None else None
                mode = desired.get('mode', 'mockup')
                if code == 2:
                    write_desired_state(False, mode)
                    socketio.emit('log_update', {'log': '[SYSTEM] The reader could not start because the database is unreachable. Not retrying automatically: fix the database setting, then press Start.'})
                    continue
                socketio.emit('log_update', {'log': f'[SYSTEM] Reader process is not running (exit code {code}); restarting it automatically.'})
                if code is not None and code > 0x7FFFFFFF:
                    code -= 1 << 32  # Windows reports a killed process as 4294967295 (-1)
                panel_event('READER_RESTARTED', f"CRASH: the reader process died (exit code {code}: crashed or was killed); "
                            f"restarting it automatically, same run, no operator needed.", level='ERROR', exit_code=code)
                launch_reader(mode, tolerate_db_down=True, fresh=False)
        except Exception as e:
            print(f"[ERROR] Reader supervisor: {e}")

@socketio.on('stop_janome')
def handle_stop():
    """Stops the background process by its recorded PID."""
    pid, _ = get_running_process()
    if pid is None:
        socketio.emit('status_change', {'is_running': False})
        finish_run()
        socketio.emit('run_update', run_summary.snapshot())
        emit('log_update', {'log': '[SYSTEM] Warning: Ingestion process is not running. Resetting UI state.'})
        if os.path.exists(PID_PATH):
            try: os.remove(PID_PATH)
            except: pass
        if os.path.exists(MODE_PATH):
            try: os.remove(MODE_PATH)
            except: pass
        return

    current_state = read_desired_state()
    write_desired_state(False, current_state.get('mode', 'mockup'))
    socketio.emit('log_update', {'log': f'[SYSTEM] Terminating Janome JP4 process (PID: {pid})...'})

    global stop_tail_event
    stop_tail_event.set()

    terminate_pid(pid)

    if os.path.exists(PID_PATH):
        try: os.remove(PID_PATH)
        except: pass
    if os.path.exists(MODE_PATH):
        try: os.remove(MODE_PATH)
        except: pass

    finish_run()
    socketio.emit('run_update', run_summary.snapshot())
    socketio.emit('status_change', {'is_running': False})
    socketio.emit('link_update', {'state': 'idle', 'detail': ''})
    socketio.emit('log_update', {'log': '[SYSTEM] Janome JP4 Ingestion process terminated.'})
    panel_event('RUN_STOP', 'Reading stopped by the operator (not a failure; it will not restart by itself).', pid=pid)

# Initial recovery check on Web GUI startup
open_store()
restore_last_run()
note_service_start()
pid, mode = get_running_process()
if pid is not None:
    print(f"[SYSTEM] Detected active Janome JP4 process running (PID: {pid}). Re-attaching...")
    start_tailing()
else:
    cfg = read_config()
    startup_cfg = cfg.get('startup', {})
    auto_start_enabled = startup_cfg.get('auto_start_on_startup', False)
    desired_state = read_desired_state()
    is_desired_running = desired_state.get('is_running', False)

    if auto_start_enabled or is_desired_running:
        target_mode = startup_cfg.get('auto_start_mode') or desired_state.get('mode', 'mockup')
        print(f"[SYSTEM] Startup config auto_start_on_startup is enabled. Auto-starting Janome JP4 ingestion in MODE={target_mode.upper()}...")
        reason = ("a run was active when the panel went down" if is_desired_running
                  else "\"Start reading automatically\" is ticked")
        panel_event('RUN_RESUMED', f"Reading resumes by itself in {target_mode.upper()} mode, no manual Start needed ({reason}).",
                    mode=target_mode)
        try:
            # Nobody is watching after a power-up, so do not give up if the database is not reachable yet
            launch_reader(target_mode, tolerate_db_down=True)
        except Exception as e:
            print(f"[ERROR] Auto-start failed: {e}")
            panel_event('READER_RESTARTED', f"Automatic start of the reader failed: {e}", level='ERROR')
    else:
        print("[SYSTEM] Startup config auto_start_on_startup is disabled. Awaiting manual start trigger.")

threading.Thread(target=supervise_reader, daemon=True).start()

def main():
    # Served on Port 8085 unless JANOME_HOST / JANOME_PORT override it
    socketio.run(
        app,
        host=os.environ.get('JANOME_HOST', '0.0.0.0'),
        port=int(os.environ.get('JANOME_PORT', '8085')),
        debug=False,
    )


if __name__ == '__main__':
    main()
