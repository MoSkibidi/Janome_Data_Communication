import serial
import serial.tools.list_ports
import time
import json
import os
import sys
import argparse
import logging
import datetime
import local_store
import csv_export
import event_log
from database_handler import DatabaseHandler, BufferedDatabaseWriter, DiskSpool, classify_db_error

# Ensure UTF-8 output encoding on Windows consoles to prevent UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# NOTE ON PROTOCOL SOURCE:
# Confirmed against the real JP4 COM protocol manuals (891817107_COM_control.pdf)
# and verified live against real JP4 hardware (B0/N1/N2 all returned correctly
# structured, checksum-valid responses). One thing differs from a naive
# reading of the manual: this press's link runs at 38400 bps, not the
# documented 9600 default (confirmed empirically).
#
# The checksum covers address+command+subcommand+data for both requests and
# responses, matching the manual exactly ("from Address 1 to the end of the
# data"). An early single response sample appeared to match a variant that
# excludes the address instead, but that turned out to be a corrupted frame
# that coincidentally satisfied a different 8-bit sum by chance - this
# connection has been intermittently noisy, and further clean captures
# confirmed the documented (address-included) formula is correct. Treat a
# checksum mismatch as a connection-quality signal, not a formula bug.

START = b'$'      # 0x24 transfer start code
END = b'\r'        # 0x0D CR transfer end code
HOST_ADDR = "00"   # controller (PC) address
PRESS_ADDR = "01"  # press address

# Result code in a Quality Data (V1) frame; anything other than 0 is an NG cycle.
V1_RESULT_CODES = {
    0: "Normal", 3211: "Upper limit error", 3212: "Lower limit error", 3213: "Sensor out of range",
    3221: "Pressure exceeded", 3222: "Position exceeded", 3224: "Speed exceeded", 3225: "Time exceeded",
    3229: "Finished mid-operation", 3230: "Other error", 3232: "External signal stopped",
    3233: "Stop time exceeded",
}
BACKLOG_WINDOW_S = 3.0  # V1 frames arriving this soon after Q1 may be cycles that finished earlier


class PressTimeout(Exception):
    """The press did not answer in time (powered off, cable out, wrong port)."""


def decode_quality_frame(data):
    """
    Decodes the fixed header of a Quality Data (V1) frame, i.e. one finished press cycle.
    Layout verified against the manual's worked example (see serial_test_webapp.decode_v1).
    Returns a dict using the database_handler.CYCLE_FIELDS names, or None if the frame is too short.
    """
    if len(data) < 88:
        return None
    signed = JanomeJP4Press._hex_to_signed
    result_code = int(data[28:32], 16)
    return {
        "program_number": int(data[0:4], 16),
        "ok": result_code == 0,
        "result_code": result_code,
        "result_text": V1_RESULT_CODES.get(result_code, f"unknown code {result_code}"),
        "prep_time_s": round(int(data[40:48], 16) * 0.001, 3),
        "work_time_s": round(int(data[48:56], 16) * 0.001, 3),
        "contact_position_mm": round(signed(data[56:64]) * 0.0001, 4),
        "contact_load_n": round(signed(data[64:72]) * 0.1, 1),
        "end_position_mm": round(signed(data[72:80]) * 0.0001, 4),
        "end_load_n": round(signed(data[80:88]) * 0.1, 1),
        "ok_total": int(data[4:12], 16),
        "fault_total": int(data[12:20], 16),
        "lifetime_total": int(data[20:28], 16),
        "sensor_fault_section": int(data[32:36], 16),
        "sensor_fault_number": int(data[36:40], 16),
        # The undecoded frame: the whole data field and what follows the fixed 88-character header
        # (sensor sampling data of variable length, not decoded yet). The cycle handler drops both
        # before the cycle is logged or saved.
        "v1_raw": data,
        "v1_extra": data[88:],
    }


# Program settings readable with S2 (Data Request), from the item table of the COM manual:
# name -> (item number, unit, scale, section level). "head" items use pressing section 0.
S2_SETTINGS = {
    # Item numbers confirmed on a real press (program 5, values matched the press screen and varied
    # per program): 2006 pressing speed, 2005 start position, 2017 end position. The manual lists the
    # start position as 1510, but that item reads 0 on this press. The return speed is item 2030, one
    # of two head speed items (the other, 2002, is the approach speed); confirm against the screen of a
    # program whose approach and return speeds differ.
    "set_pressing_speed_mm_s": (2006, 0.001, "pressing"),
    "set_return_speed_mm_s": (2030, 0.001, "head"),
    "set_start_position_mm": (2005, 0.0001, "head"),
    "set_end_position_mm": (2017, 0.0001, "pressing"),
}
IO_TYPES = (("io_system_in", 0), ("io_general_in", 1), ("io_system_out", 2), ("io_general_out", 3))


