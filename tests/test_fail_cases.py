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


def v1_data(result_code=0, ok_total=10, fault_total=1, end_pos=87.1575, end_load=677.8):
    """88 hex characters in the Quality Data (V1) layout."""
    return (f"{1:04X}{ok_total:08X}{fault_total:08X}{ok_total + fault_total:08X}{result_code:04X}{0:04X}{0:04X}"
            f"{28098:08X}{1310:08X}{int(86.162 * 10000):08X}{int(417.1 * 10):08X}"
            f"{int(end_pos * 10000):08X}{int(end_load * 10):08X}")


class World:
    """State shared by every fake serial connection: is the cable in, is the press on."""

    def __init__(self):
        self.plugged = True
        self.powered = True
        self.q1 = False
        self.q1_count = 0
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
        self.writes = []          # raw request bodies that were accepted
        self.write_headers = []
        self.retention_patches = []
        self.reject_marker = None  # a body containing this text gets HTTP 422
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
                    return self._send(503, b'{"message":"unavailable"}')
                if self.headers.get("Authorization") != "Token secret":
                    return self._send(401, b'{"message":"unauthorized"}')
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


if __name__ == "__main__":
    unittest.main()
