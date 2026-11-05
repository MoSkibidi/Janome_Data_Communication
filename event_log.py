# event_log.py
# One persistent log of everything that goes wrong (or recovers) around the press: the
# "error log". Every event is one line in logs/events.log next to config.json and is also
# written to the normal log, so it shows in the panel log as well.
#
#   2026-11-05 11:20:46 | ERROR   | RS232_LOST        | RS-232 port COM7 is not available ... | port=COM7
#
# Unlike the pipeline log (which starts empty on every Start) this file is only ever appended to,
# so the history survives Stop/Start, a crash and a reboot. It is rotated by size (events.log.1 ...).
#
# Event codes (the fail cases):
#   1  RS232_LOST, RS232_RECOVERED          USB/RS-232 adapter unplugged or the port cannot be opened
#      PRESS_NO_RESPONSE, PRESS_RECOVERED   port open but the press is silent (powered off, cable)
#   2  PRESS_NG                             a cycle finished with a non-zero result (NG / stop)
#   3  PRESS_RECOVERED                      the press answers again after power OFF/ON; reading resumes
#   4  SERVICE_START, UNCLEAN_SHUTDOWN, RUN_RESUMED, READER_RESTARTED   crash, reboot, auto-start
#   5  DB_FULL, DB_REJECTED, DB_RETENTION    InfluxDB out of space / refusing data / retention applied
#   6  DB_UNREACHABLE, DB_RESTORED, DATA_DROPPED   network or database down, data kept at the edge
#   7  CYCLE_GAP, CYCLE_COUNTER_RESET       stamping data check: cycles missing between two results
# English comments only

import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger("janome.events")

FILE_NAME = "events.log"
MAX_BYTES = 5 * 1024 * 1024
KEEP_FILES = 5
LEVELS = ("INFO", "WARNING", "ERROR")

_path = None
_lock = threading.Lock()


def default_path(config_path):
    """logs/events.log next to config.json; JANOME_EVENT_LOG overrides it."""
    override = os.environ.get("JANOME_EVENT_LOG")
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), "logs", FILE_NAME)


def configure(path):
    """Sets the file that emit() appends to (None = only the normal log)."""
    global _path
    _path = path
    if path:
        os.makedirs(os.path.dirname(path), exist_ok=True)


def current_path():
    return _path


def _clean(value):
    return str(value).replace("|", "/").replace("\r", " ").replace("\n", " ").strip()


def format_line(level, code, message, details=None, when=None):
    when = when or datetime.now().astimezone()
    extra = " ".join(f"{key}={_clean(value)}" for key, value in (details or {}).items() if value is not None)
    parts = [when.strftime("%Y-%m-%d %H:%M:%S"), f"{level:<7}", f"{code:<17}", _clean(message)]
    if extra:
        parts.append(extra)
    return " | ".join(parts)


def _rotate(path):
    """events.log -> events.log.1 -> ... -> events.log.<KEEP_FILES> (the oldest is overwritten)."""
    for number in range(KEEP_FILES - 1, 0, -1):
        if os.path.exists(f"{path}.{number}"):
            os.replace(f"{path}.{number}", f"{path}.{number + 1}")
    os.replace(path, f"{path}.1")


def emit(code, message, level="INFO", path=None, **details):
    """
    Records one event: a line in events.log and in the normal log. Never raises, so a problem
    with the log file can never stop the collection. Returns the formatted line.
    """
    level = level.upper() if level.upper() in LEVELS else "INFO"
    line = format_line(level, code, message, details)
    log = {"INFO": logger.info, "WARNING": logger.warning, "ERROR": logger.error}[level]
    log(f"[EVENT] {code} {_clean(message)}" + "".join(f" {k}={_clean(v)}" for k, v in details.items() if v is not None))
    target = path or _path
    if target:
        try:
            with _lock:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.exists(target) and os.path.getsize(target) > MAX_BYTES:
                    _rotate(target)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
        except Exception as err:  # the event is still in the normal log
            logger.warning(f"[EVENT] Could not write {target}: {err}")
    return line


def read_recent(path, limit=100, min_level=None):
    """The last `limit` events of the file, newest last, as dicts (time, level, code, message, details)."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    events = []
    for raw in lines[-max(1, int(limit)) * 2:]:
        parts = [p.strip() for p in raw.rstrip("\r\n").split(" | ", 4)]
        if len(parts) < 4:
            continue
        event = {"time": parts[0], "level": parts[1], "code": parts[2], "message": parts[3],
                 "details": parts[4] if len(parts) > 4 else ""}
        if min_level and LEVELS.index(event["level"] if event["level"] in LEVELS else "INFO") < LEVELS.index(min_level):
            continue
        events.append(event)
    return events[-int(limit):]


def uptime_seconds():
    """How long the computer has been up, or None when unknown. Tells a reboot from a plain restart."""
    try:
        if os.name == "nt":
            import ctypes
            return ctypes.windll.kernel32.GetTickCount64() / 1000.0
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except Exception:
        return None


def describe_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds // 3600} h {(seconds % 3600) // 60} min"