class JanomeJP4Press:
    def __init__(self, port, baudrate=38400, timeout=2.0):
        """
        Initializes serial connection to a JANOME Electro Press (JP4).
        Link settings confirmed against real hardware: 8 data bits, no parity,
        1 stop bit. The baud rate is configured on the press itself (one unit
        runs at 38400 bps, another at the manual's 9600 default), so it comes from config.json.
        """
        self.port = port
        self.timeout = timeout
        self._rx = bytearray()   # bytes received but not yet split into frames
        self._cycles = []        # finished-cycle (V1) frames pushed by the press, waiting to be collected
        self._q1_sent_at = None
        self.ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout
        )
        time.sleep(1)  # Allow serial port to initialize
        logger.info(f"Connected to JANOME Electro Press (JP4) on {port} at {baudrate} bps.")

    def close(self):
        """Closes the serial connection."""
        if hasattr(self, 'ser') and self.ser and self.ser.is_open:
            try:
                self.ser.close()
            except Exception:
                pass
            logger.info("Serial connection closed.")

    def compute_checksum(self, body_str):
        """
        SUM error check code: the transfer content from address 1 to the end
        of the data, added together and truncated to the lowest byte,
        expressed as 2 uppercase hex ASCII characters.
        """
        total = 0
        for char in body_str:
            total = (total + ord(char)) & 0xFF
        return f"{total:02X}"

    def build_frame(self, command, subcommand, data=""):
        """
        Builds a COM command frame:
        $ + [Address 1-2] + [Command] + [Subcommand] + [Data] + [SUM] + CR
        """
        body = PRESS_ADDR + command + subcommand + data
        checksum = self.compute_checksum(body)
        frame = START + body.encode('ascii') + checksum.encode('ascii') + END
        return frame

    def parse_frame(self, raw):
        """Validates one raw '$...CR' frame (start code, CR, checksum) and splits it into fields."""
        if not raw.startswith(START):
            raise Exception(f"Protocol error: expected '$' start code, got: {raw!r}")
        if not raw.endswith(END):
            raise Exception(f"Protocol error: response not CR-terminated: {raw!r}")

        content = raw[1:-1].decode('ascii', errors='ignore')  # strip $ and CR
        if len(content) < 6:
            raise Exception(f"Response frame too short: '{content}'")

        body = content[:-2]
        recv_checksum = content[-2:].upper()
        calc_checksum = self.compute_checksum(body)
        if recv_checksum != calc_checksum:
            raise Exception(
                f"Checksum verification failed! Received: '{recv_checksum}', Expected: '{calc_checksum}'"
            )
        return {
            "address": body[0:2],
            "command": body[2:3],
            "subcommand": body[3:4],
            "data": body[4:],
        }

    def _split_frames(self):
        """Removes every complete CR-terminated frame from the receive buffer and returns them."""
        frames = []
        while END in self._rx:
            end_idx = self._rx.index(END)
            start_idx = self._rx.rfind(START, 0, end_idx)
            if start_idx != -1:
                frames.append(bytes(self._rx[start_idx:end_idx + 1]))
            del self._rx[:end_idx + 1]  # anything before the start code is line noise
        return frames

    def _stash_push(self, frame):
        """
        Keeps a pushed Quality Data frame (command 'V') for poll_cycles().
        Returns True if the frame was a pushed cycle result, False for any other frame.
        """
        if frame["command"] != "V":
            return False
        received_at = datetime.datetime.now(datetime.timezone.utc)
        backlog = self._q1_sent_at is not None and (time.monotonic() - self._q1_sent_at) < BACKLOG_WINDOW_S
        cycle = decode_quality_frame(frame["data"])
        if cycle is None:
            logger.warning(f"Ignoring a quality-data frame shorter than expected ({len(frame['data'])} characters).")
            return True
        cycle["backlog"] = backlog
        self._cycles.append((received_at, cycle))
        return True

    def _absorb(self):
        """Reads whatever is waiting on the port and files pushed cycle frames; stale replies are dropped."""
        waiting = self.ser.in_waiting
        if waiting:
            self._rx.extend(self.ser.read(waiting))
        for raw in self._split_frames():
            try:
                self._stash_push(self.parse_frame(raw))
            except Exception as err:
                logger.debug(f"Discarded an unreadable frame: {err}")

    def poll_cycles(self):
        """Returns [(received_at, cycle_dict), ...] for every cycle the press finished since the last call."""
        self._absorb()
        cycles, self._cycles = self._cycles, []
        return cycles

    def enable_quality_data(self):
        """Asks the press to push a result frame after every cycle (Q1). Does not move the press."""
        self._absorb()
        self._q1_sent_at = time.monotonic()
        self.ser.write(self.build_frame('Q', '1'))

    def disable_quality_data(self):
        """Stops the push of cycle results (Q0); best effort, used when ingestion stops."""
        try:
            self.ser.write(self.build_frame('Q', '0'))
        except Exception:
            pass

    def send_command(self, command, subcommand, data=""):
        """
        Sends a COM command frame and reads back its CR-terminated reply. Validates the
        start code and checksum, and raises on an 'e' (communication error) response.
        Cycle results the press pushes in the meantime are kept, not mistaken for the reply.
        """
        self._absorb()
        # What is left is the start of a frame still arriving; keep it only if it is a pushed cycle result
        if not (self._rx[:1] == START and self._rx[3:4] == b'V'):
            self._rx.clear()
        self.ser.write(self.build_frame(command, subcommand, data))

        expected = command.lower()
        deadline = time.monotonic() + max(self.timeout, 0.1) * 2
        failure = None
        while True:
            chunk = self.ser.read_until(END)
            if not chunk:
                if failure:
                    raise failure
                raise PressTimeout(
                    f"Timeout waiting for response to command '{command}{subcommand}'. "
                    f"Check that the press is powered on and the RS-232 cable is connected."
                )
            self._rx.extend(chunk)
            for raw in self._split_frames():
                try:
                    frame = self.parse_frame(raw)
                except Exception as err:
                    failure = err
                    continue
                if self._stash_push(frame):
                    continue
                if frame["command"] == 'e':
                    error_meanings = {
                        '0': "Other Communication Error (parity/overrun/frame error or RX buffer overflow)",
                        '1': "Receive Timeout",
                        '2': "Command and Sub Command Code Error",
                        '4': "Check Sum Error",
                    }
                    code = frame["subcommand"]
                    meaning = error_meanings.get(code, f"Unknown error code ({code})")
                    raise Exception(f"Press returned communication error '{code}': {meaning}")
                if frame["command"].lower() == expected:
                    return frame
                # anything else (e.g. the 'q1' acknowledgement) is not our reply
            if time.monotonic() > deadline:
                raise failure or PressTimeout(f"No usable reply to command '{command}{subcommand}'.")

    @staticmethod
    def _hex_to_signed(hex_str, bits=32):
        """Converts a hex ASCII string to a signed integer (two's complement)."""
        val = int(hex_str, 16)
        if val >= (1 << (bits - 1)):
            val -= (1 << bits)
        return val

    def read_encoder_position(self):
        """Reads the actual ram position from the motor encoder (N1), in mm."""
        resp = self.send_command('N', '1')
        raw = self._hex_to_signed(resp["data"])
        return round(raw / 10000.0, 4)  # 0.0001 mm units

    def read_teaching_position(self):
        """Reads the currently loaded teaching (programmed) position (N0), in mm."""
        resp = self.send_command('N', '0')
        raw = self._hex_to_signed(resp["data"])
        return round(raw / 10000.0, 4)  # 0.0001 mm units

    def read_current_load(self):
        """Reads the current load on the ram (N2), in Newtons."""
        resp = self.send_command('N', '2')
        raw = self._hex_to_signed(resp["data"])
        return round(raw / 10.0, 1)  # 0.1 N units

    def read_system_info(self):
        """Reads static system information from the press (B0). Best-effort/informational."""
        resp = self.send_command('B', '0')
        data = resp["data"]
        info = {"raw": data}
        try:
            press_type_map = {"2": "JPF"}
            info["press_type"] = press_type_map.get(data[0:1], data[0:1])
        except Exception:
            pass
        if len(data) >= 30:  # field layout as decoded by serial_test_webapp.decode_b0
            info["model_type"] = {"0": "Stand-Alone/Head Type", "1": "Unit Type"}.get(data[0:2], data[0:2])
            info["load_spec_code"] = data[4:6]
            info["ram_length_code"] = data[8:10]
            info["sw_version"] = f"Ver. {int(data[16:20], 16) / 100:.2f}"
            info["custom_number"] = data[24:28]
            info["teaching_version"] = data[28:30]
        return info

    def read_io_status(self, io_type):
        """
        Reads the ON/OFF bit map of one I/O type with K0 (0 system in, 1 general in, 2 system out,
        3 general out). Returns the bit map as the press sends it, as hex text.
        """
        resp = self.send_command('K', '0', f"{io_type:04X}")
        return resp["data"][4:].upper()

    def read_program_settings(self, program, pressing_section=1):
        """
        Reads the settings of a program with S2 (Data Request): pressing speed, start position, end
        position and end load of the given pressing section. Returns {name: value}; a value the press
        refuses (FFFF) is None. Stops at the first communication failure (the exception is raised).
        """
        values = {}
        for name, (item, scale, level) in S2_SETTINGS.items():
            section = 0 if level == "head" else pressing_section
            resp = self.send_command('S', '2', f"{program:04X}{section:04X}{0:04X}{item:04X}")
            data = resp["data"]
            if data[0:4].upper() == "FFFF":
                values[name] = None
            else:
                values[name] = round(self._hex_to_signed(data[4:12]) * scale, 4)
        return values

    def read_telemetry(self):
        """
        Polls the press for its current encoder position, teaching position,
        and load. Mirrors the Musashi driver's read_pressure() shape so the
        ingestion loop / DB handler / web panel can follow the same pattern.
        """
        encoder_mm = self.read_encoder_position()
        teaching_mm = self.read_teaching_position()
        load_n = self.read_current_load()
        return {
            "encoder_position_mm": encoder_mm,
            "teaching_position_mm": teaching_mm,
            "load_n": load_n,
            "raw_payload": f"N1={encoder_mm}mm N0={teaching_mm}mm N2={load_n}N",
        }


