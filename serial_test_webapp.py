"""
Standalone JP4 COM-protocol test/diagnostic web app. No database, no
eventlet, no ingestion loop - a form to pick a command (in plain language),
fill in any parameters, send it, and see both the raw bytes AND a
human-readable translation of the response.

Protocol and every command's byte layout below is taken directly from the
real JP4 manuals in "C:\\JP4 Command com" (891817107_COM_control.pdf).
Checksum algorithm cross-checked against the manual's own worked examples
(B0 -> D3, N0 -> DF, N2 -> E1) and matched exactly.

Commands are split into two groups:
  - "read"    : query-only, cannot move the press or change its state.
  - "control" : causes physical motion (ram movement, a full press cycle)
                or changes press state/output. Gated behind an explicit
                confirmation checkbox in the UI - never sent without it.

Some documented commands (K2/K3 IO output set/reset, S1 data settings, U0
data save) are deliberately NOT implemented here: their byte layout in the
extracted manual text was ambiguous/incomplete, and guessing at the
encoding for a WRITE command against real hardware is not something to do
without being sure.

Run:
    .venv\\Scripts\\python.exe serial_test_webapp.py

Then open http://localhost:8086
"""
import serial
import serial.tools.list_ports
import threading
import time
import datetime
from flask import Flask, jsonify, request

app = Flask(__name__)

START = b'$'
END = b'\r'
PRESS_ADDR = "01"


def checksum(body: str) -> str:
    total = 0
    for ch in body:
        total = (total + ord(ch)) & 0xFF
    return f"{total:02X}"


def build_frame(command: str, subcommand: str, data: str = "") -> bytes:
    body = PRESS_ADDR + command + subcommand + data
    return START + body.encode("ascii") + checksum(body).encode("ascii") + END


def hex_dump(data: bytes) -> str:
    if not data:
        return "(no bytes received)"
    lines = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = ' '.join(f"{b:02x}" for b in chunk)
        hex_part = f"{hex_part:<47}"
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{offset:04x}  {hex_part}  {ascii_part}")
    return '\n'.join(lines)


def hex_field(hexstr: str, scale: float, unit: str, signed=True) -> str:
    val = int(hexstr, 16)
    if signed and val >= (1 << (len(hexstr) * 4 - 1)):
        val -= (1 << (len(hexstr) * 4))
    return f"{val * scale:g} {unit} (raw {hexstr} = {val})"


# ---------------------------------------------------------------------------
# Command registry - single source of truth for both the TX builder and the
# RX translator.
# ---------------------------------------------------------------------------

def decode_b0(data):
    if len(data) < 30:
        return {"raw": data, "note": "shorter than expected (30 hex chars)"}
    return {
        "model_type": {"0": "Stand-Alone/Head Type", "1": "Unit Type"}.get(data[0:2], data[0:2]),
        "load_spec_code": data[4:6],
        "ram_length_code": data[8:10],
        "external_signal_output": {"0": "Not Available", "1": "Available"}.get(data[10:12], data[10:12]),
        "sw_version": f"Ver. {int(data[16:20], 16) / 100:.2f}" if data[16:20] else "?",
        "custom_number": data[24:28],
        "teaching_version": data[28:30] if len(data) >= 30 else "?",
    }


def decode_position(data):
    return {"position": hex_field(data, 0.0001, "mm")}


def decode_load(data):
    return {"load": hex_field(data, 0.1, "N")}


def decode_g1(data):
    return {"program_number": int(data[0:4], 16), "end_position": hex_field(data[4:12], 0.0001, "mm")}


def decode_g2(data):
    return {"program_number": int(data[0:4], 16), "end_load": hex_field(data[4:12], 0.1, "N")}


def decode_g3(data):
    return {
        "program_number": int(data[0:4], 16),
        "pressing_section": int(data[4:8], 16),
        "sensor_section": int(data[8:12], 16),
        "sensor_value_raw": hex_field(data[12:20], 1, "(load 0.1N or position 0.0001mm - depends on sensor type)"),
    }


def decode_k0(data):
    if len(data) < 12:
        return {"raw": data}
    return {
        "type": data[0:4],
        "bits_1_8": f"{int(data[4:6], 16):08b}",
        "bits_9_16": f"{int(data[6:8], 16):08b}",
        "bit_17": data[8:10],
    }


def decode_k1(data):
    return {"type": data[0:4], "number": int(data[4:8], 16), "status": "ON" if data[8:] not in ("", "0", "00") else "OFF", "raw_tail": data[8:]}


def decode_m0(data):
    is_error = data[0:4].upper() == "FFFF"
    return {"result": "Error (e.g. Servo Ready input is OFF)" if is_error else "Normal - servo state changed"}


def decode_p_response(data):
    if data[0:4].upper() == "FFFF":
        return {"result": "Error (-1) - press could not perform this action right now"}
    try:
        val = int(data[0:4], 16)
        return {"result": "Normal (0)" if val == 0 else f"Program number = {val}"}
    except Exception:
        return {"raw": data}


def decode_generic(data):
    return {"raw_data": data}


V1_RESULT_CODES = {
    0: "Normal", 3211: "Upper limit error", 3212: "Lower limit error", 3213: "Sensor out of range",
    3221: "Pressure exceeded", 3222: "Position exceeded", 3224: "Speed exceeded", 3225: "Time exceeded",
    3229: "Finished mid-operation", 3230: "Other error", 3232: "External signal stopped",
    3233: "Stop time exceeded",
}


def decode_v1(data):
    """
    Decodes a Quality Data Transfer (V1) frame - pushed automatically by the
    press after each completed cycle (after a Q1 request has been sent to
    enable it). Field offsets verified byte-for-byte against the manual's
    own worked example (COM_control.pdf section 11.1): every value below
    reproduced that example exactly (28.098s prep time, 1.31s operation
    time, 86.162mm/417.1N work contact, 87.1575mm/677.8N end).
    Only the fixed-length header is decoded; any sensor sampling data that
    follows (variable length, depends on how many sensors are configured)
    is returned raw and unparsed.
    """
    if len(data) < 88:
        return {"Raw data": data, "Note": f"frame shorter than expected fixed header (got {len(data)} hex chars, need >=88)"}

    result_code = int(data[28:32], 16)
    result_text = V1_RESULT_CODES.get(result_code, f"unknown code {result_code}")
    sensor_fault_section = int(data[32:36], 16)
    sensor_fault_number = int(data[36:40], 16)
    prep_sec = round(int(data[40:48], 16) * 0.001, 3)
    work_sec = round(int(data[48:56], 16) * 0.001, 3)
    contact_pos = round(int(data[56:64], 16) * 0.0001, 4)
    contact_load = round(int(data[64:72], 16) * 0.1, 1)
    end_pos = round(int(data[72:80], 16) * 0.0001, 4)
    end_load = round(int(data[80:88], 16) * 0.1, 1)

    outcome = "Completed normally" if result_code == 0 else f"FAILED - {result_text}"
    sensor_fault = "None" if (sensor_fault_section == 0 and sensor_fault_number == 0) \
        else f"Section {sensor_fault_section}, sensor {sensor_fault_number}"

    return {
        "Summary": f"{outcome} - prep {prep_sec}s, run {work_sec}s, ended at {end_pos}mm / {end_load}N",
        "Program Number": int(data[0:4], 16),
        "Cycle Outcome": outcome,
        "Preparation Time": f"{prep_sec} sec",
        "Work Time (running time)": f"{work_sec} sec",
        "Work Contact Position": f"{contact_pos} mm",
        "Work Contact Load": f"{contact_load} N",
        "End Position": f"{end_pos} mm",
        "End Load": f"{end_load} N",
        "OK Cycles (running total)": int(data[4:12], 16),
        "Fault Cycles (running total)": int(data[12:20], 16),
        "Accumulative Cycles (lifetime total)": int(data[20:28], 16),
        "Sensor Fault Location": sensor_fault,
        "extra_sensor_data_raw": data[88:] if len(data) > 88 else None,
    }


