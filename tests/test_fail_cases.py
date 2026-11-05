"""
Fault-injection tests for the fail cases the panel must survive.

A fake serial port plays the press (can be unplugged, powered off, pushes cycle results) and a
fake InfluxDB server (can be taken offline) plays the database. Run from the project folder:

    python -m unittest discover -s tests -v
"""
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serial  # noqa: E402
import read_janome_jp4 as reader  # noqa: E402
from database_handler import DatabaseHandler  # noqa: E402


# ---------------------------------------------------------------- fake press on a fake serial port

def frame(body):
    return b"$" + body.encode() + f"{sum(body.encode()) & 0xFF:02X}".encode() + b"\r"


def v1_data(result_code=0, ok_total=10, fault_total=1, end_pos=87.1575, end_load=677.8, tail=""):
    """88 hex characters in the Quality Data (V1) layout, plus an optional sensor data tail."""
    return (f"{1:04X}{ok_total:08X}{fault_total:08X}{ok_total + fault_total:08X}{result_code:04X}{0:04X}{0:04X}"
            f"{28098:08X}{1310:08X}{int(86.162 * 10000):08X}{int(417.1 * 10):08X}"
            f"{int(end_pos * 10000):08X}{int(end_load * 10):08X}{tail}")


class World:
    """State shared by every fake serial connection: is the cable in, is the press on."""

    def __init__(self):
        self.plugged = True
        self.powered = True
        self.q1 = False
        self.q1_count = 0
        self.extras = True  # answers K0 (I/O), S2 (settings) and B0 (system info); False = the press ignores them
        self.rx = bytearray()
        self.opens = 0
        self.lock = threading.Lock()

    def push_cycle(self, **kwargs):
        with self.lock:
            if self.powered and self.q1:
                self.rx.extend(frame("01V1" + v1_data(**kwargs)))


class FakeSerial:
    def __init__(self, world, **_):
        if not world.plugged:
            raise serial.SerialException("could not open port: file not found")
        self.world = world
        self.is_open = True
        world.opens += 1
        world.rx.clear()

    def _check(self):
        if not self.world.plugged:
            raise serial.SerialException("ClearCommError failed (PermissionError(13, 'Access is denied.'))")

    @property
    def in_waiting(self):
        self._check()
        return len(self.world.rx)

    def read(self, n):
        self._check()
        with self.world.lock:
            out = bytes(self.world.rx[:n])
            del self.world.rx[:n]
        return out

    def read_until(self, terminator=b"\r"):
        self._check()
        with self.world.lock:
            if terminator in self.world.rx:
                idx = self.world.rx.index(terminator) + 1
                out = bytes(self.world.rx[:idx])
                del self.world.rx[:idx]
                return out
        time.sleep(0.01)
        return b""  # nothing arrived: behaves like the read timeout

    def reset_input_buffer(self):
        self.world.rx.clear()

    def write(self, data):
        self._check()
        w = self.world
        if not w.powered:
            return
        cmd, sub = data[3:4].decode(), data[4:5].decode()
        with w.lock:
            if cmd == "N":
                value = {"1": 417000, "0": 420000, "2": 5100}[sub]
                w.rx.extend(frame(f"01n{sub}{value:08X}"))
            elif cmd == "Q":
                w.q1 = sub == "1"
                w.q1_count += 1 if w.q1 else 0
                w.rx.extend(frame(f"01q{sub}"))
            elif w.extras and cmd == "K":
                io_type = int(data[5:9], 16)
                w.rx.extend(frame(f"01k0{io_type:04X}{0x10 + io_type:02X}0000"))
            elif w.extras and cmd == "S":
                item = int(data[17:21], 16)
                value = {2006: 100000, 2030: 80000, 2005: 290000, 2017: 378140}[item]
                w.rx.extend(frame(f"01s2{0:04X}{value:08X}"))
            elif w.extras and cmd == "B":
                w.rx.extend(frame("01b0" + "01" + "00" + "0A" + "00" + "02" + "01" + "0000" + "00C8" + "0000" + "0000" + "03"))

    def close(self):
        self.is_open = False