class MockJanomeJP4Press:
    """Mock/Synthetic JP4 press for driver-free & offline hardware testing."""
    CYCLE_EVERY_S = 12.0  # one synthetic finished cycle this often; every 5th one is NG

    def __init__(self, port="MOCK", baudrate=38400, timeout=2.0):
        self.port = port
        self.ser = None
        self._next_cycle_at = time.monotonic() + self.CYCLE_EVERY_S
        self._cycle_no = 0
        logger.info("Connected to MOCK JANOME Electro Press JP4 (Synthetic Simulation Mode).")

    def close(self):
        logger.info("Mock serial connection closed.")

    def enable_quality_data(self):
        pass

    def disable_quality_data(self):
        pass

    def poll_cycles(self):
        if time.monotonic() < self._next_cycle_at:
            return []
        import random
        self._next_cycle_at += self.CYCLE_EVERY_S
        self._cycle_no += 1
        ng = self._cycle_no % 5 == 0
        end_pos = round(random.uniform(60.0, 80.0), 4)
        cycle = {
            "program_number": 1,
            "ok": not ng,
            "result_code": 3225 if ng else 0,
            "result_text": V1_RESULT_CODES[3225 if ng else 0],
            "prep_time_s": round(random.uniform(0.2, 0.4), 3),
            "work_time_s": round(random.uniform(1.0, 1.6), 3),
            "contact_position_mm": round(end_pos - random.uniform(0.5, 1.5), 4),
            "contact_load_n": round(random.uniform(100.0, 200.0), 1),
            "end_position_mm": end_pos,
            "end_load_n": round(random.uniform(600.0, 700.0), 1),
            "ok_total": self._cycle_no - self._cycle_no // 5,
            "fault_total": self._cycle_no // 5,
            "lifetime_total": 1000 + self._cycle_no,
            "sensor_fault_section": 0,
            "sensor_fault_number": 0,
            "backlog": False,
            "v1_raw": "MOCK" + "0" * 84,
            "v1_extra": "",
        }
        return [(datetime.datetime.now(datetime.timezone.utc), cycle)]

    def read_system_info(self):
        return {"raw": "MOCK", "press_type": "MOCK", "sw_version": "Ver. 0.00"}

    def read_io_status(self, io_type):
        return f"{0x10 + io_type:02X}0000"

    def read_program_settings(self, program, pressing_section=1):
        return {"set_pressing_speed_mm_s": 100.0, "set_return_speed_mm_s": 80.0,
                "set_start_position_mm": 29.0, "set_end_position_mm": 37.814}

    def read_telemetry(self):
        import random
        encoder_mm = round(random.uniform(0.0, 80.0), 4)
        teaching_mm = round(encoder_mm + random.uniform(-0.05, 0.05), 4)
        load_n = round(random.uniform(400.0, 600.0), 1)
        return {
            "encoder_position_mm": encoder_mm,
            "teaching_position_mm": teaching_mm,
            "load_n": load_n,
            "raw_payload": f"MOCK N1={encoder_mm}mm N0={teaching_mm}mm N2={load_n}N",
        }