# ---------------------------------------------------------------------------
# Live Cycle Monitor - for when the machine is actually running production.
# Sends Q1 once, then keeps the port open and decodes every V1 frame the
# press pushes as each cycle completes (not just one, unlike the "Capture
# Next Quality Data" one-shot panel). Runs in a background thread since a
# single Flask request can't stay open indefinitely; the frontend polls
# /api/monitor/status to show cycles as they arrive.
# ---------------------------------------------------------------------------

_monitor_lock = threading.Lock()
_monitor_state = {
    "running": False,
    "thread": None,
    "stop_event": None,
    "cycles": [],
    "port": None,
    "baud": None,
    "error": None,
    "started_at": None,
    "last_keepalive_at": None,
    "last_keepalive_ok": None,
}

KEEPALIVE_INTERVAL_SEC = 8.0


def _monitor_worker(port, baud, stop_event):
    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                             parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.3)
    except Exception as e:
        with _monitor_lock:
            _monitor_state["error"] = f"Could not open {port}: {e}"
            _monitor_state["running"] = False
        return

    try:
        ser.rts = True
        ser.dtr = True
        time.sleep(1)
        ser.reset_input_buffer()
        ser.write(build_frame("Q", "1"))
        # Deliberately NOT discarding what comes back here - this press can
        # have a backlog of already-completed cycles' V1 frames queued up
        # and pushes them immediately once Q1 is sent, in the same burst as
        # the 'q1' ack. The main loop below already ignores non-quality-data
        # frames (like the ack), so nothing needs to be thrown away.

        buf = bytearray()
        keepalive_pending = False
        next_keepalive_at = time.time() + KEEPALIVE_INTERVAL_SEC
        while not stop_event.is_set():
            # Periodically poll a lightweight known-working command (N1)
            # while idle. Two purposes: (1) some USB-serial adapters/drivers
            # suspend an idle port after tens of seconds of no bus activity,
            # silently dropping incoming bytes until something wakes it -
            # this prevents that; (2) it gives real visibility into whether
            # the link is actually alive during a long wait, instead of a
            # silent "0 cycles" black box with no way to tell why.
            now = time.time()
            if now >= next_keepalive_at:
                if keepalive_pending:
                    # previous keepalive never got a reply - link looks dead
                    with _monitor_lock:
                        _monitor_state["last_keepalive_ok"] = False
                        _monitor_state["last_keepalive_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                try:
                    ser.write(build_frame("N", "1"))
                    keepalive_pending = True
                except Exception:
                    pass
                next_keepalive_at = now + KEEPALIVE_INTERVAL_SEC

            chunk = ser.read(256)
            if not chunk:
                continue
            buf.extend(chunk)
            while END in buf:
                end_idx = buf.index(END)
                start_idx = buf.rfind(START, 0, end_idx)
                if start_idx == -1:
                    # no start code before this CR - discard junk up to and including the CR
                    del buf[:end_idx + 1]
                    continue
                raw = bytes(buf[start_idx:end_idx + 1])
                del buf[:end_idx + 1]

                content = raw[1:-1].decode("ascii", errors="replace")
                if len(content) < 4:
                    continue
                body, recv_sum = content[:-2], content[-2:].upper()
                calc_sum = checksum(body)
                cmd = body[2:3]
                resp_data = body[4:]

                if cmd.lower() == "n":
                    # reply to our N1 keepalive poll, not a cycle result
                    keepalive_pending = False
                    with _monitor_lock:
                        _monitor_state["last_keepalive_ok"] = (recv_sum == calc_sum)
                        _monitor_state["last_keepalive_at"] = datetime.datetime.now().isoformat(timespec="seconds")
                    continue
                if cmd.lower() != "v":
                    continue  # ignore anything else that isn't a quality-data push
                # NOTE: this press pushes V1 quality data using an uppercase
                # 'V' command code, not the lowercase the general "press
                # replies in lowercase" convention would suggest for every
                # other command - confirmed against live captured frames.
                entry = {
                    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "checksum_ok": recv_sum == calc_sum,
                    **decode_v1(resp_data),
                }
                with _monitor_lock:
                    _monitor_state["cycles"].append(entry)
    except Exception as e:
        with _monitor_lock:
            _monitor_state["error"] = str(e)
    finally:
        try:
            ser.write(build_frame("Q", "0"))
        except Exception:
            pass
        ser.close()
        with _monitor_lock:
            _monitor_state["running"] = False


# ---------------------------------------------------------------------------
# Polling Set - a separate, named group of program-setting reads (S2 "Data
# Request"), added on top of the command picker above without touching it.
#
# Item numbers are from COM_control.pdf section 4.1 "Data Settings (S1)",
# the item-number table shared by S1 (write) and S2 (read). "level"
# determines which section numbers to send: per the manual, "Set the
# pressing section number and sensor section number to 0 to set the head
# section items of a program. Set the sensor section number to 0 to set the
# pressing section items." - so head items use pressing_section=0, pressing
# items use the real pressing_section with sensor_section=0.
#
# Two requested fields could not be resolved from the manual text and are
# deliberately left with item=None rather than guessed:
#   - Return Speed: listed as a head-section item (0.001 mm/s) but its item
#     number was missing from the extracted table (a column-alignment
#     casualty in the source PDF's two-column layout).
#   - Prepare Time: does not appear under this name anywhere in the three
#     manuals in "C:\\JP4 Command com". "Working Time" is mapped to item
#     2018 ("Pressing Time") as a best-effort guess - the manual never uses
#     the term "Working Time" itself, so this should be confirmed.
POLLING_SET_ITEMS = {
    "pressing_speed": {"label": "Pressing Speed", "item": 2006, "unit": "mm/s", "scale": 0.001, "level": "pressing"},
    "return_speed": {"label": "Return Speed", "item": None, "unit": "mm/s", "scale": 0.001, "level": "head",
                      "note": "Item number not found in manual - value omitted, needs confirmation."},
    "start_position": {"label": "Start Position", "item": 1510, "unit": "mm", "scale": 0.0001, "level": "head"},
    "end_position": {"label": "End Position", "item": 2017, "unit": "mm", "scale": 0.0001, "level": "pressing"},
    "end_load": {"label": "End Load", "item": 2016, "unit": "N", "scale": 0.1, "level": "pressing"},
    "working_time": {"label": "Working Time", "item": None, "unit": "sec", "scale": 0.001, "level": "pressing",
                      "note": "Not an S2 program setting - confirmed as the 'Operation time' / 'Work Time' field (same field, two manuals use different names), only available in the Quality Data (V1) frame the press pushes after a completed cycle. Use the 'Capture Next Quality Data' panel instead."},
    "prepare_time": {"label": "Prepare Time", "item": None, "unit": "sec", "scale": 0.001, "level": "pressing",
                      "note": "Not an S2 program setting - this is the manual's 'Preparation time' field, only available in the Quality Data (V1) frame the press pushes after a completed cycle. Use the 'Capture Next Quality Data' panel instead."},
}


def send_s2(ser, program, pressing_section, sensor_section, item_number):
    """Sends a raw S2 (Data Request) command and returns the decoded raw
    integer value, or raises on timeout/error/checksum mismatch."""
    data = f"{program:04X}{pressing_section:04X}{sensor_section:04X}{item_number:04X}"
    frame = build_frame("S", "2", data)
    ser.reset_input_buffer()
    ser.write(frame)
    raw = ser.read_until(END)
    if not raw:
        raise TimeoutError("no response")
    if not raw.startswith(START) or not raw.endswith(END):
        raise ValueError(f"malformed response: {raw!r}")
    content = raw[1:-1].decode("ascii", errors="replace")
    body, recv_sum = content[:-2], content[-2:].upper()
    calc_sum = checksum(body)
    if recv_sum != calc_sum:
        raise ValueError(f"checksum mismatch (received {recv_sum}, calculated {calc_sum})")
    resp_data = body[4:]
    if body[2:3] == "e":
        raise ValueError(f"press communication error, subcommand {body[3:4]}")
    result_code = resp_data[0:4]
    if result_code.upper() == "FFFF":
        raise ValueError("press returned error - program/section/item does not exist")
    value_hex = resp_data[4:12]
    val = int(value_hex, 16)
    if val >= (1 << 31):
        val -= (1 << 32)
    return val


COMMANDS = {
    # NOTE: A0 "Connection Confirmation" was removed - it does not exist in
    # COM_control.pdf (the manual that matches this press's actual $...CR
    # protocol, confirmed live against real hardware). It was mistakenly
    # carried over from COM_basic.pdf, which documents a different, older
    # fixed-block protocol this press does not speak. B0 (or any other read
    # command) already proves connectivity - there's no separate "are you
    # there" command in the protocol this unit actually uses.
    "B0": {
        "label": "B0 - System Information (model, load rating, versions)",
        "group": "read",
        "cmd": "B", "subcmd": "0", "params": [],
        "description": "Reads press model type, load rating, ram stroke length, and firmware/teaching-data version numbers.",
        "decode": decode_b0,
    },
    "N0": {
        "label": "N0 - Instructed Position (where the controller told the ram to go)",
        "group": "read",
        "cmd": "N", "subcmd": "0", "params": [],
        "description": "Current commanded ram position, in mm.",
        "decode": decode_position,
    },
    "N1": {
        "label": "N1 - Encoder Position (actual ram position, from motor encoder)",
        "group": "read",
        "cmd": "N", "subcmd": "1", "params": [],
        "description": "Actual current ram position measured by the motor encoder, in mm.",
        "decode": decode_position,
    },
    "N2": {
        "label": "N2 - Current Load (live force on the ram right now)",
        "group": "read",
        "cmd": "N", "subcmd": "2", "params": [],
        "description": "Instantaneous load/force on the ram, in Newtons.",
        "decode": decode_load,
    },
    "G1": {
        "label": "G1 - End Position of Last Cycle",
        "group": "read",
        "cmd": "G", "subcmd": "1", "params": [],
        "description": "Program number and the ram's final position at the end of the last completed cycle.",
        "decode": decode_g1,
    },
    "G2": {
        "label": "G2 - End Load of Last Cycle",
        "group": "read",
        "cmd": "G", "subcmd": "2", "params": [],
        "description": "Program number and the load recorded at the end of the last completed cycle.",
        "decode": decode_g2,
    },
    "G3": {
        "label": "G3 - Sensor Value Readout (pick a pressing/sensor section)",
        "group": "read",
        "cmd": "G", "subcmd": "3",
        "params": [
            {"name": "pressing_section", "label": "Pressing section number", "hex_digits": 4, "default": "1"},
            {"name": "sensor_section", "label": "Sensor section number", "hex_digits": 4, "default": "1"},
        ],
        "description": "Reads a specific recorded sensor value (position or load, depending on how that section is configured) from the last cycle.",
        "decode": decode_g3,
    },
    "K0": {
        "label": "K0 - IO Status Readout (all points of one type)",
        "group": "read",
        "cmd": "K", "subcmd": "0",
        "params": [
            {"name": "type", "label": "IO type (0=System In, 1=General In, 2=System Out, 3=General Out)", "hex_digits": 4, "default": "0"},
        ],
        "description": "Reads the ON/OFF status of every point of the selected IO type, as a bit flag map.",
        "decode": decode_k0,
    },
    "K1": {
        "label": "K1 - IO Status Readout (one specific point)",
        "group": "read",
        "cmd": "K", "subcmd": "1",
        "params": [
            {"name": "type", "label": "IO type (0=System In, 1=General In, 2=System Out, 3=General Out)", "hex_digits": 4, "default": "0"},
            {"name": "number", "label": "IO point number", "hex_digits": 4, "default": "1"},
        ],
        "description": "Reads the ON/OFF status of one specific IO point.",
        "decode": decode_k1,
    },
    "M0": {
        "label": "M0 - Servo ON/OFF",
        "group": "control",
        "cmd": "M", "subcmd": "0",
        "params": [
            {"name": "state", "label": "State (0 = OFF, 1 = ON)", "hex_digits": 4, "default": "1"},
        ],
        "description": "Turns the servomotor ON or OFF. Turning it ON energizes the motor (it will hold position / may be ready to move).",
        "hazard": "Energizes the servomotor.",
        "decode": decode_m0,
    },
    "P0": {
        "label": "P0 - Mechanical Initialization (motor ON, servo ON, home the ram)",
        "group": "control",
        "cmd": "P", "subcmd": "0", "params": [],
        "description": "Turns motor + servo ON and runs mechanical initialization (homing). THE RAM WILL MOVE.",
        "hazard": "Moves the ram (homing sequence).",
        "decode": decode_p_response,
    },
    "P1": {
        "label": "P1 - Change Active Program Number",
        "group": "control",
        "cmd": "P", "subcmd": "1",
        "params": [{"name": "program", "label": "Program number", "hex_digits": 4, "default": "1"}],
        "description": "Switches which program the press will run next. Does not move the ram by itself.",
        "hazard": "Changes press configuration state.",
        "decode": decode_p_response,
    },
    "P2": {
        "label": "P2 - Return to Work Home Position",
        "group": "control",
        "cmd": "P", "subcmd": "2", "params": [],
        "description": "Moves the ram to the work home position of the current program. THE RAM WILL MOVE.",
        "hazard": "Moves the ram.",
        "decode": decode_p_response,
    },
    "P3": {
        "label": "P3 - START (runs a full press cycle now)",
        "group": "control",
        "cmd": "P", "subcmd": "3", "params": [],
        "description": "Starts the press running the currently active program - a full pressing cycle.",
        "hazard": "RUNS A FULL PRESS CYCLE. This is the most dangerous command here.",
        "decode": decode_p_response,
    },
    "P4": {
        "label": "P4 - Start a Specific Program Number",
        "group": "control",
        "cmd": "P", "subcmd": "4",
        "params": [{"name": "program", "label": "Program number", "hex_digits": 4, "default": "1"}],
        "description": "Starts the press running the specified program - a full pressing cycle.",
        "hazard": "RUNS A FULL PRESS CYCLE. This is the most dangerous command here.",
        "decode": decode_p_response,
    },
    "Q1": {
        "label": "Q1 - Enable Quality Data Streaming",
        "group": "read",
        "cmd": "Q", "subcmd": "1", "params": [],
        "description": "Tells the press to start automatically pushing a Quality Data (V1) frame after every completed cycle - includes preparation time, operation time, work contact and end position/load. No response frame; use the 'Capture Next Quality Data' panel below to enable + wait for one.",
        "decode": decode_generic,
    },
    "Q0": {
        "label": "Q0 - Stop Quality Data Streaming",
        "group": "read",
        "cmd": "Q", "subcmd": "0", "params": [],
        "description": "Stops the automatic V1 push enabled by Q1. No response frame documented - a timeout here is expected/normal, not an error.",
        "decode": decode_generic,
    },
}


def build_data(cmd_id: str, param_values: dict) -> str:
    spec = COMMANDS[cmd_id]
    parts = []
    for p in spec["params"]:
        raw = param_values.get(p["name"], p["default"])
        val = int(raw)
        parts.append(f"{val:0{p['hex_digits']}X}")
    return ''.join(parts)


def run_command(ser: serial.Serial, cmd_id: str, param_values: dict):
    spec = COMMANDS[cmd_id]
    data = build_data(cmd_id, param_values)
    frame = build_frame(spec["cmd"], spec["subcmd"], data)

    result = {
        "command_id": cmd_id,
        "label": spec["label"],
        "tx_hex": frame.hex(' '),
        "tx_repr": repr(frame),
    }

    ser.reset_input_buffer()
    ser.write(frame)

    raw = ser.read_until(END)
    if not raw:
        result["rx_hex"] = None
        result["status"] = "timeout"
        return result

    result["rx_hex"] = raw.hex(' ')
    result["rx_repr"] = repr(raw)
    result["rx_dump"] = hex_dump(raw)

    if not raw.startswith(START) or not raw.endswith(END):
        result["status"] = "malformed"
        return result

    content = raw[1:-1].decode("ascii", errors="replace")
    if len(content) < 4:
        result["status"] = "malformed"
        return result

    body, recv_sum = content[:-2], content[-2:].upper()
    # Checksum covers address+command+subcommand+data, matching the manual
    # ("from Address 1 to the end of the data") - this is the documented
    # formula and matches all clean captures. An early single sample
    # appeared to match an address-excluded variant instead, but that turned
    # out to be a corrupted frame that coincidentally satisfied a different
    # 8-bit sum by chance (this connection has been intermittently noisy all
    # session) - not a real alternate algorithm. Treat mismatches as a
    # connection-quality signal, not a formula bug.
    calc_sum = checksum(body)
    result["address"] = body[0:2]
    result["command"] = body[2:3]
    result["subcommand"] = body[3:4]
    result["data"] = body[4:]
    result["checksum_received"] = recv_sum
    result["checksum_calculated"] = calc_sum
    result["checksum_ok"] = recv_sum == calc_sum

    if body[2:3] == "e":
        errors = {"0": "Other comm error", "1": "Receive timeout", "2": "Bad command/subcommand", "4": "Checksum error"}
        result["status"] = "press_error"
        result["translated"] = {"error": errors.get(body[3:4], "unknown")}
        return result

    result["status"] = "ok" if result["checksum_ok"] else "checksum_mismatch"

    # Temporary/ack response: press echoes the SAME case command letter
    # back (e.g. we sent 'P', temporary reply is also 'P') for the
    # slow-to-complete P0/P2/P3/P4 commands.
    if body[2:3] == spec["cmd"]:
        result["translated"] = {"note": "Temporary/start acknowledgement - press accepted the command and is now executing it. A second, final response follows once the motion/cycle completes."}
        return result

    decoder = spec.get("decode")
    if decoder:
        try:
            result["translated"] = decoder(result["data"])
        except Exception as e:
            result["translated"] = {"decode_error": str(e), "raw_data": result["data"]}
    else:
        result["translated"] = decode_generic(result["data"])

    return result


@app.route('/')
def home():
    return PAGE_HTML


@app.route('/api/commands')
def api_commands():
    out = {}
    for cid, spec in COMMANDS.items():
        out[cid] = {
            "label": spec["label"],
            "group": spec["group"],
            "description": spec["description"],
            "params": spec["params"],
            "hazard": spec.get("hazard"),
        }
    return jsonify(out)


@app.route('/api/scan_ports')
def scan_ports():
    ports = []
    try:
        for p in serial.tools.list_ports.comports():
            ports.append({"device": p.device, "description": p.description})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})
    return jsonify({"success": True, "ports": ports})


