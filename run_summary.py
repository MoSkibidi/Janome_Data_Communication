# run_summary.py
# In-memory summary of the current (or last) ingestion run: statistics over the
# whole run plus the samples themselves. Nothing is written to disk.
# English comments only

import csv
import io
import math
import threading
from collections import deque
from datetime import datetime, timezone


class _Accumulator:
    """Running min / max / mean / standard deviation without keeping every value (Welford's method)."""

    def __init__(self):
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0
        self.min = None
        self.max = None
        self.last = None

    def add(self, x):
        self.n += 1
        delta = x - self.mean
        self.mean += delta / self.n
        self._m2 += delta * (x - self.mean)
        self.min = x if self.min is None else min(self.min, x)
        self.max = x if self.max is None else max(self.max, x)
        self.last = x

    def as_dict(self):
        if self.n == 0:
            return None
        std = math.sqrt(self._m2 / (self.n - 1)) if self.n > 1 else 0.0
        return {
            "min": round(self.min, 4),
            "max": round(self.max, 4),
            "mean": round(self.mean, 4),
            "std": round(std, 4),
            "last": round(self.last, 4),
        }


class RunSummary:
    """
    Statistics and samples for one run (from Start to Stop).

    Statistics always cover every sample of the run. The samples themselves are kept
    in a bounded buffer (the most recent max_samples) so a run that is left going for
    days cannot use unlimited memory.
    """

    SIGNALS = ("encoder_position_mm", "teaching_position_mm", "load_n")

    def __init__(self, max_samples=50000, max_cycles=2000):
        self.max_samples = max_samples
        self.max_cycles = max_cycles
        self._lock = threading.Lock()
        self._clear(None)

    def _clear(self, mode):
        self.mode = mode
        self.started_at = None
        self.ended_at = None
        self.count = 0
        self._samples = deque(maxlen=self.max_samples)
        self._acc = {name: _Accumulator() for name in self.SIGNALS}
        self._cycles = deque(maxlen=self.max_cycles)
        self.cycle_ok = 0
        self.cycle_ng = 0

    def start(self, mode=None, when=None):
        """Begins a new run and discards the previous one."""
        with self._lock:
            self._clear(mode)
            self.started_at = when or datetime.now(timezone.utc)

    def resume(self):
        """Continues the same run after the reader was restarted automatically."""
        with self._lock:
            if self.started_at is not None:
                self.ended_at = None

    def finish(self, when=None):
        """Marks the run as ended; the data stays available until the next start()."""
        with self._lock:
            if self.started_at is not None and self.ended_at is None:
                self.ended_at = when or datetime.now(timezone.utc)

    def add(self, when, encoder, teaching, load):
        """Records one sample read from the press at time `when`."""
        with self._lock:
            if self.ended_at is not None:
                return  # late line from a run that has already ended
            if self.started_at is None:
                self.started_at = when  # attached to a run that began before this panel started
            self.count += 1
            self._samples.append((when, encoder, teaching, load))
            for name, value in zip(self.SIGNALS, (encoder, teaching, load)):
                self._acc[name].add(value)

    def add_cycle(self, when, cycle):
        """Records one finished press cycle (a dict with at least 'ok')."""
        with self._lock:
            if self.ended_at is not None:
                return
            if self.started_at is None:
                self.started_at = when
            if cycle.get("ok"):
                self.cycle_ok += 1
            else:
                self.cycle_ng += 1
            self._cycles.append(dict(cycle, time=when.isoformat()))

    def cycles(self, limit=None):
        """Finished cycles of the run, oldest first; the latest `limit` if given."""
        with self._lock:
            rows = list(self._cycles)
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return rows

    def snapshot(self):
        """Small dict describing the run and its statistics (no samples)."""
        with self._lock:
            if self.started_at is None:
                state = "idle"
            elif self.ended_at is None:
                state = "running"
            else:
                state = "finished"
            end = self.ended_at or datetime.now(timezone.utc)
            duration = (end - self.started_at).total_seconds() if self.started_at else None
            return {
                "state": state,
                "mode": self.mode,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "ended_at": self.ended_at.isoformat() if self.ended_at else None,
                "duration_s": round(duration, 1) if duration is not None else None,
                "count": self.count,
                "stored": len(self._samples),
                "truncated": self.count > len(self._samples),
                "stats": {name: self._acc[name].as_dict() for name in self.SIGNALS},
                "cycles": {"total": self.cycle_ok + self.cycle_ng, "ok": self.cycle_ok, "ng": self.cycle_ng},
            }

    def samples(self, limit=None):
        """Samples as [[iso_time, encoder, teaching, load], ...], oldest first; the latest `limit` if given."""
        with self._lock:
            rows = list(self._samples)
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return [[when.isoformat(), enc, teach, load] for when, enc, teach, load in rows]

    def csv_text(self):
        """All stored samples as CSV text (times in UTC)."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["time_utc", *self.SIGNALS])
        for row in self.samples():
            writer.writerow(row)
        return out.getvalue()