def resolve_serial_port(requested_port):
    """
    Resolves serial port path for cross-platform compatibility (Windows vs macOS/Linux).
    If running on Windows and the configured port is a POSIX path (/dev/...) or unavailable,
    automatically detects active COM ports.
    """
    try:
        available_ports = [p.device for p in serial.tools.list_ports.comports()]
    except Exception:
        available_ports = []

    if sys.platform == "win32" or os.name == "nt":
        if requested_port.startswith("/dev/") or (available_ports and requested_port not in available_ports):
            if available_ports:
                chosen = available_ports[0]
                logger.warning(
                    f"Configured port '{requested_port}' is invalid on Windows. "
                    f"Auto-selected detected port '{chosen}' (Available: {available_ports})"
                )
                return chosen
            else:
                logger.warning(
                    f"Configured port '{requested_port}' is invalid on Windows and no COM ports detected. "
                    f"Defaulting to 'COM1'."
                )
                return "COM1"
    else:
        if requested_port.startswith("COM"):
            if available_ports:
                chosen = available_ports[0]
                logger.warning(
                    f"Configured Windows port '{requested_port}' is invalid on {sys.platform}. "
                    f"Auto-selected detected port '{chosen}' (Available: {available_ports})"
                )
                return chosen

    return requested_port


def load_config(config_path="config.json"):
    """Loads configuration from JSON file or returns default parameters."""
    default_port = "COM1" if sys.platform == "win32" else "/dev/cu.usbserial-jp4"
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    logger.warning(f"Config file '{config_path}' not found. Falling back to default settings.")
    return {
        "serial": {
            "port": default_port,
            "baudrate": 38400,
            "timeout": 2.0
        },
        "database": {
            "db_type": "influxdb",
            "db_name": "janome_jp4_data.db",
            "table_name": "janome_jp4_telemetry",
            "influx_url": "http://localhost:8086",
            "influx_version": "2",
            "influx_token": "janome-local-dev-token",
            "influx_org": "janome",
            "influx_bucket": "janome_jp4",
            "influx_measurement": "janome_jp4_telemetry",
            "influx_cycle_measurement": "janome_jp4_telemetry_cycles",
            "influx_retention_days": 30,
            "description": "Database storage for JANOME Electro Press JP4 telemetry data"
        },
        "acquisition": {
            "interval_time": 1.0
        }
    }


class PressSession:
    """
    Keeps the link to the press alive: opens the serial port, and when the cable is pulled,
    the adapter disappears or the press is powered off and on again, it keeps trying until
    the press answers, then turns cycle-result streaming (Q1) back on. It never gives up.

    Link states, each reported once as a '[LINK] state=...' line when it changes:
      up               the press answers
      no_response      the port is open but the press does not answer (powered off, cable)
      port_unavailable the serial port cannot be opened or was lost (adapter unplugged)
    """

    FAILS_BEFORE_DOWN = 3  # consecutive failed polls before the link is reported as no_response
    REOPEN_EVERY = 5       # failed polls after which the port is closed and opened again

    def __init__(self, open_press, reconnect_interval=5.0, label=""):
        self.open_press = open_press  # returns an opened press object, raises if the port cannot be opened
        self.reconnect_interval = reconnect_interval
        self.label = label  # port name, for the event log
        self.press = None
        self.state = None
        self.fail_streak = 0
        self.next_open_at = 0.0
        self.down_since = None  # when the link first went wrong (monotonic), until it is up again

    def _set_state(self, state, detail=""):
        if state == self.state:
            return
        previous = self.state
        self.state = state
        print(f"[LINK] state={state} detail={detail}")
        sys.stdout.flush()
        self._report(previous, state, detail)

    def _report(self, previous, state, detail):
        """One event-log line per link change (fail cases 1 and 3)."""
        port = self.label or "serial port"
        if state == "up":
            if previous in ("port_unavailable", "no_response") and self.down_since is not None:
                down = event_log.describe_duration(time.monotonic() - self.down_since)
                if previous == "port_unavailable":
                    event_log.emit("RS232_RECOVERED", f"RS-232 port {port} is back and the press answers; reading continues "
                                   f"automatically (link was down {down}).", port=self.label or None, down_for=down)
                else:
                    event_log.emit("PRESS_RECOVERED", f"The press answers again on {port} (power back on or cable "
                                   f"reconnected); reading and cycle results continue automatically (link was down {down}).",
                                   port=self.label or None, down_for=down)
            else:
                event_log.emit("PRESS_CONNECTED", f"Connected to the press on {port}.", port=self.label or None)
            self.down_since = None
            return
        if self.down_since is None:
            self.down_since = time.monotonic()
        if state == "port_unavailable":
            event_log.emit("RS232_LOST", f"RS-232 port {port} is not available (adapter unplugged or port busy). "
                           f"Auto-reconnect: opening it again every {self.reconnect_interval:g} s.",
                           level="ERROR", port=self.label or None, reason=detail)
        elif state == "no_response":
            event_log.emit("PRESS_NO_RESPONSE", f"The press does not answer on {port}: powered off, RS-232 cable pulled "
                           f"or wrong baud rate. Auto-reconnect: still polling; reading resumes by itself.",
                           level="ERROR", port=self.label or None, reason=detail)

    def _drop_port(self):
        if self.press is not None:
            try:
                self.press.close()
            except Exception:
                pass
            self.press = None
        self.next_open_at = time.monotonic() + self.reconnect_interval

    def _port_lost(self, err):
        logger.error(f"Serial port error: {err}")
        self._drop_port()
        self._set_state("port_unavailable", str(err))

    def poll(self):
        """One telemetry read. Returns the data, or None while the link is down (the reason is logged)."""
        if self.press is None:
            if time.monotonic() < self.next_open_at:
                return None
            try:
                self.press = self.open_press()
            except Exception as err:
                self.next_open_at = time.monotonic() + self.reconnect_interval
                logger.error(f"Cannot open the serial port: {err}. Retrying every {self.reconnect_interval:g}s.")
                self._set_state("port_unavailable", f"cannot open the serial port: {err}")
                return None
            self.fail_streak = 0

        try:
            data = self.press.read_telemetry()
        except (serial.SerialException, OSError) as err:
            self._port_lost(err)
            return None
        except Exception as err:
            self.fail_streak += 1
            logger.error(f"Error reading telemetry from press: {err}")
            if self.fail_streak >= self.FAILS_BEFORE_DOWN:
                self._set_state("no_response", str(err))
            if self.fail_streak % self.REOPEN_EVERY == 0:
                logger.warning("Press still silent; closing and reopening the serial port.")
                self._drop_port()
            return None

        self.fail_streak = 0
        if self.state != "up":
            self._set_state("up", "press is answering")
            try:
                # A press that was powered off forgets the request, so ask again on every recovery
                self.press.enable_quality_data()
            except Exception as err:
                logger.warning(f"Could not enable cycle result streaming (Q1): {err}")
        return data

    def poll_cycles(self):
        """Cycles the press finished since the last call; [] while the link is down."""
        if self.press is None or self.state != "up":
            return []
        try:
            return self.press.poll_cycles()
        except (serial.SerialException, OSError) as err:
            self._port_lost(err)
            return []

    def close(self):
        if self.press is not None:
            if self.state == "up":
                self.press.disable_quality_data()
            self.press.close()
            self.press = None