@app.route('/api/listen', methods=['POST'])
def api_listen():
    req = request.get_json() or {}
    port = req.get("port", "COM3")
    baud = int(req.get("baud", 9600))
    duration = float(req.get("duration", 5.0))
    force_rts_dtr = bool(req.get("force_rts_dtr", True))

    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                             parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.2)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not open {port}: {e}"})

    line_state = {}
    try:
        if force_rts_dtr:
            ser.rts = True
            ser.dtr = True
        time.sleep(0.5)
        try:
            line_state = {"rts": ser.rts, "dtr": ser.dtr, "cts": ser.cts, "dsr": ser.dsr}
        except Exception:
            pass

        collected = bytearray()
        deadline = time.time() + duration
        while time.time() < deadline:
            chunk = ser.read(256)
            if chunk:
                collected.extend(chunk)
    finally:
        ser.close()

    return jsonify({
        "success": True, "port": port, "baud": baud, "duration": duration,
        "line_state": line_state, "byte_count": len(collected),
        "hex_dump": hex_dump(bytes(collected)),
    })


@app.route('/api/send', methods=['POST'])
def api_send():
    req = request.get_json() or {}
    cmd_id = req.get("command_id")
    if cmd_id not in COMMANDS:
        return jsonify({"success": False, "message": f"Unknown command_id: {cmd_id}"})

    spec = COMMANDS[cmd_id]
    if spec["group"] == "control" and not req.get("confirmed"):
        return jsonify({"success": False, "message": "This is a control/motion command and was not confirmed. Refusing to send."})

    port = req.get("port", "COM3")
    baud = int(req.get("baud", 9600))
    timeout = float(req.get("timeout", 2.0))
    force_rts_dtr = bool(req.get("force_rts_dtr", True))
    params = req.get("params", {})

    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                             parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=timeout)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not open {port}: {e}"})

    line_state = {}
    try:
        if force_rts_dtr:
            ser.rts = True
            ser.dtr = True
        time.sleep(1)
        try:
            line_state = {"rts": ser.rts, "dtr": ser.dtr, "cts": ser.cts, "dsr": ser.dsr}
        except Exception:
            pass
        result = run_command(ser, cmd_id, params)
    finally:
        ser.close()

    return jsonify({"success": True, "port": port, "baud": baud, "line_state": line_state, "result": result})


