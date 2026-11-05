# csv_export.py
# One CSV file written as the data arrives, so there is a small, human-readable record that opens
# straight in Excel: one row per finished press cycle (OK and NG). The first columns are the values
# of the cycle report (speeds, positions, stroke, end load, times); the rest of what the press reports
# for the cycle follows in groups that belong together. A value that would only repeat an earlier
# column is not written twice (for example the program's start position is one column).
#
# Two file layouts, chosen in the settings before a run:
#   "run": one file per Start-Stop run  ->  exports/cycles_run_2026-11-05_100635.csv
#   "day": one file per day             ->  exports/cycles_day_2026-11-05.csv
#          The day normally changes at midnight. "day_start" ("HH:MM", computer's local time) moves the
#          change to another time, for example a shift that starts at 06:30: cycles from 06:30 until
#          06:29 the next morning go in one file, named after the day it started and with the start time
#          when it is not midnight: exports/cycles_day_2026-11-05_0630.csv
# A restart of the reader inside the same run (or the same day) appends to the same file. If an
# existing file has other columns than the program writes now (an older version), the rows go to
# a numbered file next to it (..._2.csv) instead of being mixed into it.
# English comments only

import csv
import os
from datetime import datetime, timedelta, timezone

from database_handler import CYCLE_FIELDS

DEFAULT_FOLDER = "exports"
MODES = ("run", "day")
DEFAULT_DAY_START = "00:00"
KGF_PER_N = 1 / 9.80665  # the press screen shows loads in kg (kgf)

# Column order. The first block is the cycle report; the rest follow in groups that belong together.
REPORT_COLUMNS = [
    "pressing_speed_mm_s", "return_speed_mm_s", "start_position_mm", "stroke_mm",
    "end_position_mm", "end_load_kgf", "end_load_setting_kgf", "end_load_diff_kgf",
    "prep_time_s", "work_time_s",
]
COLUMNS = REPORT_COLUMNS + [
    "time_local", "time_utc", "run_id",                                   # when
    "program_number", "ok", "result_code", "result_text",                  # which program, result
    "end_position_set_mm", "end_load_n",                                   # set end position, end load in N
    "contact_position_mm", "contact_load_n",                               # where the ram touched the part
    "ok_total", "fault_total", "lifetime_total",                           # press counters
    "sensor_fault_section", "sensor_fault_number", "backlog",
]


def parse_day_start(text):
    """'HH:MM' (also 'H:MM' or 'HH:MM:SS') -> (hour, minute). Raises ValueError for anything else."""
    parts = str(text if text is not None else DEFAULT_DAY_START).strip().split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"day start must look like 06:30, not {text!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"day start must be between 00:00 and 23:59, not {text!r}")
    return hour, minute


def resolve_folder(csv_cfg, config_path):
    """Where the CSV files go: config 'csv_export.folder' (relative to config.json) or ./exports."""
    base = os.path.dirname(os.path.abspath(config_path))
    folder = (csv_cfg or {}).get("folder") or DEFAULT_FOLDER
    return folder if os.path.isabs(folder) else os.path.join(base, folder)


def _cell(kind, value):
    if value is None:
        return ""
    if kind == "bool":
        return 1 if value else 0
    return value


def report_values(cycle, settings=None, targets=None):
    """
    The derived values of the cycle report.
      pressing/return speed, start/end position: the program's settings read from the press (S2)
      stroke_mm:            end position set - start position (both settings)
      end_load_kgf:         the measured end load in kg, as the press screen shows it
      end_load_setting_kgf: the target from the configuration (not stored in the press)
      end_load_diff_kgf:    end_load_kgf - end_load_setting_kgf (positive = more load than the setting)
    A value that cannot be worked out is None (an empty cell).
    """
    settings, targets = settings or {}, targets or {}
    start = settings.get("set_start_position_mm")
    end_set = settings.get("set_end_position_mm")
    end_load = cycle.get("end_load_n")
    load_kgf = round(end_load * KGF_PER_N, 1) if end_load is not None else None
    load_setting = targets.get("end_load_setting_kgf")
    return {
        "pressing_speed_mm_s": settings.get("set_pressing_speed_mm_s"),
        "return_speed_mm_s": settings.get("set_return_speed_mm_s"),
        "start_position_mm": start,
        "end_position_set_mm": end_set,
        "stroke_mm": round(end_set - start, 4) if start is not None and end_set is not None else None,
        "end_load_kgf": load_kgf,
        "end_load_setting_kgf": load_setting,
        "end_load_diff_kgf": round(load_kgf - load_setting, 1) if load_kgf is not None and load_setting is not None else None,
    }


class _Appender:
    """Appends rows to a file named after the run or the day; every row is flushed and synced to disk."""

    prefix = "file"
    columns = []

    def __init__(self, folder, mode="run", run_started=None, run_id=None, day_start=DEFAULT_DAY_START):
        if mode not in MODES:
            raise ValueError(f"csv_export mode must be one of {MODES}, not {mode!r}")
        self.day_start = parse_day_start(day_start)  # (hour, minute) when a new day's file begins
        self.folder = folder
        self.mode = mode
        self.run_id = run_id
        started = run_started or datetime.now().astimezone()
        self._run_stamp = started.astimezone().strftime("%Y-%m-%d_%H%M%S")
        self._resolved = {}  # planned file name -> the file actually used (see _usable)
        os.makedirs(folder, exist_ok=True)

    def path_for(self, when):
        """The file a row that arrived at `when` (a datetime) belongs in."""
        if self.mode == "day":
            hour, minute = self.day_start
            # the day a row belongs to is the date `day_start` ago: with a 06:30 start, 02:00 is still yesterday
            local = when.astimezone().replace(tzinfo=None) - timedelta(hours=hour, minutes=minute)
            suffix = f"_{hour:02d}{minute:02d}" if (hour, minute) != (0, 0) else ""
            return os.path.join(self.folder, f"{self.prefix}_day_{local.strftime('%Y-%m-%d')}{suffix}.csv")
        return os.path.join(self.folder, f"{self.prefix}_run_{self._run_stamp}.csv")

    def _usable(self, planned):
        """The planned file, or a numbered one next to it when the planned file has other columns."""
        if planned in self._resolved:
            return self._resolved[planned]
        stem, ext = os.path.splitext(planned)
        path, number = planned, 1
        while os.path.exists(path) and os.path.getsize(path) > 0:
            with open(path, encoding="utf-8", newline="") as handle:
                if handle.readline().rstrip("\r\n") == ",".join(self.columns):
                    break
            number += 1
            path = f"{stem}_{number}{ext}"
        self._resolved[planned] = path
        return path

    def _times(self, when):
        return [
            when.astimezone().isoformat(timespec="milliseconds"),
            when.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            "" if self.run_id is None else self.run_id,
        ]

    def _append(self, when, row):
        path = self._usable(self.path_for(when))
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            if new_file:
                writer.writerow(self.columns)
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        return path


class CsvExporter(_Appender):
    """One row per finished cycle."""

    prefix = "cycles"
    columns = COLUMNS

    def add_cycle(self, when, cycle, settings=None, targets=None):
        """
        Writes one cycle. `when` is a timezone-aware datetime; `cycle` uses the CYCLE_FIELDS names;
        `settings` are the program settings read from the press and `targets` the configured targets
        (see report_values).
        """
        values = dict(zip(("time_local", "time_utc", "run_id"), self._times(when)))
        values.update(cycle)
        values.update(report_values(cycle, settings, targets))
        kinds = dict(CYCLE_FIELDS)
        return self._append(when, [_cell(kinds.get(name), values.get(name)) for name in COLUMNS])