def describe_db(db_cfg, db_type):
    """Short text for logs: where the data goes."""
    if db_type == "influxdb":
        return f"InfluxDB at {db_cfg.get('influx_url') or 'http://localhost:8086'} ('{db_cfg.get('influx_bucket') or db_cfg.get('influx_database')}')"
    if db_type == "sqlite":
        return f"SQLite file '{db_cfg.get('sqlite_path') or db_cfg.get('db_name', 'janome_jp4_data.db')}'"
    return f"{db_type} database '{db_cfg.get('db_name')}' at {db_cfg.get('host')}:{db_cfg.get('port')}"


STOP_RESULT_CODES = (3229, 3232, 3233)  # the cycle was stopped before it finished (stop signal / emergency stop)


class IdleWatch:
    """
    Tells the event log when the press is on and answering but makes no stroke: PRESS_IDLE once after
    `after_s` seconds without a finished cycle (counted from the link coming up or the last cycle),
    PRESS_WORKING when the next cycle arrives. 0 turns it off. A broken link is not idle: it has its own events.
    """

    def __init__(self, after_s, label="", clock=time.monotonic):
        self.after_s = after_s
        self.label = label or None
        self.clock = clock
        self.since = None      # start of the current quiet period
        self.reported = False

    def cycles_seen(self, count):
        if not count:
            return
        if self.reported and self.since is not None:
            event_log.emit("PRESS_WORKING", f"The press made a stroke again after being idle for "
                           f"{event_log.describe_duration(self.clock() - self.since)}.", port=self.label)
        self.since = self.clock()
        self.reported = False

    def check(self, link_up):
        if not self.after_s or self.after_s <= 0:
            return
        if not link_up:
            self.since, self.reported = None, False
            return
        now = self.clock()
        if self.since is None:
            self.since = now
        elif not self.reported and now - self.since >= self.after_s:
            self.reported = True
            event_log.emit("PRESS_IDLE", f"The press is on and answering but no stroke for {event_log.describe_duration(now - self.since)} "
                           f"(no job running). Nothing is lost; cycles are recorded as soon as it works.",
                           port=self.label, idle_s=int(now - self.since))


