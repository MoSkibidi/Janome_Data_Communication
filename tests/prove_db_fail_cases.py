"""
Live proof of the database fail cases. Run it and read the timeline:

    python tests/prove_db_fail_cases.py

It starts the real reader (the same code as the panel) against a fake press and a fake InfluxDB that this
script breaks on purpose, one way after the other: network down, disk full, token revoked, bucket deleted,
network that hangs. For each fault it prints the lines that appear in logs/events.log, then repairs the
fault and waits for the recovery line. At the end it compares what the reader kept in its local file with
what InfluxDB holds: nothing lost, nothing doubled.

Exit code 0 = every case was reported correctly and no data was lost.
English comments only
"""

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import event_log  # noqa: E402
import test_fail_cases as t  # noqa: E402


def say(*args):
    """The reader redirects sys.stdout into its own capture buffer, so the report goes to the real console."""
    sys.__stdout__.write(" ".join(str(a) for a in args) + "\n")
    sys.__stdout__.flush()


class Shim:
    """Stands in for the unittest.TestCase that LoopRunner expects."""

    def __init__(self):
        self.cleanups = []

    def addCleanup(self, func, *args):
        self.cleanups.append((func, args))

    def close(self):
        for func, args in reversed(self.cleanups):
            try:
                func(*args)
            except Exception:
                pass


def main():
    shim = Shim()
    influx = t.FakeInflux()
    cfg = {"db_type": "influxdb", "influx_url": influx.url, "influx_token": "secret", "influx_org": "org",
           "influx_bucket": "jp4", "retry_interval": 0.2}
    run = t.LoopRunner(shim, {"database": cfg}).start()
    log_path = os.path.join(run.tmp, "logs", "events.log")
    problems = []
    seen = [0]

    def new_lines():
        events = event_log.read_recent(log_path, 1000)
        fresh = events[seen[0]:]
        seen[0] = len(events)
        return fresh

    def codes():
        return [e["code"] for e in event_log.read_recent(log_path, 1000)]

    def show(title):
        say(f"\n--- {title}")
        for e in new_lines():
            say(f"  {e['time']} | {e['level']:<7} | {e['case']:<10} | {e['code']:<16} | {e['message'][:110]}")

    def stroke(n=1):
        for _ in range(n):
            stroke.counter += 1
            run.world.push_cycle(ok_total=stroke.counter, fault_total=0)
            time.sleep(0.1)
    stroke.counter = 100

    def wait(predicate, what, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        problems.append(f"TIMEOUT waiting for {what}")
        say(f"  !! timed out waiting for {what}")
        return False

    def fault(title, expect, setup, repair):
        before = codes().count(expect)
        restored_before = codes().count("DB_RESTORED")
        setup()
        stroke(2)
        wait(lambda: codes().count(expect) > before, f"{expect} ({title})")
        stroke(2)
        repair()
        wait(lambda: codes().count("DB_RESTORED") > restored_before, f"DB_RESTORED after {title}")
        show(title)

    try:
        t.wait_for(lambda: "[LINK] state=up" in run.text(), what="the reader to connect")
        time.sleep(0.3)
        stroke(3)
        t.wait_for(lambda: len(influx.lines()) >= 4, what="first writes")
        show("normal operation (start)")

        def set_down():
            influx.down = True

        def repair_down():
            influx.down = False
        fault("1. network / server down", "DB_UNREACHABLE", set_down, repair_down)

        def set_full():
            influx.down = True
            influx.full = True

        def repair_full():
            influx.down = False
            influx.full = False
        fault("2. database disk full", "DB_FULL", set_full, repair_full)

        def set_token():
            influx.token = "revoked"

        def repair_token():
            influx.token = "secret"
        fault("3. token revoked", "DB_AUTH", set_token, repair_token)

        def set_bucket():
            influx.bucket_missing = True

        def repair_bucket():
            influx.bucket_missing = False
        fault("4. bucket deleted", "DB_BUCKET", set_bucket, repair_bucket)

        def set_hang():
            influx.blackhole = True

        def repair_hang():
            influx.blackhole = False
        say("\n(the next case waits for the 5 s write timeout)")
        fault("5. network that hangs (cable pulled, no answer)", "DB_UNREACHABLE", set_hang, repair_hang)
        time.sleep(0.5)
    finally:
        run.finish()
        influx_lines = influx.lines()
        influx_stop = influx.stop
        shim.close()

    cycles = [ln for ln in influx_lines if ln.startswith("janome_jp4_telemetry_cycles")]
    samples = [ln for ln in influx_lines if not ln.startswith("janome_jp4_telemetry_cycles")]

    def identity(line):
        return line.split(" ", 1)[0], line.rsplit(" ", 1)[1]

    conn = sqlite3.connect(os.path.join(run.tmp, "janome_jp4_local.db"))
    local_samples = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    local_cycles = conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
    conn.close()
    held_samples = len({identity(ln) for ln in samples})
    held_cycles = len({identity(ln) for ln in cycles})

    say("\n=== data check")
    say(f"  readings  : read {local_samples}, InfluxDB holds {held_samples}")
    say(f"  cycles    : read {local_cycles}, InfluxDB holds {held_cycles}")
    if held_samples != local_samples:
        problems.append(f"readings lost or extra: {local_samples} read, {held_samples} in InfluxDB")
    if held_cycles != local_cycles:
        problems.append(f"cycles lost or extra: {local_cycles} read, {held_cycles} in InfluxDB")
    resent = (len(samples) - held_samples) + (len(cycles) - held_cycles)
    say(f"  re-sent after a lost answer (InfluxDB overwrites the same point): {resent}")

    final = [e["code"] for e in event_log.read_recent(log_path, 1000)]
    for code in ("DB_UNREACHABLE", "DB_FULL", "DB_AUTH", "DB_BUCKET", "DB_RESTORED"):
        say(f"  {code:<15}: {final.count(code)} line(s)")
        if final.count(code) < 1:
            problems.append(f"no {code} line in events.log")
    try:
        influx_stop()
    except Exception:
        pass
    say("\nRESULT:", "ALL CASES REPORTED, NO DATA LOST" if not problems else "PROBLEMS: " + "; ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
