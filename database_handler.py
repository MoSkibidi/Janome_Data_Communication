import sqlite3
import datetime
import os
import logging
import json
import threading
import re
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

logger = logging.getLogger(__name__)

# Per-cycle ("quality data") record: one row per press stroke, OK or NG.
# (column name, kind) - kind decides the SQL type; InfluxDB uses the same names as fields.
CYCLE_FIELDS = (
    ("program_number", "int"), ("ok", "bool"), ("result_code", "int"), ("result_text", "str"),
    ("prep_time_s", "float"), ("work_time_s", "float"),
    ("contact_position_mm", "float"), ("contact_load_n", "float"),
    ("end_position_mm", "float"), ("end_load_n", "float"),
    ("ok_total", "int"), ("fault_total", "int"), ("lifetime_total", "int"),
    ("sensor_fault_section", "int"), ("sensor_fault_number", "int"),
    ("backlog", "bool"),
)
_SQL_TYPES = {
    "sqlite": {"int": "INTEGER", "bool": "INTEGER", "str": "TEXT", "float": "REAL"},
    "postgresql": {"int": "INTEGER", "bool": "BOOLEAN", "str": "TEXT", "float": "DOUBLE PRECISION"},
    "mysql": {"int": "INT", "bool": "BOOLEAN", "str": "TEXT", "float": "DOUBLE"},
}


def _ts_ns(timestamp):
    """UTC datetime -> integer nanoseconds since the epoch (exact, no float rounding)."""
    if timestamp is None:
        return time.time_ns()
    return int(timestamp.timestamp()) * 10**9 + timestamp.microsecond * 1000


def _lp_escape(text):
    """Escapes a measurement name or tag value for InfluxDB line protocol."""
    return str(text).replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ").replace("=", "\\=")


_INFLUX_ENV_OVERRIDES = {
    "INFLUX_URL": "influx_url",
    "INFLUX_TOKEN": "influx_token",
    "INFLUX_ORG": "influx_org",
    "INFLUX_BUCKET": "influx_bucket",
}


def _apply_env_overrides(db_config):
    """
    Lets a deployment (e.g. the Docker Compose stack, which only knows the InfluxDB
    container's address/token at `docker compose up` time) point this at InfluxDB via
    environment variables instead of editing config.json. Only applied when set.
    """
    for env_name, key in _INFLUX_ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            db_config[key] = value