def make_cycle_handler(writer, keep_local=None, last_total=None):
    """
    Returns a function that logs finished cycles ([CYCLE] line, NG events), queues them for
    the database and, through keep_local(kind, when, data), saves them in the local file and CSV.
    last_total is the press cycle counter (lifetime_total) of the last saved cycle, if known:
    every result is checked against it so a missing cycle shows up as a CYCLE_GAP event.
    """
    last_time = [None]
    counter = [last_total]

    def check_counter(cycle):
        total = cycle.get("lifetime_total")
        if total is None:
            return
        before = counter[0]
        counter[0] = total
        if before is None:
            return
        if total > before + 1:
            event_log.emit("CYCLE_GAP", f"{total - before - 1} stamping cycle(s) are missing: the press counter went from "
                           f"{before} to {total}. They happened while this program was not reading (PC off, link down "
                           f"longer than the press keeps results).", level="WARNING",
                           missing=total - before - 1, counter_from=before, counter_to=total)
        elif total <= before:
            event_log.emit("CYCLE_COUNTER_RESET", f"The press cycle counter went from {before} to {total} "
                           f"(counter reset, another press, or a repeated result).", level="WARNING",
                           counter_from=before, counter_to=total)

    def handle(cycles):
        for received_at, cycle in cycles:
            if last_time[0] is not None and received_at <= last_time[0]:
                # a burst of queued frames must not share one timestamp (InfluxDB would overwrite)
                received_at = last_time[0] + datetime.timedelta(microseconds=1)
            last_time[0] = received_at
            # the undecoded frame is not part of the cycle record (log line, database, local file, CSV)
            for key in ("v1_raw", "v1_extra"):
                cycle.pop(key, None)
            print(f"[CYCLE] {json.dumps(dict(cycle, time=received_at.isoformat()))}")
            check_counter(cycle)
            if not cycle["ok"]:
                logger.warning(
                    f"[NG] Cycle result {cycle['result_code']} ({cycle['result_text']}): "
                    f"end {cycle['end_position_mm']} mm / {cycle['end_load_n']} N, "
                    f"{cycle['fault_total']} fault cycle(s) so far."
                )
                stopped = cycle["result_code"] in STOP_RESULT_CODES
                event_log.emit(
                    "PRESS_NG",
                    f"Press reported NG: result {cycle['result_code']} ({cycle['result_text']})"
                    + (", the stroke was stopped before it finished (emergency stop / stop signal)" if stopped else "")
                    + ". The NG record is saved to the local file, CSV and the database.",
                    level="WARNING", case="NG STOP" if stopped else None,
                    program=cycle.get("program_number"), result_code=cycle["result_code"],
                    end_position_mm=cycle.get("end_position_mm"), end_load_n=cycle.get("end_load_n"),
                    fault_total=cycle.get("fault_total"), backlog=1 if cycle.get("backlog") else None,
                )
            if keep_local:
                keep_local("cycle", received_at, cycle)
            if writer:
                writer.submit(dict(cycle, _kind="cycle"), received_at)
        if cycles:
            sys.stdout.flush()

    return handle


