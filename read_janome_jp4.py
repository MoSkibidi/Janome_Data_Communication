import serial
import serial.tools.list_ports
import time
import json
import os
import sys
import argparse
import logging
import datetime
from database_handler import DatabaseHandler, BufferedDatabaseWriter, DiskSpool

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


class JanomeJP4Press:
    def __init__(self, port, baudrate=38400, timeout=2.0):
        """
        Initializes serial connection to a JANOME Electro Press (JP4).
        Link settings confirmed against real hardware: 38400 bps (this
        press's actual configured rate, not the manual's 9600 default),
        8 data bits, no parity, 1 stop bit.
        """
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
            self.ser.close()
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

    def send_command(self, command, subcommand, data=""):
        """
        Sends a COM command frame and reads back a single CR-terminated
        response frame. Validates the start code, checksum, and raises on
        an 'e' (communication error) response.
        """
        self.ser.reset_input_buffer()
        frame = self.build_frame(command, subcommand, data)
        self.ser.write(frame)

        raw = self.ser.read_until(END)
        if not raw:
            raise Exception(
                f"Timeout waiting for response to command '{command}{subcommand}'. "
                f"Check that the press is powered on and the RS-232 cable is connected."
            )
        if not raw.startswith(START):
            raise Exception(f"Protocol error: expected '$' start code, got: {raw!r}")
        if not raw.endswith(END):
            raise Exception(f"Protocol error: response not CR-terminated: {raw!r}")

        content = raw[1:-1].decode('ascii', errors='ignore')  # strip $ and CR
        if len(content) < 4:
            raise Exception(f"Response frame too short: '{content}'")

        body = content[:-2]
        recv_checksum = content[-2:].upper()
        calc_checksum = self.compute_checksum(body)
        if recv_checksum != calc_checksum:
            raise Exception(
                f"Checksum verification failed! Received: '{recv_checksum}', Expected: '{calc_checksum}'"
            )

        resp_addr = body[0:2]
        resp_cmd = body[2:3]
        resp_subcmd = body[3:4]
        resp_data = body[4:]

        if resp_cmd == 'e':
            error_meanings = {
                '0': "Other Communication Error (parity/overrun/frame error or RX buffer overflow)",
                '1': "Receive Timeout",
                '2': "Command and Sub Command Code Error",
                '4': "Check Sum Error",
            }
            meaning = error_meanings.get(resp_subcmd, f"Unknown error code ({resp_subcmd})")
            raise Exception(f"Press returned communication error '{resp_subcmd}': {meaning}")

        return {
            "address": resp_addr,
            "command": resp_cmd,
            "subcommand": resp_subcmd,
            "data": resp_data,
        }

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
        return info

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
    def __init__(self, port="MOCK", baudrate=38400, timeout=2.0):
        self.port = port
        self.ser = None
        logger.info("Connected to MOCK JANOME Electro Press JP4 (Synthetic Simulation Mode).")

    def close(self):
        logger.info("Mock serial connection closed.")

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
            "db_type": "sqlite",
            "db_name": "janome_jp4_data.db",
            "table_name": "janome_jp4_telemetry",
            "description": "Database storage for JANOME Electro Press JP4 telemetry data"
        },
        "acquisition": {
            "interval_time": 1.0,
            "max_retries": 3
        }
    }