@app.route('/api/poll_set', methods=['POST'])
def api_poll_set():
    """Polls the named set of program settings (Pressing Speed, Return
    Speed, Start/End Position, End Load, Working/Prepare Time + computed
    Stroke) via S2 Data Request and returns them as one JSON object."""
    req = request.get_json() or {}
    port = req.get("port", "COM3")
    baud = int(req.get("baud", 38400))
    timeout = float(req.get("timeout", 2.0))
    program = int(req.get("program", 1))
    pressing_section = int(req.get("pressing_section", 1))

    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                             parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=timeout)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not open {port}: {e}"})

    values = {}
    errors = {}
    try:
        ser.rts = True
        ser.dtr = True
        time.sleep(1)

        for key, spec in POLLING_SET_ITEMS.items():
            if spec["item"] is None:
                errors[key] = spec.get("note", "item number unknown")
                continue
            section = 0 if spec["level"] == "head" else pressing_section
            try:
                raw_val = send_s2(ser, program, section, 0, spec["item"])
                values[key] = round(raw_val * spec["scale"], 6)
            except Exception as e:
                errors[key] = str(e)

        if "end_position" in values and "start_position" in values:
            values["stroke"] = round(values["end_position"] - values["start_position"], 6)
        else:
            errors["stroke"] = "requires both start_position and end_position, at least one failed"
    finally:
        ser.close()

    units = {k: v["unit"] for k, v in POLLING_SET_ITEMS.items()}
    units["stroke"] = "mm"
    output = {
        "program_number": program,
        "pressing_section": pressing_section,
        "units": units,
        "values": values,
        "unavailable": errors,
    }
    return jsonify({"success": True, "poll_result": output})