class FastTime:
    """Stands in for the time module inside the reader so the 1 s port settle delay does not slow tests."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def sleep(seconds):
        time.sleep(min(seconds, 0.03))


# ---------------------------------------------------------------- fake InfluxDB

class FakeInflux:
    def __init__(self):
        self.down = False
        self.full = False         # while down: answer like a server whose disk is full instead of "unavailable"
        self.writes = []          # raw request bodies that were accepted
        self.write_headers = []
        self.retention_patches = []
        self.reject_marker = None  # a body containing this text gets HTTP 422
        self.token = "secret"      # the token the server accepts (change it to simulate a revoked token)
        self.bucket_missing = False  # the bucket was deleted: writes get HTTP 404
        self.blackhole = False     # the server accepts the connection and never answers (network cable pulled mid-way)
        self.bucket_days = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body=b"", ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if outer.down:
                    return self._send(503, b'{"message":"down"}')
                if self.path.startswith("/health"):
                    return self._send(200, b'{"status":"pass"}')
                if self.path.startswith("/api/v2/buckets"):
                    rules = [{"type": "expire", "everySeconds": outer.bucket_days * 86400}] if outer.bucket_days else []
                    return self._send(200, json.dumps({"buckets": [{"id": "b1", "name": "jp4", "retentionRules": rules}]}).encode())
                self._send(404)

            def do_PATCH(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.retention_patches.append(json.loads(self.rfile.read(length)))
                self._send(200, b"{}")

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if outer.down:
                    if outer.full:
                        return self._send(500, b'{"message":"write failed: no space left on device"}')
                    return self._send(503, b'{"message":"unavailable"}')
                if outer.blackhole:
                    end = time.monotonic() + 15
                    while outer.blackhole and time.monotonic() < end:
                        time.sleep(0.05)
                if self.headers.get("Authorization") != f"Token {outer.token}":
                    return self._send(401, b'{"message":"unauthorized"}')
                if outer.bucket_missing:
                    return self._send(404, b'{"code":"not found","message":"bucket \\"jp4\\" not found"}')
                if not self.path.startswith("/api/v2/write"):
                    return self._send(404)
                if outer.reject_marker and outer.reject_marker.encode() in body:
                    return self._send(422, b'{"message":"unprocessable"}')
                if body:
                    outer.writes.append(body.decode())
                    outer.write_headers.append(self.path)
                self._send(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def lines(self):
        return [ln for body in self.writes for ln in body.splitlines()]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(predicate, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class LoopRunner:
    """Runs the ingestion loop in a thread against the fake press and captures its output."""

    def __init__(self, test, config, **loop_kwargs):
        self.world = World()
        self.out = io.StringIO()
        self.stop = threading.Event()
        self.result = None
        self.tmp = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmp, "config.json")
        config.setdefault("serial", {"port": "COM9", "baudrate": 38400, "timeout": 0.05})
        config.setdefault("acquisition", {"interval_time": 0.05, "reconnect_interval": 0.1})
        with open(self.config_path, "w") as f:
            json.dump(config, f)
        self.loop_kwargs = loop_kwargs
        world = self.world
        test.patches = [
            (reader.serial, "Serial", lambda **kw: FakeSerial(world, **kw)),
            (reader, "time", FastTime()),
            (reader, "resolve_serial_port", lambda p: p),
        ]
        self.saved = [(obj, name, getattr(obj, name)) for obj, name, _ in test.patches]
        for obj, name, value in test.patches:
            setattr(obj, name, value)
        test.addCleanup(self.restore)
        test.addCleanup(self.finish)

    def restore(self):
        for obj, name, value in self.saved:
            setattr(obj, name, value)

    def start(self):
        handler = logging.StreamHandler(self.out)  # log lines ([NG], [DB ERROR], ...) are checked like the printed ones
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        self.log_handler = handler

        def run():
            with redirect_stdout(self.out):
                self.result = reader.run_ingestion_loop(config_path=self.config_path, stop_event=self.stop, **self.loop_kwargs)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def finish(self):
        self.stop.set()
        self.thread.join(10)
        logging.getLogger().removeHandler(self.log_handler)

    def text(self):
        return self.out.getvalue()

    def count(self, needle):
        return self.text().count(needle)


# ---------------------------------------------------------------- tests

class SerialFailCases(unittest.TestCase):
    def test_1_cable_pulled_then_back_reconnects_by_itself(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: run.count("[STATS]") >= 2, what="first readings")
        run.world.plugged = False
        wait_for(lambda: "[LINK] state=port_unavailable" in run.text(), what="port_unavailable")
        before = run.count("[STATS]")
        run.world.plugged = True
        wait_for(lambda: "[LINK] state=up" in run.text().split("port_unavailable")[-1], what="link back up")
        wait_for(lambda: run.count("[STATS]") > before + 1, what="readings after reconnect")
        self.assertEqual(run.world.q1_count, 2, "cycle streaming (Q1) must be requested again after reconnecting")
        self.assertTrue(run.thread.is_alive())

    def test_1b_port_missing_at_start_keeps_trying(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}})
        run.world.plugged = False
        run.start()
        wait_for(lambda: "[LINK] state=port_unavailable" in run.text(), what="port_unavailable at start")
        run.world.plugged = True
        wait_for(lambda: run.count("[STATS]") >= 2, what="readings once the port exists")
        self.assertIn("[LINK] state=up", run.text())

    def test_3_press_power_cycle_resumes_without_manual_start(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: run.count("[STATS]") >= 2, what="first readings")
        run.world.powered = False
        run.world.q1 = False  # a press that lost power forgets the streaming request
        wait_for(lambda: "[LINK] state=no_response" in run.text(), what="no_response")
        before = run.count("[STATS]")
        run.world.powered = True
        wait_for(lambda: run.count("[STATS]") > before + 1, what="readings after power-on")
        self.assertEqual(run.world.q1_count, 2)
        run.world.push_cycle()
        wait_for(lambda: "[CYCLE]" in run.text(), what="a cycle result after recovery")
        self.assertTrue(run.thread.is_alive())

    def test_2_ng_cycle_and_emergency_stop_are_logged_with_ok_flag(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=0)
        run.world.push_cycle(result_code=3232, fault_total=2)
        wait_for(lambda: run.count("[CYCLE]") >= 2, what="two cycles")
        cycles = [json.loads(l.split("[CYCLE] ", 1)[1]) for l in run.text().splitlines() if "[CYCLE]" in l]
        self.assertTrue(cycles[0]["ok"])
        self.assertFalse(cycles[1]["ok"])
        self.assertEqual(cycles[1]["result_code"], 3232)
        self.assertEqual(cycles[1]["result_text"], "External signal stopped")
        self.assertEqual(cycles[1]["end_position_mm"], 87.1575)
        self.assertEqual(cycles[1]["end_load_n"], 677.8)
        self.assertGreater(cycles[1]["time"], cycles[0]["time"])
        self.assertIn("[NG]", run.text())

    def test_cycle_pushed_during_a_poll_is_not_mistaken_for_the_reply(self):
        world = World()
        press = reader.JanomeJP4Press.__new__(reader.JanomeJP4Press)
        press.timeout, press._rx, press._cycles, press._q1_sent_at = 0.05, bytearray(), [], None
        press.ser = FakeSerial(world)
        world.q1 = True
        world.push_cycle(result_code=3211)
        data = press.read_telemetry()
        self.assertEqual(data["encoder_position_mm"], 41.7)
        cycles = press.poll_cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0][1]["result_code"], 3211)


class DatabaseFailCases(unittest.TestCase):
    def setUp(self):
        self.influx = FakeInflux()
        self.addCleanup(self.influx.stop)

    def influx_cfg(self, **extra):
        cfg = {"db_type": "influxdb", "influx_url": self.influx.url, "influx_token": "secret", "influx_org": "org",
               "influx_bucket": "jp4", "retry_interval": 0.2}
        cfg.update(extra)
        return cfg

    def test_6_network_down_data_kept_on_edge_then_synced_with_original_times(self):
        run = LoopRunner(self, {"database": self.influx_cfg()}).start()
        wait_for(lambda: len(self.influx.lines()) >= 2, what="first writes to InfluxDB")
        self.influx.down = True
        time.sleep(1.0)  # several samples are read while the network is down
        run.world.push_cycle(result_code=3225)  # an NG while the network is down
        wait_for(lambda: run.count("[CYCLE]") >= 1, what="cycle while offline")
        spooled_until = datetime.now(timezone.utc)
        time.sleep(0.5)
        written_before = len(self.influx.lines())
        self.influx.down = False
        wait_for(lambda: any('result=NG' in l for l in self.influx.lines()), what="NG cycle synced after the network came back")
        ts = [int(l.rsplit(" ", 1)[1]) for l in self.influx.lines() if not l.startswith("janome_jp4_telemetry_cycles")]
        self.assertEqual(ts, sorted(ts), "replayed samples must stay in time order")
        self.assertGreater(len(self.influx.lines()), written_before)
        # samples read while offline were stamped with the time they were read, not the time they were sent
        offline_ts = [t for t in ts if t < spooled_until.timestamp() * 1e9]
        self.assertGreater(len(offline_ts), 3)
        self.assertTrue(all(self.influx.write_headers[i].find("precision=ns") > 0 for i in range(len(self.influx.write_headers))))

    def test_4_restart_with_database_unreachable_spools_then_sends_after_restart(self):
        run = LoopRunner(self, {"database": self.influx_cfg()}, tolerate_db_down=True)
        self.influx.down = True
        run.start()
        wait_for(lambda: run.count("[STATS]") >= 5, what="readings while the DB is down")
        self.assertIn("Starting anyway", run.text())
        run.finish()
        spool = os.path.join(run.tmp, "janome_jp4_spool.db")
        left = sqlite3.connect(spool).execute("SELECT COUNT(*) FROM spool").fetchone()[0]
        self.assertGreaterEqual(left, 5, "samples must survive on disk when the program ends")
        # "reboot": a new process finds the spool and sends it
        self.influx.down = False
        run2 = LoopRunner(self, {"database": self.influx_cfg()})
        run2.config_path = run.config_path
        run2.start()
        wait_for(lambda: len(self.influx.lines()) >= left, what="spool sent after restart")

    def test_manual_start_with_database_unreachable_reports_error_and_refuses(self):
        self.influx.down = True
        run = LoopRunner(self, {"database": self.influx_cfg()}).start()
        run.thread.join(10)
        self.assertEqual(run.result, 2)
        self.assertIn("[DB ERROR]", run.text() + "")

    def test_wrong_token_is_reported_at_connection_time(self):
        with self.assertRaises(Exception) as ctx:
            DatabaseHandler(self.influx_cfg(influx_token="wrong"))
        self.assertIn("credentials", str(ctx.exception))

    def test_line_protocol_shape(self):
        handler = DatabaseHandler(self.influx_cfg())
        ts = datetime(2026, 11, 5, 1, 2, 3, 456789, tzinfo=timezone.utc)
        line = handler._influx_line({"encoder_position_mm": 1.5, "teaching_position_mm": 2.0, "load_n": 3.25}, ts)
        self.assertEqual(line, "janome_jp4_telemetry encoder_position_mm=1.5,teaching_position_mm=2.0,load_n=3.25 1793840523456789000")
        cyc = handler._influx_line({"_kind": "cycle", "ok": False, "program_number": 1, "result_code": 3225,
                                    "result_text": 'Time "exceeded"', "end_load_n": 650.0, "backlog": False}, ts)
        self.assertTrue(cyc.startswith("janome_jp4_telemetry_cycles,result=NG,program=1 "))
        self.assertIn('result_code=3225i', cyc)
        self.assertIn('result_text="Time \\"exceeded\\""', cyc)
        self.assertIn("ok=false", cyc)
        self.assertTrue(cyc.endswith(" 1793840523456789000"))

    def test_one_rejected_point_does_not_block_the_others(self):
        handler = DatabaseHandler(self.influx_cfg())
        ts = datetime.now(timezone.utc)
        good = {"encoder_position_mm": 1.0, "teaching_position_mm": 1.0, "load_n": 1.0}
        bad = {"_kind": "cycle", "ok": True, "result_text": "reject-me"}
        self.influx.reject_marker = "reject-me"
        handler.insert_many([(ts, good), (ts, bad), (ts, good)])
        self.assertEqual(len(self.influx.lines()), 2)

    def test_5_retention_is_applied_to_the_bucket(self):
        DatabaseHandler(self.influx_cfg(influx_retention_days=365))
        self.assertEqual(self.influx.retention_patches, [{"retentionRules": [{"type": "expire", "everySeconds": 365 * 86400}]}])
        self.influx.retention_patches.clear()
        self.influx.bucket_days = 365
        DatabaseHandler(self.influx_cfg(influx_retention_days=365))
        self.assertEqual(self.influx.retention_patches, [], "no change needed when the bucket already matches")

    def test_sql_database_gets_a_cycles_table_with_ng_rows(self):
        db = os.path.join(tempfile.mkdtemp(), "t.db")
        run = LoopRunner(self, {"database": {"db_type": "sqlite", "sqlite_path": db}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=3222)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.finish()
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT ok, result_code, result_text FROM janome_jp4_telemetry_cycles").fetchone()
        self.assertEqual(row, (0, 3222, "Position exceeded"))
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM janome_jp4_telemetry").fetchone()[0], 0)


class DatabaseProofCases(unittest.TestCase):
    """
    Proof for the database fail cases: every outage leaves the right line in events.log AND no reading is lost
    or sent twice (what the reader kept in its local file is exactly what InfluxDB holds afterwards).
    """

    def setUp(self):
        self.influx = FakeInflux()
        self.addCleanup(self.influx.stop)

    def cfg(self, **extra):
        cfg = {"db_type": "influxdb", "influx_url": self.influx.url, "influx_token": "secret", "influx_org": "org",
               "influx_bucket": "jp4", "retry_interval": 0.2}
        cfg.update(extra)
        return cfg

    def events(self, run):
        import event_log
        return event_log.read_recent(os.path.join(run.tmp, "logs", "events.log"), 1000)

    def codes(self, run):
        return [e["code"] for e in self.events(run)]

    def local_counts(self, run):
        conn = sqlite3.connect(os.path.join(run.tmp, "janome_jp4_local.db"))
        try:
            return (conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0])
        finally:
            conn.close()

    def influx_counts(self):
        lines = self.influx.lines()
        cycles = [ln for ln in lines if ln.startswith("janome_jp4_telemetry_cycles")]
        samples = [ln for ln in lines if not ln.startswith("janome_jp4_telemetry_cycles")]
        return samples, cycles

    @staticmethod
    def identity(line):
        """What InfluxDB uses to tell points apart: measurement + tags, and the timestamp."""
        return line.split(" ", 1)[0], line.rsplit(" ", 1)[1]

    def assert_nothing_lost_or_doubled(self, run, resend_ok=False):
        """
        Everything the reader kept in its local file is in InfluxDB exactly once. With resend_ok a point may
        arrive twice (the answer to a write was lost, so it was sent again): InfluxDB overwrites a point with the
        same measurement, tags and timestamp, so it still holds it once.
        """
        run.finish()
        samples, cycles = self.influx_counts()
        local_samples, local_cycles = self.local_counts(run)
        sample_ids = [self.identity(ln) for ln in samples]
        cycle_ids = [self.identity(ln) for ln in cycles]
        self.assertEqual(len(set(sample_ids)), local_samples, "every reading kept locally must be in InfluxDB")
        self.assertEqual(len(set(cycle_ids)), local_cycles, "every finished cycle kept locally must be in InfluxDB")
        if not resend_ok:
            self.assertEqual(len(sample_ids), len(set(sample_ids)), "no reading may be sent twice")
            self.assertEqual(len(cycle_ids), len(set(cycle_ids)), "no cycle may be sent twice")

    def test_outage_loses_nothing_and_sends_nothing_twice(self):
        run = LoopRunner(self, {"database": self.cfg()}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        counter = [10]

        def stroke():
            counter[0] += 1
            run.world.push_cycle(ok_total=counter[0], fault_total=0)

        for _ in range(3):
            stroke()
        wait_for(lambda: len(self.influx_counts()[1]) >= 3, what="3 cycles written before the outage")
        self.influx.down = True
        wait_for(lambda: "DB_UNREACHABLE" in self.codes(run), what="outage noticed")
        for _ in range(5):
            stroke()
            time.sleep(0.15)
        wait_for(lambda: run.count("[CYCLE]") >= 8)
        self.assertEqual(len(self.influx_counts()[1]), 3, "nothing reaches InfluxDB while it is down")
        self.influx.down = False
        wait_for(lambda: len(self.influx_counts()[1]) >= 8, what="the 5 offline cycles synced")
        wait_for(lambda: "DB_RESTORED" in self.codes(run), what="DB_RESTORED event")
        self.assert_nothing_lost_or_doubled(run)
        codes = self.codes(run)
        self.assertEqual(codes.count("DB_UNREACHABLE"), 1)
        self.assertEqual(codes.count("DB_RESTORED"), 1)
        self.assertLess(codes.index("DB_UNREACHABLE"), codes.index("DB_RESTORED"))
        self.assertNotIn("CYCLE_GAP", codes, "the cycles were read, only the database was away")

    def test_revoked_token_is_reported_as_a_setting_problem_not_a_network_problem(self):
        run = LoopRunner(self, {"database": self.cfg()}).start()
        wait_for(lambda: len(self.influx.lines()) >= 2)
        self.influx.token = "somebody-changed-it"
        wait_for(lambda: "DB_AUTH" in self.codes(run), what="DB_AUTH event")
        (event,) = [e for e in self.events(run) if e["code"] == "DB_AUTH"]
        self.assertEqual((event["case"], event["level"]), ("DB ERROR", "ERROR"))
        self.assertIn("token", event["message"])
        self.assertNotIn("DB_UNREACHABLE", self.codes(run))
        self.influx.token = "secret"
        wait_for(lambda: "DB_RESTORED" in self.codes(run), what="DB_RESTORED after the token works again")
        self.assert_nothing_lost_or_doubled(run)

    def test_deleted_bucket_is_reported_as_missing_bucket(self):
        run = LoopRunner(self, {"database": self.cfg()}).start()
        wait_for(lambda: len(self.influx.lines()) >= 2)
        self.influx.bucket_missing = True
        wait_for(lambda: "DB_BUCKET" in self.codes(run), what="DB_BUCKET event")
        self.assertNotIn("DB_UNREACHABLE", self.codes(run))
        self.influx.bucket_missing = False
        wait_for(lambda: "DB_RESTORED" in self.codes(run))
        self.assert_nothing_lost_or_doubled(run)

    def test_network_that_hangs_never_stops_the_readings(self):
        """A pulled cable usually does not refuse the connection, it just never answers."""
        run = LoopRunner(self, {"database": self.cfg()}).start()
        wait_for(lambda: len(self.influx.lines()) >= 2)
        self.influx.blackhole = True
        before = run.count("[STATS]")
        time.sleep(1.5)   # the writer is stuck waiting for the server for up to 5 s
        self.assertGreaterEqual(run.count("[STATS]") - before, 10, "readings continue while the database hangs")
        wait_for(lambda: "DB_UNREACHABLE" in self.codes(run), timeout=15, what="timeout turned into DB_UNREACHABLE")
        self.influx.blackhole = False
        wait_for(lambda: "DB_RESTORED" in self.codes(run), timeout=15)
        self.assert_nothing_lost_or_doubled(run, resend_ok=True)

    def test_buffer_limit_reached_is_reported_as_data_dropped_and_local_file_keeps_everything(self):
        self.influx.down = True
        run = LoopRunner(self, {"database": self.cfg(max_spool_rows=5, max_buffer=5)}, tolerate_db_down=True).start()
        wait_for(lambda: "DATA_DROPPED" in self.codes(run), what="DATA_DROPPED event")
        (event,) = [e for e in self.events(run) if e["code"] == "DATA_DROPPED"]
        self.assertEqual(event["case"], "DB ERROR")
        self.assertIn("remain in the local file", event["message"])
        run.finish()
        local_samples, _ = self.local_counts(run)
        spool_left = sqlite3.connect(os.path.join(run.tmp, "janome_jp4_spool.db")).execute("SELECT COUNT(*) FROM spool").fetchone()[0]
        self.assertLessEqual(spool_left, 5, "the edge spool is bounded")
        self.assertGreater(local_samples, spool_left, "the local file still has what the spool had to drop")

    def test_a_record_the_server_refuses_is_reported_and_the_rest_are_written(self):
        import event_log
        path = os.path.join(tempfile.mkdtemp(), "logs", "events.log")
        event_log.configure(path)
        self.addCleanup(event_log.configure, None)
        handler = DatabaseHandler(self.cfg())
        ts = datetime.now(timezone.utc)
        good = {"encoder_position_mm": 1.0, "teaching_position_mm": 1.0, "load_n": 1.0}
        self.influx.reject_marker = "reject-me"
        handler.insert_many([(ts, good), (ts, {"_kind": "cycle", "ok": True, "result_text": "reject-me"}), (ts, good)])
        self.assertEqual(len(self.influx.lines()), 2)
        (event,) = [e for e in event_log.read_recent(path) if e["code"] == "DB_REJECTED"]
        self.assertEqual((event["case"], event["level"]), ("DB ERROR", "ERROR"))
        self.assertIn("HTTP 422", event["message"])

    def test_restart_while_the_database_is_down_keeps_the_data_and_logs_it(self):
        self.influx.down = True
        run = LoopRunner(self, {"database": self.cfg()}, tolerate_db_down=True).start()
        wait_for(lambda: "DB_UNREACHABLE" in self.codes(run))
        wait_for(lambda: run.count("[STATS]") >= 5)
        run.finish()
        left = [e for e in self.events(run) if e["code"] == "DB_UNREACHABLE" and "kept in" in e["message"]]
        self.assertTrue(left, "the log says how many readings wait on disk for the next start")


class AutostartCases(unittest.TestCase):
    """'Start with Windows' from the panel: Logon needs no administrator, Startup opens the permission window."""

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def setUp(self):
        import autostart
        self.autostart = autostart
        self.base = tempfile.mkdtemp()
        for name in ("install_autostart.ps1", "uninstall_autostart.ps1"):
            with open(os.path.join(self.base, name), "w") as f:
                f.write("# test")
        self.calls = []
        saved = (autostart.supported, autostart.is_admin)
        autostart.supported = lambda: True
        autostart.is_admin = lambda: False
        self.addCleanup(lambda: (setattr(autostart, "supported", saved[0]), setattr(autostart, "is_admin", saved[1])))

    def runner(self, *results):
        queue = list(results)

        def run(cmd, **kwargs):
            self.calls.append(cmd)
            return queue.pop(0) if queue else self.Result()
        return run

    def test_status_tells_boot_and_login_tasks_apart(self):
        a = self.autostart
        self.assertEqual(a.status(run=self.runner(self.Result(stdout="SYSTEM|Ready\n")))["mode"], "Startup")
        self.assertEqual(a.status(run=self.runner(self.Result(stdout="PC\\Operator|Ready\n")))["mode"], "Logon")
        off = a.status(run=self.runner(self.Result(stdout="")))
        self.assertFalse(off["installed"])
        self.assertIsNone(off["mode"])

    def test_login_start_is_installed_directly_without_the_permission_window(self):
        result = self.autostart.enable("Logon", self.base, run=self.runner())
        self.assertTrue(result["ok"])
        self.assertFalse(result["pending"])
        self.assertIn("-Mode", self.calls[0])
        self.assertEqual(self.calls[0][self.calls[0].index("-Mode") + 1], "Logon")
        self.assertNotIn("RunAs", " ".join(self.calls[0]))

    def test_boot_start_without_administrator_opens_the_permission_window(self):
        result = self.autostart.enable("Startup", self.base, run=self.runner())
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"], "the person approves on the PC; the panel waits for the result")
        command = " ".join(self.calls[0])
        self.assertIn("-Verb RunAs", command)
        self.assertIn("install_autostart.ps1", command)
        self.assertIn("'Startup'", command)

    def test_boot_start_as_administrator_is_installed_directly(self):
        self.autostart.is_admin = lambda: True
        result = self.autostart.enable("Startup", self.base, run=self.runner())
        self.assertTrue(result["ok"] and not result["pending"])
        self.assertNotIn("RunAs", " ".join(self.calls[0]))

    def test_a_bad_mode_or_missing_script_is_refused_without_running_anything(self):
        self.assertFalse(self.autostart.enable("Whenever", self.base, run=self.runner())["ok"])
        self.assertFalse(self.autostart.enable("Logon", tempfile.mkdtemp(), run=self.runner())["ok"])
        self.assertEqual(self.calls, [])

    def test_a_failed_install_reports_the_reason(self):
        result = self.autostart.enable("Logon", self.base, run=self.runner(self.Result(1, "", "Access is denied")))
        self.assertFalse(result["ok"])
        self.assertIn("Access is denied", result["message"])

    def test_turning_off_keeps_the_running_panel_and_asks_permission_for_a_boot_task(self):
        ok = self.autostart.disable(self.base, run=self.runner())
        self.assertTrue(ok["ok"] and not ok["pending"])
        self.assertIn("-KeepRunning", self.calls[0])
        self.calls.clear()
        denied = self.autostart.disable(self.base, run=self.runner(self.Result(1, "", "Access is denied"), self.Result()))
        self.assertTrue(denied["ok"] and denied["pending"])
        self.assertIn("RunAs", " ".join(self.calls[1]))

    def test_scripts_are_found_next_to_the_exe_or_in_deploy(self):
        a = self.autostart
        self.assertEqual(a.find_script(self.base, "install_autostart.ps1"), os.path.join(self.base, "install_autostart.ps1"))
        source = tempfile.mkdtemp()
        os.makedirs(os.path.join(source, "deploy"))
        with open(os.path.join(source, "deploy", "install_autostart.ps1"), "w") as f:
            f.write("#")
        self.assertEqual(a.find_script(source, "install_autostart.ps1"), os.path.join(source, "deploy", "install_autostart.ps1"))
        self.assertIsNone(a.find_script(tempfile.mkdtemp(), "install_autostart.ps1"))


class PackagedProgramCases(unittest.TestCase):
    """The packaged exe is one file that runs as the panel, the reader or the COM test page."""

    def test_entry_point_picks_the_mode_from_the_arguments(self):
        import types
        import janome_main
        seen = []
        fake_reader = types.SimpleNamespace(main=lambda argv: seen.append(("reader", argv)))
        fake_app = types.SimpleNamespace(main=lambda: seen.append(("panel",)))
        saved_argv, saved_modules = sys.argv, {k: sys.modules.get(k) for k in ("read_janome_jp4", "app")}
        self.addCleanup(lambda: [sys.modules.__setitem__(k, v) if v is not None else sys.modules.pop(k, None) for k, v in saved_modules.items()])
        self.addCleanup(setattr, sys, "argv", saved_argv)
        sys.modules["read_janome_jp4"], sys.modules["app"] = fake_reader, fake_app
        sys.argv = ["JanomeJP4.exe", "--reader", "--config", "c.json", "--mock"]
        janome_main.run()
        sys.argv = ["JanomeJP4.exe"]
        janome_main.run()
        self.assertEqual(seen, [("reader", ["--config", "c.json", "--mock"]), ("panel",)])


class LocalFileCases(unittest.TestCase):
    """The local SQLite file keeps everything that was read, with or without a database server."""

    def local_db(self, run):
        return os.path.join(run.tmp, "janome_jp4_local.db")

    def test_no_database_configured_still_writes_readings_and_cycles_to_the_local_file(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=3232)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        wait_for(lambda: run.count("[STATS]") >= 4)
        run.finish()
        conn = sqlite3.connect(self.local_db(run))
        self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 4)
        payload = json.loads(conn.execute("SELECT payload FROM cycles").fetchone()[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["result_code"], 3232)
        started, ended = conn.execute("SELECT started_at, ended_at FROM runs").fetchone()
        self.assertIsNotNone(ended, "a run the reader started itself is closed when it stops")

    def test_database_server_down_data_is_still_in_the_local_file(self):
        influx = FakeInflux()
        self.addCleanup(influx.stop)
        influx.down = True
        cfg = {"db_type": "influxdb", "influx_url": influx.url, "influx_token": "secret",
               "influx_org": "o", "influx_bucket": "b", "retry_interval": 0.2}
        run = LoopRunner(self, {"database": cfg}, tolerate_db_down=True).start()
        wait_for(lambda: run.count("[STATS]") >= 5)
        run.finish()
        conn = sqlite3.connect(self.local_db(run))
        self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0], 5)
        self.assertEqual(influx.lines(), [])

    def test_the_panel_run_id_is_used_and_the_run_stays_open(self):
        from local_store import LocalStore
        run = LoopRunner(self, {"database": {"db_type": "none"}}, run_id=1)
        store = LocalStore(self.local_db(run))
        self.assertEqual(store.start_run("real"), 1)
        run.start()
        wait_for(lambda: run.count("[STATS]") >= 3)
        run.finish()
        self.assertGreaterEqual(store.count_samples(1), 3)
        self.assertIsNone(store.get_run(1)["ended_at"], "the panel, not the reader, ends a run it created")

    def test_panel_rebuilds_the_last_run_after_a_restart(self):
        from local_store import LocalStore
        from run_summary import RunSummary, restore_from_store
        store = LocalStore(os.path.join(tempfile.mkdtemp(), "l.db"))
        run_id = store.start_run("real")
        base = datetime(2026, 11, 5, 1, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            store.add_sample(run_id, base.replace(second=i), {"encoder_position_mm": float(i), "teaching_position_mm": 0.0, "load_n": 100.0})
        store.add_cycle(run_id, base, {"ok": False, "result_code": 3225})
        store.end_run(run_id, base.replace(second=30))
        summary = RunSummary()
        restore_from_store(summary, store, run_id)
        snap = summary.snapshot()
        self.assertEqual((snap["state"], snap["count"]), ("finished", 10))
        self.assertEqual(snap["stats"]["encoder_position_mm"]["max"], 9.0)
        self.assertEqual(snap["cycles"], {"total": 1, "ok": 0, "ng": 1})
        self.assertEqual(len(store.csv_text(run_id).splitlines()), 11)

    def test_the_file_is_bounded_oldest_readings_go_first(self):
        from local_store import LocalStore
        store = LocalStore(os.path.join(tempfile.mkdtemp(), "l.db"), max_rows=1000)
        run_id = store.start_run("real")
        now = datetime.now(timezone.utc)
        for i in range(1300):
            store.add_sample(run_id, now, {"encoder_position_mm": float(i), "teaching_position_mm": 0.0, "load_n": 0.0})
        store.prune()
        self.assertEqual(store.count_samples(), 1000)
        first = next(store.run_samples(run_id))
        self.assertEqual(first[1], 300.0)


class PastedFolderCases(unittest.TestCase):
    """A folder pasted into the panel (Windows 'Copy as path' adds quotes) is used as it is."""

    def test_full_path_with_or_without_quotes_is_used_as_typed(self):
        import csv_export
        base = os.path.join(tempfile.mkdtemp(), "config.json")
        for typed in (r"C:\Intel", r'"C:\Intel"', r"  C:\Intel  ", r"'C:\Intel'", r'"C:\Intel\"'):
            folder = csv_export.resolve_folder({"folder": typed}, base)
            self.assertEqual(folder.rstrip("\\/").lower().replace("/", "\\"), r"c:\intel", typed)
        self.assertEqual(csv_export.resolve_folder({"folder": "exports"}, base), os.path.join(os.path.dirname(base), "exports"))
        self.assertEqual(csv_export.resolve_folder({"folder": ""}, base), os.path.join(os.path.dirname(base), csv_export.DEFAULT_FOLDER))

    def test_a_folder_that_does_not_exist_yet_is_created(self):
        import csv_export
        target = os.path.join(tempfile.mkdtemp(), "new", "deep")
        csv_export.CsvExporter(target, "day")
        self.assertTrue(os.path.isdir(target))


class CsvFileCases(unittest.TestCase):
    """Every finished cycle is also appended to a CSV file: one file per run, or one per day."""

    CYCLE = {"program_number": 5, "ok": True, "result_code": 0, "result_text": "Normal", "prep_time_s": 1.5,
             "work_time_s": 0.3, "contact_position_mm": 29.0, "contact_load_n": 0.0, "end_position_mm": 37.8,
             "end_load_n": 2500.0, "ok_total": 10, "fault_total": 2, "lifetime_total": 12,
             "sensor_fault_section": 0, "sensor_fault_number": 0, "backlog": False}

    def rows(self, path):
        import csv
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_run_mode_one_file_header_once_and_restart_appends_to_the_same_file(self):
        import csv_export
        folder = os.path.join(tempfile.mkdtemp(), "exports")
        started = datetime(2026, 11, 5, 3, 6, 35, tzinfo=timezone.utc)
        when = datetime(2026, 11, 5, 3, 9, 33, tzinfo=timezone.utc)
        first = csv_export.CsvExporter(folder, "run", run_started=started, run_id=7)
        path = first.add_cycle(when, self.CYCLE)
        # the reader was restarted inside the same run: a new exporter, same run start
        second = csv_export.CsvExporter(folder, "run", run_started=started, run_id=7)
        ng = dict(self.CYCLE, ok=False, result_code=3232, result_text="External signal stopped", backlog=True)
        self.assertEqual(second.add_cycle(when.replace(minute=10), ng), path)
        rows = self.rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["ok"] for r in rows], ["1", "0"])
        self.assertEqual(rows[1]["result_code"], "3232")
        self.assertEqual(rows[1]["backlog"], "1")
        self.assertEqual(rows[0]["run_id"], "7")
        self.assertTrue(rows[0]["time_utc"].startswith("2026-11-05T03:09:33"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().count("time_local"), 1, "the header is written once")
        self.assertEqual(os.listdir(folder), [os.path.basename(path)])
        self.assertTrue(os.path.basename(path).startswith("cycles_run_"))

    def test_a_new_run_gets_its_own_file(self):
        import csv_export
        folder = tempfile.mkdtemp()
        when = datetime(2026, 11, 5, 3, 9, 33, tzinfo=timezone.utc)
        a = csv_export.CsvExporter(folder, "run", run_started=when, run_id=1).add_cycle(when, self.CYCLE)
        b = csv_export.CsvExporter(folder, "run", run_started=when.replace(minute=30), run_id=2).add_cycle(when, self.CYCLE)
        self.assertNotEqual(a, b)

    def test_day_mode_starts_a_new_file_at_midnight_and_ignores_the_run(self):
        import csv_export
        folder = tempfile.mkdtemp()
        exporter = csv_export.CsvExporter(folder, "day", run_id=1)
        day1 = datetime(2026, 11, 5, 12, 0, 0).astimezone()
        day2 = datetime(2026, 11, 6, 0, 0, 1).astimezone()
        p1 = exporter.add_cycle(day1, self.CYCLE)
        p2 = exporter.add_cycle(day2, self.CYCLE)
        again = csv_export.CsvExporter(folder, "day", run_id=2).add_cycle(day1.replace(hour=13), self.CYCLE)
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1, again, "another run on the same day appends to that day's file")
        self.assertEqual(len(self.rows(p1)), 2)
        self.assertIn("2026-11-05", p1)
        self.assertIn("2026-11-06", p2)

    def test_day_start_moves_the_change_of_day_and_shows_in_the_file_name(self):
        import csv_export
        folder = tempfile.mkdtemp()
        exporter = csv_export.CsvExporter(folder, "day", run_id=1, day_start="06:30")
        local = lambda d, h, m: datetime(2026, 11, d, h, m, 0).astimezone()
        evening = exporter.path_for(local(5, 23, 0))
        after_midnight = exporter.path_for(local(6, 2, 0))
        just_before = exporter.path_for(local(6, 6, 29))
        at_start = exporter.path_for(local(6, 6, 30))
        self.assertTrue(evening.endswith("cycles_day_2026-11-05_0630.csv"), evening)
        self.assertEqual(evening, after_midnight, "02:00 still belongs to the day that started at 06:30 yesterday")
        self.assertEqual(evening, just_before)
        self.assertTrue(at_start.endswith("cycles_day_2026-11-06_0630.csv"), at_start)
        self.assertEqual(len(self.rows(exporter.add_cycle(local(6, 2, 0), self.CYCLE))), 1)

    def test_seven_am_starts_a_new_day(self):
        import csv_export
        exporter = csv_export.CsvExporter(tempfile.mkdtemp(), "day", day_start="07:00")
        at = lambda d, h, m: exporter.path_for(datetime(2026, 11, d, h, m, 0).astimezone())
        self.assertTrue(at(5, 7, 0).endswith("cycles_day_2026-11-05_0700.csv"))
        self.assertEqual(at(5, 7, 0), at(6, 6, 59), "06:59 next morning is still the day that began at 07:00")
        self.assertTrue(at(6, 7, 0).endswith("cycles_day_2026-11-06_0700.csv"))
        self.assertEqual(at(6, 0, 0), at(5, 23, 59), "midnight does not start a new day")

    def test_midnight_day_start_keeps_the_plain_calendar_name(self):
        import csv_export
        folder = tempfile.mkdtemp()
        for kwargs in ({}, {"day_start": "00:00"}, {"day_start": "0:00"}):
            path = csv_export.CsvExporter(folder, "day", **kwargs).path_for(datetime(2026, 11, 5, 12, 0).astimezone())
            self.assertTrue(path.endswith("cycles_day_2026-11-05.csv"), path)

    def test_day_start_is_checked(self):
        import csv_export
        self.assertEqual(csv_export.parse_day_start("06:30"), (6, 30))
        self.assertEqual(csv_export.parse_day_start("6:05:59"), (6, 5))
        self.assertEqual(csv_export.parse_day_start(None), (0, 0))
        for bad in ("24:00", "12:60", "noon", "6", "-1:00", ""):
            with self.assertRaises(ValueError, msg=bad):
                csv_export.parse_day_start(bad)
            with self.assertRaises(ValueError, msg=bad):
                csv_export.CsvExporter(tempfile.mkdtemp(), "day", day_start=bad)

    def test_the_reader_uses_day_start_and_falls_back_on_a_bad_value(self):
        for value in ("06:30", "nonsense"):
            run = LoopRunner(self, {"database": {"db_type": "none"}, "csv_export": {"mode": "day", "day_start": value}}).start()
            wait_for(lambda: "[LINK] state=up" in run.text())
            time.sleep(0.2)
            run.world.push_cycle(result_code=0)
            wait_for(lambda: run.count("[CYCLE]") >= 1)
            run.finish()
            (name,) = os.listdir(os.path.join(run.tmp, "exports"))
            if value == "nonsense":
                self.assertIn("Bad csv_export day_start", run.text())
                self.assertRegex(name, r"^cycles_day_\d{4}-\d\d-\d\d\.csv$")
            else:
                self.assertRegex(name, r"^cycles_day_\d{4}-\d\d-\d\d_0630\.csv$")

    def test_unknown_mode_is_rejected(self):
        import csv_export
        with self.assertRaises(ValueError):
            csv_export.CsvExporter(tempfile.mkdtemp(), "week")

    def test_the_reader_writes_ok_and_ng_cycles_to_exports(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=3232)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.world.push_cycle(result_code=0)
        wait_for(lambda: run.count("[CYCLE]") >= 2)
        run.finish()
        folder = os.path.join(run.tmp, "exports")
        files = [n for n in os.listdir(folder) if n.startswith("cycles_")]
        self.assertEqual(len(files), 1)
        self.assertEqual(len(os.listdir(folder)), 1, "only the cycle file is written")
        rows = self.rows(os.path.join(folder, files[0]))
        self.assertEqual([r["result_code"] for r in rows], ["3232", "0"])
        self.assertEqual([r["ok"] for r in rows], ["0", "1"])

    def test_csv_can_be_switched_off(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}, "csv_export": {"enabled": False}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=0)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.finish()
        self.assertFalse(os.path.exists(os.path.join(run.tmp, "exports")))

    def test_unusable_folder_never_stops_the_collection(self):
        blocker = os.path.join(tempfile.mkdtemp(), "not_a_folder")
        with open(blocker, "w") as f:
            f.write("x")
        run = LoopRunner(self, {"database": {"db_type": "none"}, "csv_export": {"folder": blocker}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=0)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        wait_for(lambda: run.count("[STATS]") >= 4)
        run.finish()
        self.assertIn("[CSV] Cannot use the folder", run.text())
        conn = sqlite3.connect(os.path.join(run.tmp, "janome_jp4_local.db"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0], 1, "the local file still got the cycle")


class CycleReportCases(unittest.TestCase):
    """The Basic file follows the cycle report: speeds, positions, stroke, end load in kg and its difference to the setting."""

    CYCLE = CsvFileCases.CYCLE
    SETTINGS = {"set_pressing_speed_mm_s": 100.0, "set_return_speed_mm_s": 80.0,
                "set_start_position_mm": 29.0, "set_end_position_mm": 37.814}
    TARGETS = {"end_load_setting_kgf": 250}

    def row(self, cycle, settings, targets):
        import csv
        import csv_export
        folder = tempfile.mkdtemp()
        when = datetime(2026, 11, 5, 3, 9, 33, tzinfo=timezone.utc)
        path = csv_export.CsvExporter(folder, "run", run_started=when).add_cycle(when, cycle, settings, targets)
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))[0]

    def test_report_values_from_settings_and_the_cycle(self):
        cycle = dict(self.CYCLE, prep_time_s=0.24, work_time_s=0.318, end_position_mm=37.812, end_load_n=2531.6)
        row = self.row(cycle, self.SETTINGS, self.TARGETS)
        self.assertEqual((row["pressing_speed_mm_s"], row["return_speed_mm_s"]), ("100.0", "80.0"))
        self.assertEqual((row["start_position_mm"], row["end_position_set_mm"], row["stroke_mm"]), ("29.0", "37.814", "8.814"))
        self.assertEqual((row["end_position_mm"], row["end_load_n"], row["end_load_kgf"]), ("37.812", "2531.6", "258.2"))
        self.assertEqual((row["end_load_setting_kgf"], row["end_load_diff_kgf"]), ("250", "8.2"))
        self.assertEqual((row["prep_time_s"], row["work_time_s"]), ("0.24", "0.318"))
        self.assertNotIn("cycle_time_s", row)
        self.assertNotIn("uph", row)
        self.assertNotIn("uph_ok", row)

    def test_end_load_below_the_setting_gives_a_negative_difference(self):
        row = self.row(dict(self.CYCLE, end_load_n=2354.0), self.SETTINGS, self.TARGETS)  # 240.0 kg
        self.assertEqual((row["end_load_kgf"], row["end_load_diff_kgf"]), ("240.0", "-10.0"))

    def test_values_that_cannot_be_worked_out_stay_empty(self):
        row = self.row(self.CYCLE, None, {})
        self.assertEqual([row[c] for c in ("pressing_speed_mm_s", "return_speed_mm_s", "stroke_mm",
                                           "end_load_setting_kgf", "end_load_diff_kgf")], [""] * 5)
        self.assertEqual(row["result_code"], "0")

    def test_file_with_other_columns_is_left_alone_and_a_numbered_file_is_used(self):
        import csv_export
        folder = tempfile.mkdtemp()
        when = datetime(2026, 11, 5, 3, 9, 33, tzinfo=timezone.utc)
        exporter = csv_export.CsvExporter(folder, "day")
        old = exporter.path_for(when)
        with open(old, "w", newline="", encoding="utf-8") as f:
            f.write("time_local,time_utc,run_id,program_number\r\nx,y,1,5\r\n")
        used = csv_export.CsvExporter(folder, "day").add_cycle(when, self.CYCLE, self.SETTINGS, self.TARGETS)
        self.assertEqual(used, old.replace(".csv", "_2.csv"))
        with open(old, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2, "the older file is untouched")

    def test_the_reader_fills_the_report_from_the_press_settings(self):
        cfg = {"database": {"db_type": "none"}, "csv_export": {}}
        run = LoopRunner(self, cfg).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=0, end_pos=37.812, end_load=2531.6)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.finish()
        import csv
        folder = os.path.join(run.tmp, "exports")
        (name,) = os.listdir(folder)
        with open(os.path.join(folder, name), newline="", encoding="utf-8") as f:
            (row,) = list(csv.DictReader(f))
        self.assertEqual((row["pressing_speed_mm_s"], row["return_speed_mm_s"], row["stroke_mm"]), ("100.0", "80.0", "8.814"))
        self.assertEqual((row["end_load_kgf"], row["end_load_setting_kgf"]), ("258.2", "250"))
        self.assertEqual((row["prep_time_s"], row["work_time_s"]), ("28.098", "1.31"))
        self.assertEqual(row["end_load_diff_kgf"], "8.2")


class FolderBrowserCases(unittest.TestCase):
    """The folder list behind the Browse button of the CSV folder setting."""

    def test_lists_sub_folders_sorted_without_files_or_hidden_ones(self):
        import folder_browser
        base = tempfile.mkdtemp()
        for name in ("beta", "Alpha", ".hidden", "$Recycle.Bin"):
            os.mkdir(os.path.join(base, name))
        with open(os.path.join(base, "file.txt"), "w") as f:
            f.write("x")
        result = folder_browser.browse(base, base)
        self.assertEqual([f["name"] for f in result["folders"]], ["Alpha", "beta"])
        self.assertEqual(result["path"], os.path.normpath(base))
        self.assertEqual(result["parent"], os.path.dirname(os.path.normpath(base)))
        self.assertTrue(result["roots"])

    def test_relative_path_is_taken_from_the_base_and_a_missing_folder_shows_the_nearest_one(self):
        import folder_browser
        base = tempfile.mkdtemp()
        os.mkdir(os.path.join(base, "exports"))
        self.assertEqual(folder_browser.browse("exports", base)["path"], os.path.join(base, "exports"))
        self.assertEqual(folder_browser.browse("exports/not_yet/deeper", base)["path"], os.path.join(base, "exports"))
        result = folder_browser.browse("", base)  # nothing chosen yet: browsing starts at the home folder
        self.assertEqual(result["path"], folder_browser.home())
        self.assertEqual(result["home"], folder_browser.home())
        self.assertTrue(os.path.isdir(result["path"]))

    def test_new_folder_is_created_and_bad_names_are_refused(self):
        import folder_browser
        base = tempfile.mkdtemp()
        path = folder_browser.make_folder(base, "Janome data", base)
        self.assertTrue(os.path.isdir(path))
        for bad in ("", "  ", "..", "a/b", "a\\b", "a:b", "what?", "dot."):
            with self.assertRaises(folder_browser.BrowseError, msg=bad):
                folder_browser.make_folder(base, bad, base)
        with self.assertRaises(folder_browser.BrowseError):
            folder_browser.make_folder(base, "Janome data", base)  # already there
        with self.assertRaises(folder_browser.BrowseError):
            folder_browser.make_folder(os.path.join(base, "missing"), "x", base)


class EventLogCases(unittest.TestCase):
    """Every fail case leaves a clear line in logs/events.log (the error log), with a recovery line when it ends."""

    def events(self, run, level=None):
        import event_log
        return event_log.read_recent(os.path.join(run.tmp, "logs", "events.log"), 1000, level)

    def codes(self, run):
        return [e["code"] for e in self.events(run)]

    def test_1_cable_pulled_is_logged_as_lost_then_recovered(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: run.count("[STATS]") >= 2)
        run.world.plugged = False
        wait_for(lambda: "RS232_LOST" in self.codes(run), what="RS232_LOST event")
        run.world.plugged = True
        wait_for(lambda: "RS232_RECOVERED" in self.codes(run), what="RS232_RECOVERED event")
        events = {e["code"]: e for e in self.events(run)}
        self.assertEqual(events["RS232_LOST"]["level"], "ERROR")
        self.assertIn("COM9", events["RS232_LOST"]["message"])
        self.assertIn("Auto-reconnect", events["RS232_LOST"]["message"])
        self.assertIn("reading continues automatically", events["RS232_RECOVERED"]["message"])
        self.assertIn("down_for=", events["RS232_RECOVERED"]["details"])
        self.assertEqual(self.codes(run).count("RS232_LOST"), 1, "one line per outage, not one per retry")

    def test_3_press_off_and_on_is_logged_as_no_response_then_recovered(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: run.count("[STATS]") >= 2)
        run.world.powered = False
        run.world.q1 = False
        wait_for(lambda: "PRESS_NO_RESPONSE" in self.codes(run), what="PRESS_NO_RESPONSE event")
        run.world.powered = True
        wait_for(lambda: "PRESS_RECOVERED" in self.codes(run), what="PRESS_RECOVERED event")
        codes = self.codes(run)
        self.assertLess(codes.index("PRESS_NO_RESPONSE"), codes.index("PRESS_RECOVERED"))
        self.assertEqual(codes.count("PRESS_NO_RESPONSE"), 1)

    def test_2_ng_and_emergency_stop_are_logged_with_their_data(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=0)
        run.world.push_cycle(result_code=3232, fault_total=2, ok_total=10)
        run.world.push_cycle(result_code=3225, fault_total=3, ok_total=10)
        wait_for(lambda: run.count("[CYCLE]") >= 3)
        run.finish()
        ng = [e for e in self.events(run) if e["code"] == "PRESS_NG"]
        self.assertEqual(len(ng), 2, "the OK cycle is not an event")
        self.assertEqual(ng[0]["level"], "WARNING")
        self.assertIn("3232", ng[0]["message"])
        self.assertIn("emergency stop", ng[0]["message"])
        self.assertIn("result_code=3232", ng[0]["details"])
        self.assertNotIn("emergency stop", ng[1]["message"], "a time-exceeded NG is not a stop")

    def test_7_missing_cycles_are_reported_as_a_gap(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(ok_total=10, fault_total=1)   # counter 11
        run.world.push_cycle(ok_total=11, fault_total=1)   # counter 12: continuous
        run.world.push_cycle(ok_total=15, fault_total=1)   # counter 16: 3 cycles missing
        run.world.push_cycle(ok_total=2, fault_total=0)    # counter 2: went backwards
        wait_for(lambda: run.count("[CYCLE]") >= 4)
        run.finish()
        events = self.events(run)
        (gap,) = [e for e in events if e["code"] == "CYCLE_GAP"]
        self.assertIn("3 stamping cycle(s) are missing", gap["message"])
        self.assertIn("counter_from=12 counter_to=16", gap["details"])
        (reset,) = [e for e in events if e["code"] == "CYCLE_COUNTER_RESET"]
        self.assertIn("from 16 to 2", reset["message"])

    def test_7b_the_counter_is_remembered_across_a_restart(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(ok_total=10, fault_total=1)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.finish()
        # the PC was off for a while and the press made 4 strokes meanwhile
        run2 = LoopRunner(self, {"database": {"db_type": "none"}})
        run2.config_path = run.config_path
        run2.start()
        wait_for(lambda: "[LINK] state=up" in run2.text())
        time.sleep(0.2)
        run2.world.push_cycle(ok_total=14, fault_total=1)
        wait_for(lambda: run2.count("[CYCLE]") >= 1)
        run2.finish()
        gaps = [e for e in self.events(run) if e["code"] == "CYCLE_GAP"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("counter_from=11 counter_to=15", gaps[0]["details"])
        self.assertEqual(self.codes(run).count("READER_START"), 2, "each start of the reader is on record")

    def test_5_6_database_down_and_back_are_logged(self):
        influx = FakeInflux()
        self.addCleanup(influx.stop)
        cfg = {"db_type": "influxdb", "influx_url": influx.url, "influx_token": "secret", "influx_org": "org",
               "influx_bucket": "jp4", "retry_interval": 0.2}
        run = LoopRunner(self, {"database": cfg}).start()
        wait_for(lambda: len(influx.lines()) >= 2)
        influx.down = True
        wait_for(lambda: "DB_UNREACHABLE" in self.codes(run), what="DB_UNREACHABLE event")
        influx.down = False
        wait_for(lambda: "DB_RESTORED" in self.codes(run), what="DB_RESTORED event")
        events = {e["code"]: e for e in self.events(run)}
        self.assertEqual(events["DB_UNREACHABLE"]["level"], "ERROR")
        self.assertIn("stored at the edge", events["DB_UNREACHABLE"]["message"])
        self.assertIn("syncing", events["DB_RESTORED"]["message"])
        self.assertEqual(self.codes(run).count("DB_UNREACHABLE"), 1, "one line per outage, not one per retry")

    def test_5_full_database_is_reported_as_full_not_as_network(self):
        influx = FakeInflux()
        self.addCleanup(influx.stop)
        influx.full = True
        cfg = {"db_type": "influxdb", "influx_url": influx.url, "influx_token": "secret", "influx_org": "org",
               "influx_bucket": "jp4", "retry_interval": 0.2}
        run = LoopRunner(self, {"database": cfg}).start()
        wait_for(lambda: len(influx.lines()) >= 2)
        influx.down = True
        wait_for(lambda: "DB_FULL" in self.codes(run), what="DB_FULL event")
        full = [e for e in self.events(run) if e["code"] == "DB_FULL"][0]
        self.assertIn("no space left", full["message"])
        self.assertNotIn("DB_UNREACHABLE", self.codes(run))

    def test_classify_db_error_tells_a_wrong_setting_from_a_network_problem(self):
        from database_handler import classify_db_error
        self.assertEqual(classify_db_error(Exception("InfluxDB HTTP 401: unauthorized")), "DB_AUTH")
        self.assertEqual(classify_db_error(Exception("InfluxDB rejected the credentials (HTTP 403): x")), "DB_AUTH")
        self.assertEqual(classify_db_error(Exception("InfluxDB HTTP 404: bucket not found")), "DB_BUCKET")
        self.assertEqual(classify_db_error(Exception("InfluxDB bucket 'jp4' (org 'o') was not found: x")), "DB_BUCKET")
        self.assertEqual(classify_db_error(Exception("cannot reach InfluxDB at http://x: [WinError 10061] refused")), "DB_UNREACHABLE")

    def test_classify_db_error(self):
        from database_handler import classify_db_error
        self.assertEqual(classify_db_error(Exception("InfluxDB HTTP 507: Insufficient Storage")), "DB_FULL")
        self.assertEqual(classify_db_error(Exception("write failed: No space left on device")), "DB_FULL")
        self.assertEqual(classify_db_error(Exception("cannot reach InfluxDB at http://x: timed out")), "DB_UNREACHABLE")

    def test_manual_start_with_database_unreachable_is_logged_before_exiting(self):
        influx = FakeInflux()
        self.addCleanup(influx.stop)
        influx.down = True
        cfg = {"db_type": "influxdb", "influx_url": influx.url, "influx_token": "secret", "influx_org": "org", "influx_bucket": "jp4"}
        run = LoopRunner(self, {"database": cfg}).start()
        run.thread.join(10)
        self.assertEqual(run.result, 2)
        (event,) = [e for e in self.events(run) if e["code"] == "DB_UNREACHABLE"]
        self.assertIn("Reading was not started", event["message"])

    def test_event_file_format_filter_and_rotation(self):
        import event_log
        path = os.path.join(tempfile.mkdtemp(), "logs", "events.log")
        line = event_log.emit("RS232_LOST", "port gone | really\nnow", level="ERROR", path=path, port="COM7", empty=None)
        event_log.emit("SERVICE_START", "panel up", path=path)
        self.assertRegex(line, r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| ERROR   \| COMM ERROR \| RS232_LOST\s+\| port gone / really now \| port=COM7$")
        events = event_log.read_recent(path, 10)
        self.assertEqual([e["code"] for e in events], ["RS232_LOST", "SERVICE_START"])
        self.assertEqual([e["case"] for e in events], ["COMM ERROR", "START"])
        self.assertEqual(events[0]["details"], "port=COM7")
        self.assertEqual([e["code"] for e in event_log.read_recent(path, 10, "WARNING")], ["RS232_LOST"])
        self.assertEqual(len(event_log.read_recent(path, 1)), 1)
        saved = event_log.MAX_BYTES
        try:
            event_log.MAX_BYTES = 10
            event_log.emit("SERVICE_START", "again", path=path)
        finally:
            event_log.MAX_BYTES = saved
        self.assertTrue(os.path.exists(path + ".1"), "a full file is rotated, never truncated")
        self.assertEqual([e["code"] for e in event_log.read_recent(path, 10)], ["SERVICE_START"])
        self.assertEqual(event_log.read_recent(os.path.join(tempfile.mkdtemp(), "none.log")), [])

    def test_every_event_code_has_a_plain_case_label(self):
        import event_log
        expected = {
            "SERVICE_START": "START", "READER_START": "START", "RUN_RESUMED": "START",
            "RUN_STOP": "STOP", "READER_STOP": "STOP", "SERVICE_STOP": "SHUTDOWN",
            "RS232_LOST": "COMM ERROR", "PRESS_NO_RESPONSE": "COMM ERROR",
            "RS232_RECOVERED": "RECOVERED", "PRESS_RECOVERED": "RECOVERED", "DB_RESTORED": "RECOVERED",
            "PRESS_NG": "NG", "UNCLEAN_SHUTDOWN": "CRASH", "READER_RESTARTED": "CRASH", "READER_ERROR": "CRASH",
            "DB_UNREACHABLE": "DB ERROR", "DB_FULL": "DB FULL",
            "CYCLE_GAP": "DATA CHECK", "CYCLE_COUNTER_RESET": "DATA CHECK",
        }
        for code, case in expected.items():
            self.assertEqual(event_log.case_of(code), case, code)
        self.assertEqual(event_log.case_of("SOMETHING_NEW"), "INFO", "an unknown code is still shown, as INFO")

    def test_press_on_but_idle_for_10_s_is_logged_once_and_again_when_it_works(self):
        import read_janome_jp4 as reader
        import event_log
        now = [100.0]
        path = os.path.join(tempfile.mkdtemp(), "logs", "events.log")
        event_log.configure(path)
        self.addCleanup(event_log.configure, None)
        watch = reader.IdleWatch(10, label="COM7", clock=lambda: now[0])
        watch.check(True)                       # link just came up: the quiet period starts
        now[0] += 9.5
        watch.check(True)
        self.assertEqual(event_log.read_recent(path), [], "not yet 10 s")
        now[0] += 1.0
        watch.check(True)
        now[0] += 30
        watch.check(True)                       # still idle: no second line
        (idle,) = event_log.read_recent(path)
        self.assertEqual((idle["code"], idle["case"], idle["level"]), ("PRESS_IDLE", "IDLE", "INFO"))
        self.assertIn("no stroke for 10 s", idle["message"])
        watch.cycles_seen(1)
        working = event_log.read_recent(path)[-1]
        self.assertEqual((working["code"], working["case"]), ("PRESS_WORKING", "WORKING"))
        self.assertIn("idle for 40 s", working["message"])
        # a stroke every few seconds is never idle; a link problem is not idle either
        for _ in range(5):
            now[0] += 8
            watch.cycles_seen(1)
            watch.check(True)
        watch.check(False)
        now[0] += 60
        watch.check(False)
        self.assertEqual([e["code"] for e in event_log.read_recent(path)], ["PRESS_IDLE", "PRESS_WORKING"])
        disabled = reader.IdleWatch(0, clock=lambda: now[0])
        disabled.check(True)
        now[0] += 999
        disabled.check(True)
        self.assertEqual(len(event_log.read_recent(path)), 2)

    def test_the_running_reader_logs_idle_when_no_stroke_comes(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}, "event_log": {"idle_alert_s": 1}}).start()
        wait_for(lambda: "PRESS_IDLE" in self.codes(run), what="PRESS_IDLE event")
        run.world.push_cycle(result_code=0)
        wait_for(lambda: "PRESS_WORKING" in self.codes(run), what="PRESS_WORKING event")
        run.finish()
        self.assertEqual(self.codes(run).count("PRESS_IDLE"), 1)

    def test_a_case_can_be_overridden_and_old_lines_still_read(self):
        import event_log
        path = os.path.join(tempfile.mkdtemp(), "logs", "events.log")
        line = event_log.emit("PRESS_NG", "stopped", level="WARNING", path=path, case="NG STOP")
        self.assertIn("| NG STOP    | PRESS_NG", line)
        with open(path, "a", encoding="utf-8") as f:   # a line from before the case column existed
            f.write("2026-11-01 08:00:00 | ERROR   | UNCLEAN_SHUTDOWN  | old line | run=1\n")
        events = event_log.read_recent(path, 10)
        self.assertEqual([e["case"] for e in events], ["NG STOP", "CRASH"])
        self.assertEqual(events[1]["message"], "old line")
        self.assertEqual(events[1]["details"], "run=1")

    def test_a_crash_reader_exit_is_labelled_crash_and_a_normal_stop_is_not(self):
        run = LoopRunner(self, {"database": {"db_type": "none"}}).start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=3232)
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        run.finish()
        by_code = {e["code"]: e for e in self.events(run)}
        self.assertEqual(by_code["READER_START"]["case"], "START")
        self.assertEqual(by_code["PRESS_NG"]["case"], "NG STOP", "result 3232 = stopped before finishing")

    def test_a_log_file_that_cannot_be_written_never_stops_the_collection(self):
        import event_log
        blocker = os.path.join(tempfile.mkdtemp(), "file")
        with open(blocker, "w") as f:
            f.write("x")
        event_log.emit("SERVICE_START", "still fine", path=os.path.join(blocker, "sub", "events.log"))  # must not raise


class SingleCsvCases(unittest.TestCase):
    """Only one CSV file is written: the cycle file, with the report columns first."""

    FIRST = ["pressing_speed_mm_s", "return_speed_mm_s", "start_position_mm", "stroke_mm",
             "end_position_mm", "end_load_kgf", "end_load_setting_kgf", "end_load_diff_kgf",
             "prep_time_s", "work_time_s"]

    def run_with_cycle(self, extras=True):
        run = LoopRunner(self, {"database": {"db_type": "none"}})
        run.world.extras = extras
        run.start()
        wait_for(lambda: "[LINK] state=up" in run.text())
        time.sleep(0.2)
        run.world.push_cycle(result_code=3232, tail="ABCD")
        wait_for(lambda: run.count("[CYCLE]") >= 1)
        wait_for(lambda: run.count("[STATS]") >= 6)
        run.finish()
        return run

    def read(self, run):
        import csv
        folder = os.path.join(run.tmp, "exports")
        (name,) = os.listdir(folder)
        self.assertTrue(name.startswith("cycles_run_"), "no other CSV file is written")
        with open(os.path.join(folder, name), newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def test_columns_start_with_the_report_and_none_is_repeated(self):
        import csv_export
        self.assertEqual(csv_export.COLUMNS[:len(self.FIRST)], self.FIRST)
        self.assertEqual(len(csv_export.COLUMNS), len(set(csv_export.COLUMNS)), "no column twice")
        for name in ("time_local", "time_utc", "run_id", "program_number", "ok", "result_code", "result_text",
                     "end_position_set_mm", "end_load_n",
                     "contact_position_mm", "contact_load_n", "ok_total", "fault_total", "lifetime_total",
                     "sensor_fault_section", "sensor_fault_number", "backlog"):
            self.assertIn(name, csv_export.COLUMNS)
        self.assertFalse([c for c in csv_export.COLUMNS if c.startswith(("set_", "v1_", "io_", "full"))])
        self.assertNotIn("cycle_time_s", csv_export.COLUMNS)
        self.assertFalse([c for c in csv_export.COLUMNS if c.startswith("uph")], "UPH is not in the file")

    def test_the_reader_writes_one_file_with_the_report_first(self):
        run = self.run_with_cycle()
        header, row = self.read(run)
        self.assertEqual(header[:len(self.FIRST)], self.FIRST)
        values = dict(zip(header, row))
        self.assertEqual([values[c] for c in self.FIRST[:4]], ["100.0", "80.0", "29.0", "8.814"])
        self.assertEqual(values["result_code"], "3232")
        cycle_lines = [ln for ln in run.text().splitlines() if ln.startswith("[CYCLE]")]
        self.assertNotIn("v1_raw", cycle_lines[0])
        conn = sqlite3.connect(os.path.join(run.tmp, "janome_jp4_local.db"))
        self.assertNotIn("v1_raw", json.loads(conn.execute("SELECT payload FROM cycles").fetchone()[0]))

    def test_press_that_ignores_the_settings_read_never_stops_the_collection(self):
        run = self.run_with_cycle(extras=False)
        self.assertNotIn("[LINK] state=no_response", run.text())
        header, row = self.read(run)
        values = dict(zip(header, row))
        self.assertEqual([values[c] for c in ("pressing_speed_mm_s", "return_speed_mm_s", "start_position_mm", "stroke_mm")], [""] * 4)
        self.assertEqual(values["result_code"], "3232")
        self.assertIn("[CSV] Extra read failed", run.text())


if __name__ == "__main__":
    unittest.main()