def run_ingestion_loop(config_path="config.json", max_iterations=None, override_interval=None, override_port=None, mock_mode=False):
    """
    Main loop that continuously reads position/load telemetry from the
    JANOME JP4 press and saves it to the database at a configured interval.
    """
    config = load_config(config_path)

    serial_cfg = config.get("serial", {})
    db_cfg = config.get("database", {})
    acq_cfg = config.get("acquisition", {})

    raw_port = override_port if override_port is not None else serial_cfg.get("port", "COM1" if sys.platform == "win32" else "/dev/cu.usbserial-jp4")
    port = "MOCK" if mock_mode else resolve_serial_port(raw_port)
    baudrate = serial_cfg.get("baudrate", 38400)
    timeout = serial_cfg.get("timeout", 2.0)

    interval_time = override_interval if override_interval is not None else acq_cfg.get("interval_time", 1.0)

    print("=" * 65)
    print("      JANOME Electro Press JP4 Telemetry Ingestion Service      ")
    print("=" * 65)
    db_type = str(db_cfg.get("db_type") or "none").lower()
    log_only = db_type == "none"

    print(f"  Mode:          {'MOCK (Synthetic Hardware Simulation)' if mock_mode else 'REAL (Physical Hardware RS-232)'}")
    print(f"  Serial Port:   {port} @ {baudrate} bps")
    print(f"  Interval Time: {interval_time} seconds")
    if log_only:
        print("  Database:      NONE (log only - samples are not stored)")
    else:
        print(f"  DB Type:       {db_type}")
        print(f"  DB Name:       {db_cfg.get('db_name', 'janome_jp4_data.db')}")
        print(f"  Table Name:    {db_cfg.get('table_name', 'janome_jp4_telemetry')}")
    print("=" * 65)

    press = None
    writer = None

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
                where = f"{db_type} database '{db_cfg.get('db_name')}' at {db_cfg.get('host')}:{db_cfg.get('port')}"
                if spool.count == 0:
                    logger.error(f"[DB ERROR] Cannot connect to {where}: {err}")
                    logger.error("[DB ERROR] Ingestion not started. Fix the database settings or set the database to 'None (log only)'.")
                    spool.close()
                    return 2
                # Samples from an earlier run are still waiting on disk: keep collecting, send everything once the DB is back
                logger.warning(f"[DB] Cannot connect to {where}: {err}")
                logger.warning(f"[DB] {spool.count} sample(s) from an earlier run are waiting in '{spool_path}'. Starting anyway; samples are spooled until the database is reachable.")
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

        if mock_mode:
            press = MockJanomeJP4Press(port="MOCK", baudrate=baudrate, timeout=timeout)
        else:
            press = JanomeJP4Press(port=port, baudrate=baudrate, timeout=timeout)

        iteration = 0
        print(f"\nStarting data collection loop (interval: {interval_time}s). Press Ctrl+C to stop.\n")

        while True:
            iteration += 1
            read_at = datetime.datetime.now(datetime.timezone.utc)
            timestamp_str = read_at.strftime("%Y-%m-%d %H:%M:%S %Z")
            print(f"[{timestamp_str}] [Loop #{iteration}] Polling press telemetry...")

            try:
                data = press.read_telemetry()
            except Exception as err:
                logger.error(f"Error reading telemetry from press: {err}")
                sys.stdout.flush()
                if not mock_mode:
                    logger.warning(
                        f"[HINT] No/invalid response received from serial port '{port}'.\n"
                        f"       1. Ensure the press is powered ON and the RS-232 cable is connected.\n"
                        f"       2. Verify the port name (e.g., --port COM3 or --port COM4).\n"
                        f"       3. This driver assumes JP4 speaks the JP5 COM protocol - if frames\n"
                        f"          consistently fail, JP4 firmware may use a different protocol.\n"
                        f"       4. To run in driver-free simulation mode, use: python read_janome_jp4.py --mock"
                    )
            else:
                if writer:
                    writer.submit(data, read_at)
                    db_state = "connected" if writer.connected else "down"
                    if writer.connected:
                        print("  -> Success! Sample read; queued for database.")
                    else:
                        print(f"  -> Success! Sample read; DATABASE DOWN - {writer.pending} sample(s) spooled to disk, retrying.")
                else:
                    db_state = "none"
                    print("  -> Success! Sample read (log only, not stored).")
                print(f"     Encoder Position: {data['encoder_position_mm']} mm | Teaching Position: {data['teaching_position_mm']} mm | Load: {data['load_n']} N")
                written = writer.written if writer else 0
                db_errors = writer.db_errors if writer else 0
                pending = writer.pending if writer else 0
                dropped = writer.dropped if writer else 0
                print(f"[STATS] polled={iteration} | written={written} | db_errors={db_errors} | encoder_position_mm={data['encoder_position_mm']} | teaching_position_mm={data['teaching_position_mm']} | load_n={data['load_n']} | db_state={db_state} | db_pending={pending} | db_dropped={dropped}")
                sys.stdout.flush()

            if max_iterations is not None and iteration >= max_iterations:
                print(f"\nReached target iterations limit ({max_iterations}). Exiting loop.")
                break

            print(f"  -> Sleeping for {interval_time} seconds...\n")
            time.sleep(interval_time)

    except KeyboardInterrupt:
        print("\n[STOP] Ingestion loop stopped by user (KeyboardInterrupt). Exiting...")
    except Exception as err:
        logger.error(f"Service encountered an error: {err}")
        return 1
    finally:
        if press:
            press.close()
        if writer:
            writer.close()
        print("Service shutdown complete.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="JANOME Electro Press JP4 Data Ingestion and Database Service")
    parser.add_argument("--config", type=str, default="config.json", help="Path to config.json file")
    parser.add_argument("--interval", type=float, default=None, help="Override interval time in seconds between iterations")
    parser.add_argument("--port", type=str, default=None, help="Override serial port (e.g. COM3 or /dev/ttyUSB0)")
    parser.add_argument("--mock", action="store_true", help="Run in mock/simulation mode without physical hardware")
    parser.add_argument("--once", action="store_true", help="Run once instead of infinite loop")

    args = parser.parse_args()

    max_iter = 1 if args.once else None
    sys.exit(run_ingestion_loop(
        config_path=args.config,
        max_iterations=max_iter,
        override_interval=args.interval,
        override_port=args.port,
        mock_mode=args.mock
    ))