@app.route('/api/capture_v1', methods=['POST'])
def api_capture_v1():
    """Sends Q1 to enable quality-data streaming, then waits up to
    wait_seconds for the press to push a V1 frame after a cycle completes.
    This does NOT trigger a cycle itself - the press (or an operator) needs
    to actually run one while this is waiting."""
    req = request.get_json() or {}
    port = req.get("port", "COM3")
    baud = int(req.get("baud", 38400))
    wait_seconds = float(req.get("wait_seconds", 30.0))

    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=serial.EIGHTBITS,
                             parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE, timeout=0.5)
    except Exception as e:
        return jsonify({"success": False, "message": f"Could not open {port}: {e}"})

    try:
        ser.rts = True
        ser.dtr = True
        time.sleep(1)

        q1_frame = build_frame("Q", "1")
        ser.reset_input_buffer()
        ser.write(q1_frame)
        # NOT discarding what comes back: the press acks Q1 with a 'q1'
        # frame, and may immediately follow it with backlogged V1 frames
        # from already-completed cycles in the same burst. Both the ack and
        # any V1 frames are parsed below; only a V1/v1 frame is returned.

        deadline = time.time() + wait_seconds
        buf = bytearray()
        result = None
        while time.time() < deadline and result is None:
            chunk = ser.read(256)
            if not chunk:
                continue
            buf.extend(chunk)
            while END in buf:
                end_idx = buf.index(END)
                start_idx = buf.rfind(START, 0, end_idx)
                if start_idx == -1:
                    del buf[:end_idx + 1]
                    continue
                raw = bytes(buf[start_idx:end_idx + 1])
                del buf[:end_idx + 1]

                content = raw[1:-1].decode("ascii", errors="replace")
                if len(content) < 4:
                    continue
                body, recv_sum = content[:-2], content[-2:].upper()
                calc_sum = checksum(body)
                cmd = body[2:3]
                resp_data = body[4:]
                if cmd.lower() != "v":
                    continue  # e.g. the 'q1' ack - keep waiting for actual quality data
                result = {
                    "captured": True,
                    "rx_hex": raw.hex(' '),
                    "checksum_received": recv_sum,
                    "checksum_calculated": calc_sum,
                    "checksum_ok": recv_sum == calc_sum,
                    "command": cmd,
                    "translated": decode_v1(resp_data),
                }
                break

        if result is None:
            return jsonify({"success": True, "captured": False,
                             "message": f"No V1 frame received within {wait_seconds}s. Run a press cycle while this is waiting, then try again."})
    finally:
        ser.close()

    return jsonify({"success": True, **result})


@app.route('/api/monitor/start', methods=['POST'])
def api_monitor_start():
    req = request.get_json() or {}
    port = req.get("port", "COM3")
    baud = int(req.get("baud", 38400))

    with _monitor_lock:
        if _monitor_state["running"]:
            return jsonify({"success": False, "message": "Monitor is already running. Stop it first."})
        stop_event = threading.Event()
        _monitor_state.update({
            "running": True, "thread": None, "stop_event": stop_event,
            "cycles": [], "port": port, "baud": baud, "error": None,
            "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "last_keepalive_at": None, "last_keepalive_ok": None,
        })
        t = threading.Thread(target=_monitor_worker, args=(port, baud, stop_event), daemon=True)
        _monitor_state["thread"] = t
        t.start()

    return jsonify({"success": True, "message": f"Monitor started on {port} @ {baud}. Q1 sent - run cycles now."})


@app.route('/api/monitor/stop', methods=['POST'])
def api_monitor_stop():
    with _monitor_lock:
        stop_event = _monitor_state["stop_event"]
        thread = _monitor_state["thread"]
    if stop_event is None:
        return jsonify({"success": False, "message": "Monitor was never started."})
    stop_event.set()
    if thread:
        thread.join(timeout=5)
    with _monitor_lock:
        cycles = list(_monitor_state["cycles"])
        running = _monitor_state["running"]
    return jsonify({"success": True, "still_running": running, "cycle_count": len(cycles), "cycles": cycles})


@app.route('/api/monitor/status')
def api_monitor_status():
    with _monitor_lock:
        return jsonify({
            "success": True,
            "running": _monitor_state["running"],
            "port": _monitor_state["port"],
            "baud": _monitor_state["baud"],
            "started_at": _monitor_state["started_at"],
            "error": _monitor_state["error"],
            "last_keepalive_at": _monitor_state["last_keepalive_at"],
            "last_keepalive_ok": _monitor_state["last_keepalive_ok"],
            "cycle_count": len(_monitor_state["cycles"]),
            "cycles": list(_monitor_state["cycles"]),
        })


PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>JP4 Serial Diagnostic</title>
<style>
  body { background:#0b0f17; color:#e2e8f0; font-family: 'Segoe UI', sans-serif; margin:0; padding:24px; max-width:900px; }
  h1 { font-size:1.3rem; margin:0 0 4px 0; }
  .sub { color:#94a3b8; font-size:0.85rem; margin-bottom:20px; }
  .panel { background:#111827; border:1px solid #263042; border-radius:10px; padding:16px; margin-bottom:16px; }
  label { display:block; font-size:0.75rem; color:#94a3b8; margin-bottom:4px; text-transform:uppercase; letter-spacing:0.05em; }
  input, select { background:#0b0f17; border:1px solid #263042; color:#e2e8f0; padding:8px 10px; border-radius:6px; font-size:0.9rem; width:100%; box-sizing:border-box; }
  .row { display:flex; gap:12px; margin-bottom:12px; flex-wrap:wrap; }
  .row > div { flex:1; min-width:140px; }
  button { background:#0ea5e9; color:#fff; border:none; padding:10px 18px; border-radius:6px; font-weight:600; cursor:pointer; font-size:0.9rem; }
  button:hover { background:#0284c7; }
  button:disabled { background:#334155; cursor:not-allowed; }
  button.secondary { background:#263042; }
  button.secondary:hover { background:#334155; }
  button.danger { background:#dc2626; }
  button.danger:hover { background:#b91c1c; }
  .checkbox-row { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
  .checkbox-row input { width:auto; }
  .result { border:1px solid #263042; border-radius:8px; padding:10px 12px; margin-bottom:10px; font-size:0.85rem; }
  .result.ok { border-color:#10b981; }
  .result.timeout { border-color:#f59e0b; }
  .result.checksum_mismatch { border-color:#f59e0b; }
  .result.press_error, .result.malformed, .result.unexpected { border-color:#ef4444; }
  .result .label { font-weight:600; margin-bottom:6px; color:#e2e8f0; }
  .result .line { color:#94a3b8; word-break:break-all; font-family:'Consolas',monospace; font-size:0.8rem; }
  .status-badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.7rem; font-weight:700; text-transform:uppercase; margin-left:8px; }
  .status-badge.ok { background:#064e3b; color:#10b981; }
  .status-badge.timeout, .status-badge.checksum_mismatch { background:#78350f; color:#f59e0b; }
  .status-badge.press_error, .status-badge.malformed, .status-badge.unexpected { background:#7f1d1d; color:#ef4444; }
  .translated { background:#0b0f17; border:1px solid #1e293b; border-radius:6px; padding:10px 12px; margin-top:8px; }
  .translated table { width:100%; border-collapse:collapse; }
  .translated td { padding:3px 6px; font-size:0.85rem; }
  .translated td:first-child { color:#94a3b8; text-transform:capitalize; width:40%; }
  .translated td:last-child { color:#e2e8f0; font-weight:600; }
  pre.dump { background:#0b0f17; border:1px solid #1e293b; border-radius:6px; padding:8px 10px; margin:6px 0 0 0; overflow-x:auto; font-size:0.78rem; line-height:1.4; }
  .devlist { font-size:0.8rem; color:#94a3b8; margin-top:6px; }
  .devlist span { cursor:pointer; color:#0ea5e9; text-decoration:underline; margin-right:10px; }
  .desc { font-size:0.8rem; color:#94a3b8; margin:8px 0; }
  .hazard-note { background:#450a0a; border:1px solid #dc2626; border-radius:6px; padding:10px 12px; color:#fca5a5; font-size:0.85rem; margin:8px 0; }
  optgroup { color:#94a3b8; }
</style>
</head>
<body>
  <h1>JP4 Serial Diagnostic</h1>
  <div class="sub">Pick a command in plain language, fill in any parameters, send it. Responses are auto-translated.</div>

  <div class="panel">
    <div class="row">
      <div>
        <label>Serial Port</label>
        <input type="text" id="port" value="COM3">
        <div class="devlist" id="devlist"></div>
      </div>
      <div>
        <label>Baud Rate</label>
        <select id="baud">
          <option value="9600">9600</option>
          <option value="19200">19200</option>
          <option value="38400" selected>38400 (confirmed working)</option>
        </select>
      </div>
      <div>
        <label>Timeout (sec)</label>
        <input type="number" id="timeout" value="2.0" step="0.5" min="0.5">
      </div>
    </div>
    <div class="checkbox-row">
      <input type="checkbox" id="forceRtsDtr" checked>
      <label style="margin:0;text-transform:none;">Force RTS/DTR high before testing</label>
    </div>
    <button onclick="scanPorts()" class="secondary">Scan Ports</button>
  </div>

  <div class="panel">
    <label>Command</label>
    <select id="commandSelect" onchange="onCommandChange()"></select>
    <div class="desc" id="commandDesc"></div>
    <div id="paramFields"></div>
    <div id="hazardBox"></div>
    <button id="sendBtn" onclick="sendCommand()">Send</button>
  </div>

  <div id="lineState" class="sub"></div>
  <div id="results"></div>

  <div class="panel">
    <div class="sub" style="margin-bottom:12px;">
      <strong style="color:#e2e8f0;">Polling Set (JSON)</strong> - reads a fixed named set of program settings
      (Pressing Speed, Return Speed, Start/End Position, End Load, Working/Prepare Time, plus computed Stroke)
      via S2 Data Request and outputs them together as JSON. Separate from the single-command picker above.
    </div>
    <div class="row">
      <div style="max-width:160px;">
        <label>Program Number</label>
        <input type="number" id="pollProgram" value="1" min="1">
      </div>
      <div style="max-width:160px;">
        <label>Pressing Section</label>
        <input type="number" id="pollSection" value="1" min="1">
      </div>
    </div>
    <button onclick="runPollSet()">Poll All</button>
  </div>
  <div id="pollSetResult"></div>

  <div class="panel">
    <div class="sub" style="margin-bottom:12px;">
      <strong style="color:#e2e8f0;">Capture Next Quality Data (Prepare/Work Time)</strong> - sends Q1 to enable
      quality-data streaming, then waits for the press to push a Quality Data (V1) frame after a cycle
      completes. Includes Preparation Time, Work Time, work-contact and end position/load, OK/fault counters.
      <strong>This does not run a cycle itself</strong> - run one on the press (or via P3/P4 if you've confirmed
      that separately) while this is waiting.
    </div>
    <div class="row">
      <div style="max-width:200px;">
        <label>Max Wait (sec)</label>
        <input type="number" id="captureWait" value="30" step="5" min="5">
      </div>
    </div>
    <button onclick="runCaptureV1()">Enable &amp; Wait for Next Cycle</button>
  </div>
  <div id="captureV1Result"></div>

  <div class="panel">
    <div class="sub" style="margin-bottom:12px;">
      <strong style="color:#e2e8f0;">Live Cycle Monitor</strong> - for when the machine is actually running
      production. Sends Q1 once and keeps listening in the background, decoding <em>every</em> cycle's
      Quality Data frame as it arrives (not just one) - Preparation Time, Work Time, end position/load,
      OK/fault counters, etc. Leave it running while the machine cycles; each completed cycle appears
      below automatically. Stop when done to get the full JSON log. Polls a lightweight keep-alive
      (N1) every 8s while idle - this also shows in the status line below so you can tell the link is
      actually alive, not just silently waiting.
    </div>
    <div class="row" style="align-items:flex-end;">
      <button id="monitorStartBtn" onclick="startMonitor()">Start Monitoring</button>
      <button id="monitorStopBtn" onclick="stopMonitor()" class="secondary" disabled>Stop &amp; Get Log</button>
    </div>
  </div>
  <div id="monitorStatus" class="sub"></div>
  <div id="monitorResult"></div>

  <div class="panel">
    <div class="sub" style="margin-bottom:12px;">
      <strong style="color:#e2e8f0;">Listen Only</strong> - opens the port and just watches for any bytes
      the press sends on its own. Sends nothing.
    </div>
    <div class="row">
      <div style="max-width:160px;">
        <label>Listen Duration (sec)</label>
        <input type="number" id="listenDuration" value="5" step="1" min="1">
      </div>
    </div>
    <button onclick="runListen()" class="secondary">Start Listening</button>
  </div>
  <div id="listenResult"></div>

<script>
let COMMANDS = {};

async function loadCommands() {
  const res = await fetch('/api/commands');
  COMMANDS = await res.json();
  const sel = document.getElementById('commandSelect');
  const readGroup = document.createElement('optgroup');
  readGroup.label = 'Read / Query - safe, cannot move the press';
  const controlGroup = document.createElement('optgroup');
  controlGroup.label = 'Control - WILL move the press or change its state';
  for (const [id, spec] of Object.entries(COMMANDS)) {
    const opt = document.createElement('option');
    opt.value = id;
    opt.textContent = spec.label;
    (spec.group === 'control' ? controlGroup : readGroup).appendChild(opt);
  }
  sel.appendChild(readGroup);
  sel.appendChild(controlGroup);
  onCommandChange();
}

function onCommandChange() {
  const id = document.getElementById('commandSelect').value;
  const spec = COMMANDS[id];
  document.getElementById('commandDesc').textContent = spec.description;

  const paramsEl = document.getElementById('paramFields');
  paramsEl.innerHTML = spec.params.map(p => `
    <div style="margin-bottom:10px;">
      <label>${p.label}</label>
      <input type="number" id="param_${p.name}" value="${p.default}">
    </div>
  `).join('');

  const hazardEl = document.getElementById('hazardBox');
  const sendBtn = document.getElementById('sendBtn');
  if (spec.group === 'control') {
    hazardEl.innerHTML = `
      <div class="hazard-note">⚠ ${spec.hazard}</div>
      <div class="checkbox-row">
        <input type="checkbox" id="confirmHazard" onchange="document.getElementById('sendBtn').disabled = !this.checked">
        <label style="margin:0;text-transform:none;color:#fca5a5;">I understand and want to send this to the physical press</label>
      </div>
    `;
    sendBtn.className = 'danger';
    sendBtn.disabled = true;
  } else {
    hazardEl.innerHTML = '';
    sendBtn.className = '';
    sendBtn.disabled = false;
  }
}

async function scanPorts() {
  const res = await fetch('/api/scan_ports');
  const data = await res.json();
  const el = document.getElementById('devlist');
  if (data.success && data.ports.length) {
    el.innerHTML = data.ports.map(p => `<span onclick="document.getElementById('port').value='${p.device}'">${p.device} (${p.description})</span>`).join('');
  } else {
    el.innerHTML = 'No ports detected.';
  }
}

function renderTranslated(t) {
  if (!t) return '';
  const rows = Object.entries(t).map(([k, v]) => `<tr><td>${k.replace(/_/g, ' ')}</td><td>${v}</td></tr>`).join('');
  return `<div class="translated"><table>${rows}</table></div>`;
}

async function sendCommand() {
  const commandId = document.getElementById('commandSelect').value;
  const spec = COMMANDS[commandId];
  const port = document.getElementById('port').value.trim();
  const baud = document.getElementById('baud').value;
  const timeout = document.getElementById('timeout').value;
  const forceRtsDtr = document.getElementById('forceRtsDtr').checked;
  const confirmed = spec.group === 'control' ? document.getElementById('confirmHazard').checked : true;

  const params = {};
  spec.params.forEach(p => { params[p.name] = document.getElementById(`param_${p.name}`).value; });

  const resultsEl = document.getElementById('results');
  const lineStateEl = document.getElementById('lineState');
  resultsEl.innerHTML = '<div class="sub">Sending...</div>';

  try {
    const res = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command_id: commandId, port, baud, timeout, force_rts_dtr: forceRtsDtr, confirmed, params})
    });
    const data = await res.json();

    if (!data.success) {
      resultsEl.innerHTML = `<div class="result press_error"><div class="label">Error</div><div class="line">${data.message}</div></div>`;
      return;
    }

    if (data.line_state) {
      const ls = data.line_state;
      lineStateEl.innerHTML = `Line state: RTS=${ls.rts} DTR=${ls.dtr} CTS(in)=${ls.cts} DSR(in)=${ls.dsr}`;
    }

    const r = data.result;
    let html = `<div class="result ${r.status}">`;
    html += `<div class="label">${r.label} <span class="status-badge ${r.status}">${r.status.replace(/_/g,' ')}</span></div>`;
    html += `<div class="line">TX: ${r.tx_hex}</div>`;
    if (r.rx_hex) {
      html += `<div class="line">RX: ${r.rx_hex}</div>`;
      if (r.rx_dump) html += `<pre class="dump">${r.rx_dump}</pre>`;
      if (r.data !== undefined) {
        html += `<div class="line" style="margin-top:6px;">Checksum: received=${r.checksum_received} calculated=${r.checksum_calculated} (${r.checksum_ok ? 'OK' : 'MISMATCH - treat data with caution'})</div>`;
      }
    } else {
      html += `<div class="line">RX: (nothing received)</div>`;
    }
    if (r.translated) html += renderTranslated(r.translated);
    html += `</div>`;
    resultsEl.innerHTML = html;
  } catch (err) {
    resultsEl.innerHTML = `<div class="result press_error"><div class="label">Request failed</div><div class="line">${err.message}</div></div>`;
  }
}

async function runPollSet() {
  const port = document.getElementById('port').value.trim();
  const baud = document.getElementById('baud').value;
  const timeout = document.getElementById('timeout').value;
  const program = document.getElementById('pollProgram').value;
  const pressing_section = document.getElementById('pollSection').value;

  const el = document.getElementById('pollSetResult');
  el.innerHTML = '<div class="sub">Polling...</div>';

  try {
    const res = await fetch('/api/poll_set', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({port, baud, timeout, program, pressing_section})
    });
    const data = await res.json();
    if (!data.success) {
      el.innerHTML = `<div class="result press_error"><div class="label">Error</div><div class="line">${data.message}</div></div>`;
      return;
    }
    const jsonText = JSON.stringify(data.poll_result, null, 2);
    const unavailableCount = Object.keys(data.poll_result.unavailable || {}).length;
    const status = unavailableCount > 0 ? 'checksum_mismatch' : 'ok';
    el.innerHTML = `
      <div class="result ${status}">
        <div class="label">Poll result <span class="status-badge ${status}">${unavailableCount} field(s) unavailable</span></div>
        <pre class="dump">${jsonText}</pre>
        <button class="secondary" style="margin-top:8px;" onclick="navigator.clipboard.writeText(${JSON.stringify(jsonText)})">Copy JSON</button>
      </div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="result press_error"><div class="label">Request failed</div><div class="line">${err.message}</div></div>`;
  }
}

let monitorPollTimer = null;

function renderMonitorCycles(cycles) {
  if (!cycles || cycles.length === 0) {
    return '<div class="sub">No cycles captured yet - run the press.</div>';
  }
  // newest first
  const rows = cycles.slice().reverse().map(c => {
    const okBadge = c.checksum_ok ? '<span class="status-badge ok">ok</span>' : '<span class="status-badge checksum_mismatch">checksum mismatch</span>';
    return `<pre class="dump">[${c.timestamp}] ${okBadge}\n${JSON.stringify(c, null, 2)}</pre>`;
  }).join('');
  return rows;
}

async function pollMonitorStatus() {
  try {
    const res = await fetch('/api/monitor/status');
    const data = await res.json();
    const statusEl = document.getElementById('monitorStatus');
    const resultEl = document.getElementById('monitorResult');
    if (!data.success) return;

    let linkText = 'Link: not checked yet';
    if (data.last_keepalive_at) {
      linkText = data.last_keepalive_ok ? `Link: ALIVE (checked ${data.last_keepalive_at})` : `Link: NO RESPONSE (checked ${data.last_keepalive_at})`;
    }
    statusEl.textContent = `Running: ${data.running} | Port: ${data.port}@${data.baud} | Started: ${data.started_at} | Cycles captured: ${data.cycle_count} | ${linkText}` + (data.error ? ` | ERROR: ${data.error}` : '');
    resultEl.innerHTML = renderMonitorCycles(data.cycles);

    document.getElementById('monitorStartBtn').disabled = data.running;
    document.getElementById('monitorStopBtn').disabled = !data.running;
  } catch (err) {
    // keep polling even if one tick fails
  }
}

async function startMonitor() {
  const port = document.getElementById('port').value.trim();
  const baud = document.getElementById('baud').value;

  const res = await fetch('/api/monitor/start', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({port, baud})
  });
  const data = await res.json();
  if (!data.success) {
    document.getElementById('monitorStatus').textContent = data.message;
    return;
  }
  document.getElementById('monitorStartBtn').disabled = true;
  document.getElementById('monitorStopBtn').disabled = false;
  if (monitorPollTimer) clearInterval(monitorPollTimer);
  monitorPollTimer = setInterval(pollMonitorStatus, 2000);
  pollMonitorStatus();
}

async function stopMonitor() {
  const res = await fetch('/api/monitor/stop', { method: 'POST' });
  const data = await res.json();
  if (monitorPollTimer) { clearInterval(monitorPollTimer); monitorPollTimer = null; }
  document.getElementById('monitorStartBtn').disabled = false;
  document.getElementById('monitorStopBtn').disabled = true;
  if (!data.success) {
    document.getElementById('monitorStatus').textContent = data.message;
    return;
  }
  const fullLog = JSON.stringify(data.cycles, null, 2);
  document.getElementById('monitorStatus').textContent = `Stopped. ${data.cycle_count} cycle(s) captured.`;
  document.getElementById('monitorResult').innerHTML = `
    <div class="result ok">
      <div class="label">Full session log (${data.cycle_count} cycles)</div>
      <pre class="dump">${fullLog}</pre>
      <button class="secondary" style="margin-top:8px;" onclick="navigator.clipboard.writeText(${JSON.stringify(fullLog)})">Copy Full JSON</button>
    </div>
  `;
}

// Resume polling on page load in case a monitor session is already running
pollMonitorStatus();

async function runCaptureV1() {
  const port = document.getElementById('port').value.trim();
  const baud = document.getElementById('baud').value;
  const wait_seconds = document.getElementById('captureWait').value;

  const el = document.getElementById('captureV1Result');
  el.innerHTML = `<div class="sub">Enabled Q1 streaming, waiting up to ${wait_seconds}s for the next cycle's Quality Data frame - run a cycle on the press now...</div>`;

  try {
    const res = await fetch('/api/capture_v1', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({port, baud, wait_seconds})
    });
    const data = await res.json();
    if (!data.success) {
      el.innerHTML = `<div class="result press_error"><div class="label">Error</div><div class="line">${data.message}</div></div>`;
      return;
    }
    if (!data.captured) {
      el.innerHTML = `<div class="result timeout"><div class="label">No frame captured <span class="status-badge timeout">timeout</span></div><div class="line">${data.message}</div></div>`;
      return;
    }
    const status = data.checksum_ok ? 'ok' : 'checksum_mismatch';
    let html = `<div class="result ${status}">`;
    html += `<div class="label">Quality Data captured <span class="status-badge ${status}">${data.checksum_ok ? 'checksum OK' : 'checksum MISMATCH'}</span></div>`;
    html += `<div class="line">RX: ${data.rx_hex}</div>`;
    if (data.translated) html += renderTranslated(data.translated);
    if (data.note) html += `<div class="line">${data.note}</div>`;
    html += `</div>`;
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = `<div class="result press_error"><div class="label">Request failed</div><div class="line">${err.message}</div></div>`;
  }
}

async function runListen() {
  const port = document.getElementById('port').value.trim();
  const baud = document.getElementById('baud').value;
  const duration = document.getElementById('listenDuration').value;
  const forceRtsDtr = document.getElementById('forceRtsDtr').checked;

  const el = document.getElementById('listenResult');
  el.innerHTML = `<div class="sub">Listening for ${duration}s...</div>`;

  try {
    const res = await fetch('/api/listen', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({port, baud, duration, force_rts_dtr: forceRtsDtr})
    });
    const data = await res.json();
    if (!data.success) {
      el.innerHTML = `<div class="result press_error"><div class="label">Error</div><div class="line">${data.message}</div></div>`;
      return;
    }
    const ls = data.line_state || {};
    const status = data.byte_count > 0 ? 'ok' : 'timeout';
    let html = `<div class="result ${status}">`;
    html += `<div class="label">Listened ${data.duration}s on ${data.port} @ ${data.baud} <span class="status-badge ${status}">${data.byte_count} bytes received</span></div>`;
    html += `<div class="line">Line state: RTS=${ls.rts} DTR=${ls.dtr} CTS(in)=${ls.cts} DSR(in)=${ls.dsr}</div>`;
    html += `<pre class="dump">${data.hex_dump}</pre>`;
    html += `</div>`;
    el.innerHTML = html;
  } catch (err) {
    el.innerHTML = `<div class="result press_error"><div class="label">Request failed</div><div class="line">${err.message}</div></div>`;
  }
}

scanPorts();
loadCommands();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8086, debug=False)