def run_ingestion_loop(config_path="config.json", max_iterations=None, override_interval=None, override_port=None,
                       mock_mode=False, tolerate_db_down=False, stop_event=None, run_id=None):
    """
    Main loop that continuously reads position/load telemetry and finished-cycle results from
    the JANOME JP4 press and saves them to the database. The press link is kept alive
    automatically (see PressSession).

    tolerate_db_down: start even if the database cannot be reached and spool everything until it
    is back (used for automatic restarts, where nobody is there to fix the database setting).
    stop_event: optional threading.Event; setting it ends the loop cleanly (used by tests).
    run_id: the run (in the local file) this process adds to; the panel creates it. Without one the
    reader starts and ends its own run.

    Everything read is also saved in the local SQLite file (see local_store.py), whether or not a
    database is configured, so the data is on this computer even while the database server is down.
    """
    config = load_config(config_path)
    event_log.configure(event_log.default_path(config_path))

    serial_cfg = config.get("serial", {})
    db_cfg = config.get("database", {})
    acq_cfg = config.get("acquisition", {})

    raw_port = override_port if override_port is not None else serial_cfg.get("port", "COM1" if sys.platform == "win32" else "/dev/cu.usbserial-jp4")
    port = "MOCK" if mock_mode else resolve_serial_port(raw_port)
    baudrate = serial_cfg.get("baudrate", 38400)
    timeout = serial_cfg.get("timeout", 2.0)

    interval_time = override_interval if override_interval is not None else acq_cfg.get("interval_time", 1.0)
    reconnect_interval = float(acq_cfg.get("reconnect_interval", 5.0))

    print("=" * 65)
    print("      JANOME Electro Press JP4 Telemetry Ingestion Service      ")
    print("=" * 65)
    db_type = str(db_cfg.get("db_type") or "none").lower()
    log_only = db_type == "none"

    print(f"  Mode:          {'MOCK (Synthetic Hardware Simulation)' if mock_mode else 'REAL (Physical Hardware RS-232)'}")
    print(f"  Serial Port:   {port} @ {baudrate} bps")
    print(f"  Interval Time: {interval_time} seconds")
    local_cfg = config.get("local_store", {})
    local_enabled = local_cfg.get("enabled", True)
    local_path = local_store.resolve_path(local_cfg, config_path)
    print(f"  Local file:    {local_path if local_enabled else 'OFF (nothing is kept on this computer)'}")
    csv_cfg = config.get("csv_export", {})
    csv_enabled = csv_cfg.get("enabled", True)
    csv_mode = str(csv_cfg.get("mode") or "run").lower()
    if csv_mode not in csv_export.MODES:
        logger.warning(f"[CSV] Unknown csv_export mode '{csv_mode}'; using 'run'.")
        csv_mode = "run"
    csv_folder = csv_export.resolve_folder(csv_cfg, config_path)
    try:
        csv_day_start = csv_export.parse_day_start(csv_cfg.get("day_start"))
    except (ValueError, TypeError) as err:
        logger.warning(f"[CSV] Bad csv_export day_start ({err}); the day changes at 00:00.")
        csv_day_start = (0, 0)
    csv_day_start_text = f"{csv_day_start[0]:02d}:{csv_day_start[1]:02d}"
    # Target of the cycle report (not stored in the press): the end load setting in kg
    csv_targets = {"end_load_setting_kgf": csv_cfg.get("end_load_setting_kgf", 250)}
    if csv_enabled:
        print(f"  Cycle CSV:     {csv_folder} (one file per "
              f"{'Start-Stop run' if csv_mode == 'run' else 'day, the day changes at ' + csv_day_start_text})")
    else:
        print("  Cycle CSV:     OFF")
    if log_only:
        print("  Database:      NONE (data is kept in the local file only)")
    else:
        print(f"  Database:      {describe_db(db_cfg, db_type)}")
        if db_type == "influxdb":
            print(f"  Measurements:  {db_cfg.get('influx_measurement') or db_cfg.get('table_name', 'janome_jp4_telemetry')} (telemetry), "
                  f"{db_cfg.get('influx_cycle_measurement') or (db_cfg.get('table_name', 'janome_jp4_telemetry') + '_cycles')} (cycles)")
        else:
            print(f"  Tables:        {db_cfg.get('table_name', 'janome_jp4_telemetry')} (telemetry), "
                  f"{db_cfg.get('cycles_table') or (db_cfg.get('table_name', 'janome_jp4_telemetry') + '_cycles')} (cycles)")
    print("=" * 65)

    session = None
    writer = None
    local = None
    own_run = False

    try:
        # Check the database first so a bad DB setting fails fast, before the serial port is opened
        if not log_only:
            spool_path = db_cfg.get("spool_path") or os.path.join(
                os.path.dirname(os.path.abspath(config_path)), "janome_jp4_spool.db"
            )
            spool = DiskSpool(spool_path, max_rows=int(db_cfg.get("max_spool_rows", 1000000)))
            db_connected = True
            try:
                db_handler = DatabaseHandler(db_cfg)
            except Exception as err:
                where = describe_db(db_cfg, db_type)
                if spool.count == 0 and not tolerate_db_down:
                    logger.error(f"[DB ERROR] Cannot connect to {where}: {err}")
                    logger.error("[DB ERROR] Ingestion not started. Fix the database settings or set the database to 'This computer only'.")
                    event_log.emit(classify_db_error(err), f"Cannot connect to {where}: {err}. Reading was not started; fix the "
                                   f"database setting (or use 'This computer only') and press Start.", level="ERROR")
                    spool.close()
                    return 2
                # Keep collecting and send everything once the DB is back (earlier samples on disk, or an automatic restart)
                logger.warning(f"[DB] Cannot connect to {where}: {err}")
                if spool.count:
                    logger.warning(f"[DB] {spool.count} sample(s) from an earlier run are waiting in '{spool_path}'.")
                logger.warning("[DB] Starting anyway; samples are spooled to disk until the database is reachable.")
                event_log.emit(classify_db_error(err), f"Cannot connect to {where}: {err}. Reading starts anyway; data is "
                               f"stored at the edge (local file + disk spool) and sent when the database is back.",
                               level="ERROR")
                db_handler = DatabaseHandler(db_cfg, connect=False)
                db_connected = False
            else:
                if spool.count:
                    logger.info(f"[DB] Sending {spool.count} sample(s) left in '{spool_path}' by an earlier run.")
            writer = BufferedDatabaseWriter(
                db_handler,
                spool,
                max_buffer=int(db_cfg.get("max_buffer", 100000)),
                retry_interval=float(db_cfg.get("retry_interval", 5.0)),
                connected=db_connected,
            )

        if local_enabled:
            try:
                local = local_store.LocalStore(local_path, max_rows=int(local_cfg.get("max_rows", 2000000)))
                if run_id is None:
                    run_id = local.start_run("mock" if mock_mode else "real")
                    own_run = True
            except Exception as err:
                local = None
                logger.warning(f"[LOCAL] Cannot open the local file '{local_path}': {err}. Continuing without it.")
        local_failures = [0]

        run_started = None
        if local is not None and run_id is not None:
            try:
                row = local.get_run(run_id)
                if row:
                    run_started = datetime.datetime.fromisoformat(row["started_at"])
            except Exception:
                run_started = None
        exporter = None
        if csv_enabled:
            try:
                exporter = csv_export.CsvExporter(csv_folder, csv_mode, run_started=run_started, run_id=run_id,
                                              day_start=csv_day_start_text)
            except Exception as err:
                logger.warning(f"[CSV] Cannot use the folder '{csv_folder}': {err}. Continuing without CSV files.")
                exporter = None
        csv_failures = [0]
        settings_state = {"cache": {}, "failures": 0}

        def note_extra_failure(what, err):
            """Reading the program settings for the CSV is best effort: log the first problems, never stop."""
            settings_state["failures"] += 1
            if settings_state["failures"] in (1, 100, 10000):
                logger.warning(f"[CSV] Extra read failed: {what} ({err}); {settings_state['failures']} time(s) so far.")

        def settings_for(program):
            """Program settings (S2), cached per program so a burst of cycles does not read them again."""
            now = time.monotonic()
            cached = settings_state["cache"].get(program)
            if cached and now < cached[0]:
                return cached[1]
            press = session.press if session else None
            values = {}
            if press is not None:
                try:
                    values = press.read_program_settings(program)
                except Exception as err:
                    note_extra_failure("reading the program settings", err)
            settings_state["cache"][program] = (now + (300 if values else 30), values)
            return values

        def keep_csv(when, cycle):
            """Appends one finished cycle to the CSV file; a problem here never stops the collection."""
            if exporter is None:
                return
            try:
                exporter.add_cycle(when, cycle, settings_for(cycle.get("program_number")), csv_targets)
            except Exception as err:
                csv_failures[0] += 1
                if csv_failures[0] in (1, 100, 10000):
                    logger.warning(f"[CSV] Could not write the CSV file ({err}); {csv_failures[0]} failure(s) so far.")

        def keep_local(kind, when, data):
            """Saves one reading or cycle in the local file and CSV; a problem here never stops the collection."""
            if kind == "cycle":
                keep_csv(when, data)
            if local is None:
                return
            try:
                if kind == "cycle":
                    local.add_cycle(run_id, when, data)
                else:
                    local.add_sample(run_id, when, data)
            except Exception as err:
                local_failures[0] += 1
                if local_failures[0] in (1, 100, 10000):
                    logger.warning(f"[LOCAL] Could not save to the local file ({err}); {local_failures[0]} failure(s) so far.")

        if mock_mode:
            session = PressSession(lambda: MockJanomeJP4Press(port="MOCK", baudrate=baudrate, timeout=timeout), reconnect_interval, label="MOCK")
        else:
            session = PressSession(lambda: JanomeJP4Press(port=port, baudrate=baudrate, timeout=timeout), reconnect_interval, label=port)
        last_total = None
        if local is not None:
            try:
                last_total = local.last_lifetime_total("mock" if mock_mode else "real")
            except Exception as err:
                logger.warning(f"[LOCAL] Could not read the last cycle counter: {err}")
        base_handle_cycles = make_cycle_handler(writer, keep_local, last_total)
        try:
            idle_after_s = float((config.get("event_log") or {}).get("idle_alert_s", 60))
        except (TypeError, ValueError):
            logger.warning("[EVENT] Bad event_log idle_alert_s; using 60 s.")
            idle_after_s = 10.0
        idle_watch = IdleWatch(idle_after_s, label=session.label)

        def handle_cycles(cycles):
            idle_watch.cycles_seen(len(cycles))
            base_handle_cycles(cycles)
            idle_watch.check(session.press is not None and session.state == "up")

        event_log.emit("READER_START",
                       f"Reader started ({'automatic start, no operator' if tolerate_db_down else 'manual start'}): "
                       f"{'MOCK simulation' if mock_mode else 'press on ' + str(port) + ' @ ' + str(baudrate) + ' bps'}; "
                       f"data goes to {'the local file only' if log_only else describe_db(db_cfg, db_type)}.",
                       run_id=run_id, mode="mock" if mock_mode else "real")

        iteration = 0
        print(f"\nStarting data collection loop (interval: {interval_time}s). Press Ctrl+C to stop.\n")

        while not (stop_event and stop_event.is_set()):
            iteration += 1
            read_at = datetime.datetime.now(datetime.timezone.utc)
            timestamp_str = read_at.strftime("%Y-%m-%d %H:%M:%S %Z")
            if session.press is not None:
                print(f"[{timestamp_str}] [Loop #{iteration}] Polling press telemetry...")

            data = session.poll()
            if data is None:
                sys.stdout.flush()
                if session.press is not None and not mock_mode and session.fail_streak == 1:
                    logger.warning(
                        f"[HINT] No/invalid response received from serial port '{port}'.\n"
                        f"       1. Ensure the press is powered ON and the RS-232 cable is connected.\n"
                        f"       2. Verify the port name (e.g., --port COM3 or --port COM4).\n"
                        f"       3. The link is retried automatically; nothing needs to be restarted.\n"
                        f"       4. To run in driver-free simulation mode, use: python read_janome_jp4.py --mock"
                    )
            else:
                keep_local("sample", read_at, data)
                if writer:
                    writer.submit(data, read_at)
                    db_state = "connected" if writer.connected else "down"
                    if writer.connected:
                        print("  -> Success! Sample read; queued for database.")
                    else:
                        print(f"  -> Success! Sample read; DATABASE DOWN - {writer.pending} sample(s) spooled to disk, retrying.")
                else:
                    db_state = "none"
                    print("  -> Success! Sample read (kept in the local file only).")
                print(f"     Encoder Position: {data['encoder_position_mm']} mm | Teaching Position: {data['teaching_position_mm']} mm | Load: {data['load_n']} N")
                written = writer.written if writer else 0
                db_errors = writer.db_errors if writer else 0
                pending = writer.pending if writer else 0
                dropped = writer.dropped if writer else 0
                print(f"[STATS] polled={iteration} | written={written} | db_errors={db_errors} | encoder_position_mm={data['encoder_position_mm']} | teaching_position_mm={data['teaching_position_mm']} | load_n={data['load_n']} | db_state={db_state} | db_pending={pending} | db_dropped={dropped} | time={read_at.isoformat()}")
                sys.stdout.flush()
            handle_cycles(session.poll_cycles())

            if max_iterations is not None and iteration >= max_iterations:
                print(f"\nReached target iterations limit ({max_iterations}). Exiting loop.")
                break

            print(f"  -> Sleeping for {interval_time} seconds...\n")
            # Sleep in short slices so a finished cycle is time-stamped within ~0.2 s of when it arrives
            wake_at = time.monotonic() + interval_time
            while True:
                remaining = wake_at - time.monotonic()
                if remaining <= 0 or (stop_event and stop_event.is_set()):
                    break
                time.sleep(min(0.2, remaining))
                handle_cycles(session.poll_cycles())

    except KeyboardInterrupt:
        print("\n[STOP] Ingestion loop stopped by user (KeyboardInterrupt). Exiting...")
        event_log.emit("READER_STOP", "Reader stopped (Ctrl+C or stop request).", run_id=run_id)
    except Exception as err:
        logger.error(f"Service encountered an error: {err}")
        event_log.emit("READER_ERROR", f"CRASH: the reader stopped because of an error: {err}. The panel restarts it "
                       f"automatically if the run should still be going.", level="ERROR", run_id=run_id)
        return 1
    finally:
        if session:
            session.close()
        if writer:
            writer.close()
        if local is not None:
            if own_run:
                try:
                    local.end_run(run_id)
                except Exception:
                    pass
            local.close()
        print("Service shutdown complete.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="JANOME Electro Press JP4 Data Ingestion and Database Service")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json file")
    parser.add_argument("--interval", type=float, default=None, help="Override interval time in seconds between iterations")
    parser.add_argument("--port", type=str, default=None, help="Override serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--mock", action="store_true", help="Run in mock/simulation mode without physical hardware")
    parser.add_argument("--once", action="store_true", help="Run once instead of infinite loop")
    parser.add_argument("--tolerate-db-down", action="store_true",
                        help="Start even if the database is unreachable and spool samples until it is back (automatic restarts)")

    parser.add_argument("--run-id", type=int, default=None,
                        help="Run (in the local file) to add to; given by the panel, so the panel and the reader agree on the run")

    args = parser.parse_args(argv)

    max_iter = 1 if args.once else None
    sys.exit(run_ingestion_loop(
        config_path=args.config,
        max_iterations=max_iter,
        override_interval=args.interval,
        override_port=args.port,
        mock_mode=args.mock,
        tolerate_db_down=args.tolerate_db_down,
        run_id=args.run_id,
    ))


if __name__ == "__main__":
    main()
