# local_store.py
# Small SQLite file on this computer that keeps what the reader collected: every reading and
# every finished press cycle, grouped by run (Start to Stop). It is written whether or not a
# database is configured, so the data is on disk even while the database server is down.
# English comments only

import csv
import io
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

DEFAULT_FILE = "janome_jp4_local.db"
PRUNE_EVERY = 5000  # inserts between size checks


def resolve_path(local_cfg, config_path):
    """Where the local file lives: config 'local_store.path' (relative to config.json) or next to config.json."""
    base = os.path.dirname(os.path.abspath(config_path))
    path = (local_cfg or {}).get("path") or DEFAULT_FILE
    return path if os.path.isabs(path) else os.path.join(base, path)


def _now():
    return datetime.now(timezone.utc).isoformat()


class LocalStore:
    """
    Runs, readings and cycles in one SQLite file (WAL mode, so the panel can read while the
    reader writes). Every write is committed with synchronous=FULL, which survives a crash or a
    power cut. The file is bounded: beyond max_rows readings the oldest ones are deleted.
    """

    def __init__(self, path, max_rows=2000000):
        self.path = path
        self.max_rows = max(int(max_rows), 1000)
        self._lock = threading.Lock()
        self._since_prune = 0
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    mode TEXT
                );
                CREATE TABLE IF NOT EXISTS samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    encoder_position_mm REAL NOT NULL,
                    teaching_position_mm REAL NOT NULL,
                    load_n REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS samples_run ON samples (run_id, id);
                CREATE TABLE IF NOT EXISTS cycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cycles_run ON cycles (run_id, id);
            """)

    # ---- runs

    def start_run(self, mode=None, when=None):
        with self._lock, self.conn:
            cur = self.conn.execute("INSERT INTO runs (started_at, mode) VALUES (?, ?)",
                                    ((when or datetime.now(timezone.utc)).isoformat(), mode))
            run_id = cur.lastrowid
        self.prune()
        return run_id

    def end_run(self, run_id, when=None):
        """Marks the run as ended (no effect if it already is)."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE runs SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                              ((when or datetime.now(timezone.utc)).isoformat(), run_id))

    def reopen_run(self, run_id):
        """Continues a run after the reader was restarted automatically."""
        with self._lock, self.conn:
            self.conn.execute("UPDATE runs SET ended_at = NULL WHERE id = ?", (run_id,))

    def get_run(self, run_id):
        with self._lock:
            row = self.conn.execute("SELECT id, started_at, ended_at, mode FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(zip(("id", "started_at", "ended_at", "mode"), row)) if row else None

    def latest_run(self):
        with self._lock:
            row = self.conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return self.get_run(row[0]) if row else None

    def last_activity(self, run_id):
        """Time of the newest reading or cycle of the run (ISO text), or None."""
        with self._lock:
            a = self.conn.execute("SELECT MAX(ts) FROM samples WHERE run_id = ?", (run_id,)).fetchone()[0]
            b = self.conn.execute("SELECT MAX(ts) FROM cycles WHERE run_id = ?", (run_id,)).fetchone()[0]
        return max(x for x in (a, b) if x) if (a or b) else None

    # ---- data

    def add_sample(self, run_id, when, data):
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO samples (run_id, ts, encoder_position_mm, teaching_position_mm, load_n) VALUES (?, ?, ?, ?, ?)",
                (run_id, when.isoformat(), data["encoder_position_mm"], data["teaching_position_mm"], data["load_n"]))
        self._maybe_prune()

    def add_cycle(self, run_id, when, cycle):
        with self._lock, self.conn:
            self.conn.execute("INSERT INTO cycles (run_id, ts, payload) VALUES (?, ?, ?)",
                              (run_id, when.isoformat(), json.dumps(dict(cycle, time=when.isoformat()))))

    def last_lifetime_total(self, mode=None):
        """Press cycle counter (lifetime_total) of the newest saved cycle of runs in `mode`, or None."""
        sql = "SELECT c.payload FROM cycles c JOIN runs r ON r.id = c.run_id"
        args = ()
        if mode is not None:
            sql += " WHERE r.mode = ?"
            args = (mode,)
        with self._lock:
            row = self.conn.execute(sql + " ORDER BY c.id DESC LIMIT 1", args).fetchone()
        try:
            return json.loads(row[0]).get("lifetime_total") if row else None
        except (ValueError, AttributeError):
            return None

    def count_samples(self, run_id=None):
        with self._lock:
            if run_id is None:
                return self.conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            return self.conn.execute("SELECT COUNT(*) FROM samples WHERE run_id = ?", (run_id,)).fetchone()[0]

    def run_samples(self, run_id, batch=5000):
        """Yields (iso_time, encoder, teaching, load) of a run, oldest first, without loading it all at once."""
        last_id = 0
        while True:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT id, ts, encoder_position_mm, teaching_position_mm, load_n FROM samples "
                    "WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?", (run_id, last_id, batch)).fetchall()
            if not rows:
                return
            last_id = rows[-1][0]
            for _, ts, e, t, l in rows:
                yield ts, e, t, l

    def run_cycles(self, run_id):
        with self._lock:
            rows = self.conn.execute("SELECT payload FROM cycles WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def csv_text(self, run_id):
        """Every reading of the run as CSV (times in UTC)."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["time_utc", "encoder_position_mm", "teaching_position_mm", "load_n"])
        for row in self.run_samples(run_id):
            writer.writerow(row)
        return out.getvalue()

    # ---- size limit

    def _maybe_prune(self):
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self.prune()

    def prune(self):
        """Deletes the oldest readings beyond max_rows (and cycles beyond a tenth of that), then empty ended runs."""
        self._since_prune = 0
        with self._lock, self.conn:
            for table, limit in (("samples", self.max_rows), ("cycles", max(self.max_rows // 10, 1000))):
                excess = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] - limit
                if excess > 0:
                    self.conn.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY id LIMIT ?)", (excess,))
            self.conn.execute(
                "DELETE FROM runs WHERE ended_at IS NOT NULL "
                "AND id NOT IN (SELECT DISTINCT run_id FROM samples) AND id NOT IN (SELECT DISTINCT run_id FROM cycles)")

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
