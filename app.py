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
from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Writable runtime files (config, PID, state, log, SQLite DB) live in DATA_DIR.
# Defaults to the project folder; set JANOME_DATA_DIR to a mounted volume in a container.
DATA_DIR = os.path.abspath(os.environ.get('JANOME_DATA_DIR', BASE_DIR))
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
PID_PATH = os.path.join(DATA_DIR, '.janome_jp4_process.pid')
MODE_PATH = os.path.join(DATA_DIR, '.janome_jp4_process.mode')
DESIRED_STATE_PATH = os.path.join(DATA_DIR, '.janome_jp4_desired_state.json')
LOG_PATH = os.path.join(DATA_DIR, 'janome_jp4_pipeline.log')

# Seed an empty data dir with the bundled config (its serial settings differ from the built-in defaults).
_SEED_CONFIG = os.path.join(BASE_DIR, 'config.json')
if not os.path.exists(CONFIG_PATH) and os.path.exists(_SEED_CONFIG):
    shutil.copyfile(_SEED_CONFIG, CONFIG_PATH)

tail_thread = None
stop_tail_event = threading.Event()
last_stats = {}

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
            "db_type": "none",
            "db_name": "janome_jp4_data.db",
            "table_name": "janome_jp4_telemetry",
            "host": "localhost",
            "port": 5432,
            "user": "admin",
            "password": "admin",
            "sqlite_path": "janome_jp4_data.db",
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
    r"\[STATS\] polled=(?P<polled>[0-9,]+) \| written=(?P<written>[0-9,]+) \| db_errors=(?P<errors>[0-9]+) \| encoder_position_mm=(?P<encoder>-?[0-9\.]+) \| teaching_position_mm=(?P<teach>-?[0-9\.]+) \| load_n=(?P<load>-?[0-9\.]+)(?: \| db_state=(?P<db_state>\w+) \| db_pending=(?P<db_pending>[0-9]+) \| db_dropped=(?P<db_dropped>[0-9]+))?"
)

def parse_and_emit_stats(line):
    """Parses stats from a line and updates global caches."""
    global last_stats
    match = STATS_REGEX.search(line)
    if match:
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

def tail_log_file():
    """Background loop tailing the physical log file to feed sockets."""
    print("[SYSTEM] Janome JP4 Log tailing thread started.")

    while not os.path.exists(LOG_PATH) and not stop_tail_event.is_set():
        eventlet.sleep(0.2)

    try:
        with open(LOG_PATH, 'r', errors='replace') as f:
            f.seek(0, os.SEEK_END)

            while not stop_tail_event.is_set():
                # is_pid_running() shells out to tasklist on Windows, which
                # is a real blocking subprocess call - route it through
                # eventlet's thread pool for the same reason as the read
                # below (see comment there).
                pid, _ = eventlet.tpool.execute(get_running_process)
                if pid is None:
                    socketio.emit('status_change', {'is_running': False})
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

def start_tailing():
    """Starts a new background tailing thread if not active."""
    global tail_thread, stop_tail_event
    stop_tail_event.clear()
    if tail_thread is None or not tail_thread.is_alive():
        tail_thread = threading.Thread(target=tail_log_file, daemon=True)
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
    if write_config(config_data):
        return jsonify({'status': 'success', 'config': config_data})
    return jsonify({'status': 'error', 'message': 'Failed to save configuration.'}), 500

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
        return jsonify({'success': True, 'message': 'No database selected - samples are shown in the log only and are not stored.'})

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

    logs = get_last_logs(50)
    for log_line in logs:
        emit('log_update', {'log': log_line})

    if is_active:
        start_tailing()

@socketio.on('start_janome')
def handle_start(data=None):
    """Spawns Janome JP4 ingestion process in background."""
    data = data or {}
    pid, mode = get_running_process()
    if pid is not None:
        emit('log_update', {'log': '[SYSTEM] Warning: Janome JP4 ingestion process is already running.'})
        return

    run_mode = data.get('mode', 'mockup')
    write_desired_state(True, run_mode)

    script_path = os.path.join(BASE_DIR, "read_janome_jp4.py")

    cmd = [sys.executable, script_path, "--config", CONFIG_PATH]
    if run_mode == "mockup" or run_mode == "mock":
        cmd.append("--mock")

    try:
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            f.write(f"[SYSTEM] Log session initialized for JANOME JP4 mode={run_mode.upper()}\n")

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

        with open(PID_PATH, 'w') as f:
            f.write(str(proc.pid))
        with open(MODE_PATH, 'w') as f:
            f.write(run_mode)

        socketio.emit('status_change', {'is_running': True, 'mode': run_mode})
        socketio.emit('log_update', {'log': f'[SYSTEM] Spawning Janome JP4 process (PID: {proc.pid}) mode={run_mode.upper()}'})

        start_tailing()

    except Exception as e:
        socketio.emit('log_update', {'log': f'[SYSTEM] Failed to spawn Janome JP4 process: {e}'})

@socketio.on('stop_janome')
def handle_stop():
    """Stops the background process by its recorded PID."""
    pid, _ = get_running_process()
    if pid is None:
        socketio.emit('status_change', {'is_running': False})
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

    socketio.emit('status_change', {'is_running': False})
    socketio.emit('log_update', {'log': '[SYSTEM] Janome JP4 Ingestion process terminated.'})

# Initial recovery check on Web GUI startup
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
        handle_start({'mode': target_mode})
    else:
        print("[SYSTEM] Startup config auto_start_on_startup is disabled. Awaiting manual start trigger.")

if __name__ == '__main__':
    # Served on Port 8085 unless JANOME_HOST / JANOME_PORT override it
    socketio.run(
        app,
        host=os.environ.get('JANOME_HOST', '0.0.0.0'),
        port=int(os.environ.get('JANOME_PORT', '8085')),
        debug=False,
    )
