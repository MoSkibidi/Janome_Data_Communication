import sqlite3
import datetime
import os
import logging
import json
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

class DatabaseHandler:
    def __init__(self, db_config, connect=True):
        """
        Initializes Database Connection based on configuration.
        Supports SQLite out of the box, with extensible support for PostgreSQL/MySQL.

        With connect=False nothing is opened yet; the first reconnect() connects and
        creates the table (used when starting while the database is still down).
        """
        self.config = db_config
        self.db_type = db_config.get("db_type", "sqlite").lower()
        self.db_name = db_config.get("db_name", "janome_jp4_data.db")
        self.table_name = db_config.get("table_name", "janome_jp4_telemetry")
        self.description = db_config.get("description", "JANOME Electro Press JP4 Telemetry DB")

        self.conn = None
        self._schema_ready = False
        if connect:
            self.connect()
            self.init_db()
            self._schema_ready = True

    def connect(self):
        """Establishes connection to the configured database. Auto-creates DB if missing."""
        if self.db_type == "sqlite":
            # sqlite_path is what the panel's "SQLite file path" field saves; db_name is the PostgreSQL database name
            sqlite_file = self.config.get("sqlite_path") or self.db_name
            self.conn = sqlite3.connect(sqlite_file, check_same_thread=False)
            logger.info(f"Connected to SQLite database: {sqlite_file}")
        elif self.db_type in ("postgres", "postgresql", "timescaledb"):
            try:
                import psycopg2

                def pg_connect(dbname):
                    # connect_timeout bounds how long an unreachable server can block us
                    return psycopg2.connect(
                        dbname=dbname,
                        user=self.config.get("user", "postgres"),
                        password=self.config.get("password", ""),
                        host=self.config.get("host", "localhost"),
                        port=self.config.get("port", 5432),
                        connect_timeout=self.config.get("connect_timeout", 5)
                    )

                try:
                    self.conn = pg_connect(self.db_name)
                except psycopg2.OperationalError as oe:
                    # Only try to create the database when the server is reachable and says it is missing;
                    # network/auth failures are re-raised as-is so the real cause is reported.
                    if "does not exist" not in str(oe):
                        raise
                    logger.warning(f"Database '{self.db_name}' does not exist ({oe}). Attempting database creation...")
                    maint_conn = pg_connect("postgres")
                    maint_conn.autocommit = True
                    with maint_conn.cursor() as cur:
                        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (self.db_name,))
                        if not cur.fetchone():
                            cur.execute(f'CREATE DATABASE "{self.db_name}"')
                            logger.info(f"Database '{self.db_name}' auto-created successfully.")
                    maint_conn.close()
                    self.conn = pg_connect(self.db_name)
                logger.info(f"Connected to PostgreSQL database: {self.db_name} at {self.config.get('host')}")
            except ImportError:
                raise ImportError("psycopg2 package is required for PostgreSQL connections.")
        elif self.db_type == "mysql":
            try:
                import mysql.connector
                try:
                    self.conn = mysql.connector.connect(
                        database=self.db_name,
                        user=self.config.get("user", "root"),
                        password=self.config.get("password", ""),
                        host=self.config.get("host", "localhost"),
                        port=self.config.get("port", 3306)
                    )
                except Exception as me:
                    logger.warning(f"Connecting to MySQL database '{self.db_name}' failed ({me}). Attempting database creation...")
                    maint_conn = mysql.connector.connect(
                        user=self.config.get("user", "root"),
                        password=self.config.get("password", ""),
                        host=self.config.get("host", "localhost"),
                        port=self.config.get("port", 3306)
                    )
                    with maint_conn.cursor() as cur:
                        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db_name}`")
                        logger.info(f"MySQL database '{self.db_name}' auto-created successfully.")
                    maint_conn.close()
                    self.conn = mysql.connector.connect(
                        database=self.db_name,
                        user=self.config.get("user", "root"),
                        password=self.config.get("password", ""),
                        host=self.config.get("host", "localhost"),
                        port=self.config.get("port", 3306)
                    )
                logger.info(f"Connected to MySQL database: {self.db_name} at {self.config.get('host')}")
            except ImportError:
                raise ImportError("mysql-connector-python package is required for MySQL connections.")
        elif self.db_type == "influxdb":
            logger.info("Configured InfluxDB destination.")
            self.conn = None
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    def init_db(self):
        """Creates the target table if it does not already exist."""
        if self.db_type == "influxdb":
            return

        cursor = self.conn.cursor()

        if self.db_type == "sqlite":
            query = f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                encoder_position_mm REAL NOT NULL,
                teaching_position_mm REAL NOT NULL,
                load_n REAL NOT NULL,
                raw_payload TEXT NOT NULL
            );
            """
        elif self.db_type in ("postgres", "postgresql", "timescaledb"):
            query = f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id SERIAL PRIMARY KEY,
                timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                encoder_position_mm DOUBLE PRECISION NOT NULL,
                teaching_position_mm DOUBLE PRECISION NOT NULL,
                load_n DOUBLE PRECISION NOT NULL,
                raw_payload TEXT NOT NULL
            );
            """
        elif self.db_type == "mysql":
            query = f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id INT AUTO_INCREMENT PRIMARY KEY,
                timestamp DATETIME NOT NULL,
                encoder_position_mm DOUBLE NOT NULL,
                teaching_position_mm DOUBLE NOT NULL,
                load_n DOUBLE NOT NULL,
                raw_payload TEXT NOT NULL
            );
            """

        cursor.execute(query)
        self.conn.commit()
        cursor.close()
        logger.info(f"Database table '{self.table_name}' verified/initialized.")

    def reconnect(self):
        """Drops the current connection and opens a new one; creates the table only if never done."""
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.connect()
        if not self._schema_ready:
            self.init_db()
            self._schema_ready = True

    def insert_many(self, records):
        """
        Inserts several (timestamp, data) records in one transaction: all or nothing
        (InfluxDB has no transactions, so it writes them one by one).
        """
        if not records:
            return
        if self.db_type == "influxdb":
            for timestamp, data in records:
                self._insert_influx(data, timestamp)
            return
        try:
            for timestamp, data in records:
                self.insert_telemetry(data, timestamp=timestamp, commit=False)
            self.conn.commit()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise
        logger.info(f"Inserted {len(records)} record(s) into '{self.table_name}'")

    def insert_telemetry(self, data, timestamp=None, commit=True):
        """
        Inserts a single telemetry record into the database.
        Raises on any failure; the caller decides whether to retry or buffer.

        :param data: Dictionary containing telemetry parameters from JanomeJP4Press
        :param timestamp: Time the sample was read (UTC datetime); defaults to now
        :param commit: Commit immediately; insert_many passes False to commit once at the end
        :return: Inserted record ID or boolean success
        """
        if self.db_type == "influxdb":
            return self._insert_influx(data, timestamp)

        now_dt = timestamp or datetime.datetime.now(datetime.timezone.utc)
        if self.db_type == "sqlite":
            db_timestamp = now_dt.isoformat(" ")
        else:
            db_timestamp = now_dt

        if self.db_type in ("sqlite", "postgres", "postgresql", "timescaledb"):
            placeholder = "%s" if self.db_type != "sqlite" else "?"
            query = f"""
            INSERT INTO {self.table_name} (
                timestamp, encoder_position_mm, teaching_position_mm,
                load_n, raw_payload
            ) VALUES ({', '.join([placeholder]*5)});
            """
            params = (
                db_timestamp,
                data.get("encoder_position_mm", 0.0),
                data.get("teaching_position_mm", 0.0),
                data.get("load_n", 0.0),
                data.get("raw_payload", "")
            )
        elif self.db_type == "mysql":
            query = f"""
            INSERT INTO {self.table_name} (
                timestamp, encoder_position_mm, teaching_position_mm,
                load_n, raw_payload
            ) VALUES (%s, %s, %s, %s, %s);
            """
            params = (
                db_timestamp,
                data.get("encoder_position_mm", 0.0),
                data.get("teaching_position_mm", 0.0),
                data.get("load_n", 0.0),
                data.get("raw_payload", "")
            )

        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            if commit:
                self.conn.commit()
            last_row_id = getattr(cursor, "lastrowid", None)
            cursor.close()
            if commit:
                logger.info(f"Inserted record into '{self.table_name}' at {now_dt}")
            return last_row_id
        except Exception:
            # Leave the connection usable (or visibly broken) for the caller's retry logic
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    def _insert_influx(self, data, timestamp=None):
        import time
        import urllib.request
        import urllib.parse
        url = self.config.get("influx_url", "http://localhost:8086").rstrip('/')
        token = self.config.get("influx_token", "")
        org = self.config.get("influx_org", "mddp")
        bucket = self.config.get("influx_bucket", "janome_jp4_telemetry")
        measurement = self.config.get("influx_measurement", self.table_name)

        write_url = f"{url}/api/v2/write?org={urllib.parse.quote(org)}&bucket={urllib.parse.quote(bucket)}&precision=s"
        fields = [
            f"encoder_position_mm={float(data.get('encoder_position_mm', 0.0))}",
            f"teaching_position_mm={float(data.get('teaching_position_mm', 0.0))}",
            f"load_n={float(data.get('load_n', 0.0))}",
        ]
        ts_sec = int(timestamp.timestamp()) if timestamp else int(time.time())
        line_protocol = f"{measurement} {','.join(fields)} {ts_sec}"

        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Accept": "application/json"
        }
        if token:
            headers["Authorization"] = f"Token {token}"

        req = urllib.request.Request(write_url, data=line_protocol.encode('utf-8'), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            if resp.status not in (200, 204):
                raise Exception(f"InfluxDB HTTP status {resp.status}")
        return True

    def close(self):
        """Closes the database connection cleanly."""
        if self.conn:
            self.conn.close()
            self.conn = None
            logger.info("Database connection closed.")


class DiskSpool:
    """
    Durable on-disk queue (a small SQLite file) used while the database is unreachable.

    Samples are appended in arrival order and removed only after the database has
    committed them, so a crash or power loss never loses what was already spooled.
    If the spool grows past max_rows the oldest rows are discarded.
    """

    def __init__(self, path, max_rows=1000000):
        self.path = path
        self.max_rows = max_rows
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA synchronous=FULL")  # survive power loss, not only a process crash
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS spool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.conn.commit()
        self.count = self.conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0]

    def append(self, items):
        """Stores [(timestamp, data), ...] in one transaction; returns how many old rows were discarded."""
        rows = [(ts.isoformat(), json.dumps(data)) for ts, data in items]
        with self.conn:
            self.conn.executemany("INSERT INTO spool (ts, payload) VALUES (?, ?)", rows)
        self.count += len(rows)

        excess = self.count - self.max_rows
        if excess > 0:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM spool WHERE id IN (SELECT id FROM spool ORDER BY id LIMIT ?)", (excess,)
                )
            self.count -= excess
            return excess
        return 0

    def peek(self, limit):
        """Returns the oldest rows as [(id, timestamp, data), ...] without removing them."""
        cur = self.conn.execute("SELECT id, ts, payload FROM spool ORDER BY id LIMIT ?", (limit,))
        return [(row_id, datetime.datetime.fromisoformat(ts), json.loads(payload)) for row_id, ts, payload in cur.fetchall()]

    def remove_up_to(self, last_id, n_rows):
        """Deletes rows with id <= last_id (the n_rows just returned by peek)."""
        with self.conn:
            self.conn.execute("DELETE FROM spool WHERE id <= ?", (last_id,))
        self.count -= n_rows

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class BufferedDatabaseWriter:
    """
    Writes telemetry to a DatabaseHandler from a background thread so serial polling is
    never blocked by a slow or unreachable database.

    Normal path: samples are queued in memory with the time they were read and inserted
    right away. While the database is unreachable, the queue is moved to the DiskSpool
    immediately and stays there (surviving a crash or restart). After reconnecting, the
    spool is replayed first, then the in-memory queue, so order and read timestamps are
    preserved. A crash while replaying can re-send at most one batch (duplicate rows).
    """

    BATCH_SIZE = 500

    def __init__(self, handler, spool, max_buffer=100000, retry_interval=5.0, connected=True):
        self.handler = handler
        self.spool = spool
        self.max_buffer = max_buffer
        self.retry_interval = retry_interval
        self.connected = connected
        self.written = 0
        self.db_errors = 0
        self.dropped = 0

        self._buffer = deque()
        self._cond = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._thread.start()

    @property
    def pending(self):
        """Samples not yet committed to the database (memory + spool)."""
        with self._cond:
            return len(self._buffer) + self.spool.count

    def submit(self, data, timestamp):
        """Queues one sample; returns immediately."""
        with self._cond:
            if len(self._buffer) >= self.max_buffer:
                self._buffer.popleft()
                self._note_dropped(1)
            self._buffer.append((timestamp, data))
            self._cond.notify()

    def _note_dropped(self, n):
        before = self.dropped
        self.dropped += n
        if before == 0 or self.dropped // 1000 > before // 1000:
            logger.warning(f"[DB] Dropped {self.dropped} oldest sample(s) because the buffer/spool limit was reached.")

    def _spill(self):
        """Moves the in-memory queue to disk; keeps it in memory if the disk write fails."""
        with self._cond:
            items = list(self._buffer)
            self._buffer.clear()
        if not items:
            return
        try:
            discarded = self.spool.append(items)
        except Exception as err:
            logger.error(f"[DB] Could not write {len(items)} sample(s) to the disk spool ({err}); keeping them in memory.")
            with self._cond:
                self._buffer.extendleft(reversed(items))
            return
        if discarded:
            self._note_dropped(discarded)

    def _mark_down(self, err, next_retry):
        self.db_errors += 1
        self.connected = False
        next_retry[0] = time.monotonic() + self.retry_interval
        logger.warning(f"[DB] Database unavailable ({err}); spooling samples to disk until it is back.")

    def _run(self):
        next_retry = [0.0]
        while True:
            with self._cond:
                idle = not self._buffer and self.spool.count == 0
                if idle:
                    if self._stop:
                        return
                    self._cond.wait(timeout=1.0)
                    continue

            if not self.connected:
                self._spill()
                if self._stop:
                    return
                remaining = next_retry[0] - time.monotonic()
                if remaining > 0:
                    with self._cond:
                        self._cond.wait(timeout=min(remaining, 0.5))
                    continue
                try:
                    self.handler.reconnect()
                except Exception as err:
                    self.db_errors += 1
                    next_retry[0] = time.monotonic() + self.retry_interval
                    logger.warning(f"[DB] Reconnect failed ({err}); {self.pending} sample(s) waiting.")
                    continue
                self.connected = True
                logger.info(f"[DB] Connection restored; sending {self.pending} waiting sample(s).")

            # Connected: oldest first, so replay the disk spool before the in-memory queue
            if self.spool.count:
                rows = self.spool.peek(self.BATCH_SIZE)
                try:
                    self.handler.insert_many([(ts, data) for _, ts, data in rows])
                except Exception as err:
                    self._mark_down(err, next_retry)
                    continue
                self.spool.remove_up_to(rows[-1][0], len(rows))
                self.written += len(rows)
                continue

            with self._cond:
                batch = []
                while self._buffer and len(batch) < self.BATCH_SIZE:
                    batch.append(self._buffer.popleft())
            if not batch:
                continue
            try:
                self.handler.insert_many(batch)
            except Exception as err:
                with self._cond:
                    self._buffer.extendleft(reversed(batch))
                self._mark_down(err, next_retry)
                continue
            self.written += len(batch)

    def close(self, timeout=10.0):
        """Stops the writer, keeping anything unsent in the spool for the next start."""
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        self._thread.join(timeout)
        self._spill()
        left = self.spool.count
        if left:
            logger.warning(f"[DB] {left} sample(s) not yet in the database were kept in '{self.spool.path}' and will be sent on the next start.")
        self.spool.close()
        self.handler.close()