class DatabaseHandler:
    def __init__(self, db_config, connect=True):
        """
        Initializes Database Connection based on configuration.
        Supports SQLite out of the box, with extensible support for PostgreSQL/MySQL.

        With connect=False nothing is opened yet; the first reconnect() connects and
        creates the table (used when starting while the database is still down).
        """
        _apply_env_overrides(db_config)
        self.config = db_config
        self.db_type = db_config.get("db_type", "sqlite").lower()
        self.db_name = db_config.get("db_name", "janome_jp4_data.db")
        self.table_name = db_config.get("table_name", "janome_jp4_telemetry")
        self.cycles_table = db_config.get("cycles_table") or f"{self.table_name}_cycles"
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
            self.conn = None
            self.influx_check()
        else:
            raise ValueError(f"Unsupported database type: {self.db_type}")

    def init_db(self):
        """Creates the target tables (telemetry and per-cycle results) if they do not already exist."""
        if self.db_type == "influxdb":
            return

        cursor = self.conn.cursor()
        sql_kind = "postgresql" if self.db_type in ("postgres", "postgresql", "timescaledb") else self.db_type
        types = _SQL_TYPES[sql_kind]
        id_col = {
            "sqlite": "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "postgresql": "id SERIAL PRIMARY KEY",
            "mysql": "id INT AUTO_INCREMENT PRIMARY KEY",
        }[sql_kind]
        ts_type = {"sqlite": "TEXT", "postgresql": "TIMESTAMP WITH TIME ZONE", "mysql": "DATETIME"}[sql_kind]
        cycle_cols = ",\n                ".join(f"{name} {types[kind]}" for name, kind in CYCLE_FIELDS)
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.cycles_table} (
                {id_col},
                timestamp {ts_type} NOT NULL,
                {cycle_cols}
            );
            """)

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
            self._influx_write([self._influx_line(data, timestamp) for timestamp, data in records])
            return
        try:
            for timestamp, data in records:
                if data.get("_kind") == "cycle":
                    self.insert_cycle(data, timestamp=timestamp, commit=False)
                else:
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
            self._influx_write([self._influx_line(data, timestamp)])
            return True

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

    def insert_cycle(self, data, timestamp=None, commit=True):
        """Inserts one per-cycle result row (SQL back ends; InfluxDB goes through insert_many)."""
        now_dt = timestamp or datetime.datetime.now(datetime.timezone.utc)
        db_timestamp = now_dt.isoformat(" ") if self.db_type == "sqlite" else now_dt
        placeholder = "?" if self.db_type == "sqlite" else "%s"
        columns = ["timestamp"] + [name for name, _ in CYCLE_FIELDS]
        query = (f"INSERT INTO {self.cycles_table} ({', '.join(columns)}) "
                 f"VALUES ({', '.join([placeholder] * len(columns))});")
        params = [db_timestamp] + [data.get(name) for name, _ in CYCLE_FIELDS]
        try:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            if commit:
                self.conn.commit()
            cursor.close()
        except Exception:
            try:
                self.conn.rollback()
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # InfluxDB (v2 token/org/bucket, or v1 database + optional user/password).
    # Samples carry the time they were read, so a replay after an outage keeps
    # the original timestamps; identical series+timestamp simply overwrite in
    # InfluxDB, so re-sending a batch after a crash does not create duplicates.
    # ------------------------------------------------------------------

    def _influx_url(self):
        return (self.config.get("influx_url") or "http://localhost:8086").rstrip("/")

    def _influx_v1(self):
        return str(self.config.get("influx_version") or "2").startswith("1")

    def _influx_target(self):
        """Bucket (v2) or database (v1) name."""
        return self.config.get("influx_bucket") or self.config.get("influx_database") or "janome_jp4"

    def _influx_headers(self, content_type="text/plain; charset=utf-8"):
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if self._influx_v1():
            user = self.config.get("influx_user") or ""
            if user:
                raw = f"{user}:{self.config.get('influx_password') or ''}".encode("utf-8")
                headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        elif self.config.get("influx_token"):
            headers["Authorization"] = f"Token {self.config['influx_token']}"
        return headers

    def _influx_request(self, method, path, body=None, content_type="text/plain; charset=utf-8", timeout=5.0):
        """Returns (http_status, body_text) for any HTTP answer; raises only when the server cannot be reached."""
        req = urllib.request.Request(self._influx_url() + path, data=body,
                                     headers=self._influx_headers(content_type), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")

    def _influx_write_path(self):
        target = urllib.parse.quote(self._influx_target())
        if self._influx_v1():
            return f"/write?db={target}&precision=ns"
        org = urllib.parse.quote(self.config.get("influx_org") or "")
        return f"/api/v2/write?org={org}&bucket={target}&precision=ns"

    def _influx_line(self, data, timestamp):
        """One line-protocol line for a telemetry sample or a per-cycle result."""
        ts = _ts_ns(timestamp)
        if data.get("_kind") == "cycle":
            measurement = self.config.get("influx_cycle_measurement") or self.cycles_table
            tags = f"{_lp_escape(measurement)},result={'OK' if data.get('ok') else 'NG'}"
            if data.get("program_number") is not None:
                tags += f",program={int(data['program_number'])}"
            fields = []
            for name, kind in CYCLE_FIELDS:
                value = data.get(name)
                if value is None:
                    continue
                if kind == "int":
                    fields.append(f"{name}={int(value)}i")
                elif kind == "bool":
                    fields.append(f"{name}={'true' if value else 'false'}")
                elif kind == "str":
                    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                    fields.append(f'{name}="{escaped}"')
                else:
                    fields.append(f"{name}={float(value)!r}")
            return f"{tags} {','.join(fields)} {ts}"
        measurement = self.config.get("influx_measurement") or self.table_name
        fields = [
            f"encoder_position_mm={float(data.get('encoder_position_mm', 0.0))!r}",
            f"teaching_position_mm={float(data.get('teaching_position_mm', 0.0))!r}",
            f"load_n={float(data.get('load_n', 0.0))!r}",
        ]
        return f"{_lp_escape(measurement)} {','.join(fields)} {ts}"

    @staticmethod
    def _influx_message(body):
        try:
            parsed = json.loads(body)
            return parsed.get("message") or parsed.get("error") or body[:200]
        except Exception:
            return (body or "").strip()[:200]

    def _influx_write(self, lines):
        """
        Sends lines in one request. An unreachable server, bad credentials, a missing bucket
        or a server error raises (the caller keeps the data and retries). A line the server
        rejects as malformed (HTTP 400/422) is logged and dropped so one bad point can never
        block everything queued behind it.
        """
        if not lines:
            return
        ok = (200, 204)
        status, body = self._influx_request("POST", self._influx_write_path(), "\n".join(lines).encode("utf-8"))
        if status in ok:
            return
        if status in (400, 422):
            if len(lines) > 1:
                for line in lines:
                    self._influx_write([line])
                return
            logger.error(f"[DB] InfluxDB rejected a sample (HTTP {status}: {self._influx_message(body)}); dropped: {lines[0][:120]}")
            return
        raise Exception(f"InfluxDB HTTP {status}: {self._influx_message(body)}")

    def influx_check(self):
        """Raises with a clear message if InfluxDB is unreachable or rejects the credentials/bucket."""
        url = self._influx_url()
        target = self._influx_target()
        v1 = self._influx_v1()
        try:
            status, body = self._influx_request("GET", "/ping" if v1 else "/health", timeout=4.0)
        except Exception as err:
            raise Exception(f"cannot reach InfluxDB at {url}: {err}")
        if status not in (200, 204):
            raise Exception(f"InfluxDB at {url} answered HTTP {status} to the health check")

        # An empty write checks the credentials and the bucket/database without storing anything
        status, body = self._influx_request("POST", self._influx_write_path(), b"")
        if status == 404 and v1:
            create = urllib.parse.urlencode({"q": f'CREATE DATABASE "{target}"'}).encode()
            c_status, c_body = self._influx_request("POST", "/query", create, "application/x-www-form-urlencoded")
            if c_status != 200:
                raise Exception(f"InfluxDB database '{target}' does not exist and could not be created (HTTP {c_status}: {self._influx_message(c_body)})")
            logger.info(f"InfluxDB database '{target}' created.")
        elif status in (401, 403):
            raise Exception(f"InfluxDB rejected the credentials (HTTP {status}): {self._influx_message(body)}")
        elif status == 404:
            raise Exception(f"InfluxDB bucket '{target}' (org '{self.config.get('influx_org')}') was not found: {self._influx_message(body)}")
        logger.info(f"Connected to InfluxDB at {url} ({'v1 database' if v1 else 'v2 bucket'} '{target}').")

        try:
            self._influx_apply_retention()
        except Exception as err:
            logger.warning(f"[DB] Could not apply the InfluxDB retention setting: {err}")

    def _influx_apply_retention(self):
        """
        Sets how long InfluxDB keeps data (database config key influx_retention_days; 0 or
        empty leaves the server's setting alone). InfluxDB deletes older data by itself, which
        is what stops the disk from filling up. Needs a token/user allowed to edit the bucket.
        """
        days = int(self.config.get("influx_retention_days") or 0)
        if days <= 0:
            return
        target = self._influx_target()
        if self._influx_v1():
            q = urllib.parse.urlencode({"q": f'SHOW RETENTION POLICIES ON "{target}"'})
            status, body = self._influx_request("GET", f"/query?{q}")
            if status != 200:
                raise Exception(f"HTTP {status}: {self._influx_message(body)}")
            series = json.loads(body)["results"][0].get("series") or []
            rows = series[0]["values"] if series else []
            policy = next((r for r in rows if r[4]), rows[0] if rows else None)
            if policy is None:
                return
            seconds = sum(int(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+)([hms])", policy[1]))
            if seconds != days * 86400:
                alter = urllib.parse.urlencode({"q": f'ALTER RETENTION POLICY "{policy[0]}" ON "{target}" DURATION {days}d'}).encode()
                a_status, a_body = self._influx_request("POST", "/query", alter, "application/x-www-form-urlencoded")
                if a_status != 200:
                    raise Exception(f"HTTP {a_status}: {self._influx_message(a_body)}")
                logger.info(f"[DB] InfluxDB retention of '{target}' set to {days} day(s).")
            return

        q = urllib.parse.urlencode({"name": target, "org": self.config.get("influx_org") or ""})
        status, body = self._influx_request("GET", f"/api/v2/buckets?{q}")
        if status != 200:
            raise Exception(f"HTTP {status}: {self._influx_message(body)} (the token needs permission to read/edit buckets; retention can also be set in the InfluxDB UI)")
        buckets = json.loads(body).get("buckets") or []
        if not buckets:
            return
        bucket = buckets[0]
        rules = bucket.get("retentionRules") or []
        if rules and rules[0].get("everySeconds") == days * 86400:
            return
        patch = json.dumps({"retentionRules": [{"type": "expire", "everySeconds": days * 86400}]}).encode()
        p_status, p_body = self._influx_request("PATCH", f"/api/v2/buckets/{bucket['id']}", patch, "application/json")
        if p_status != 200:
            raise Exception(f"HTTP {p_status}: {self._influx_message(p_body)}")
        logger.info(f"[DB] InfluxDB retention of bucket '{target}' set to {days} day(s).")

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
