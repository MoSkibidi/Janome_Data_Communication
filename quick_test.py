"""
Standalone JP4 COM-protocol test script. No Flask, no eventlet, no database -
just opens the serial port, sends a few commands, and prints exactly what
goes out and what (if anything) comes back, in raw hex.

Protocol confirmed against the real JP4 manuals in "C:\\JP4 Command com"
(891817107_COM_control.pdf): $ + address(2) + command(1) + subcommand(1) +
data + SUM(2 hex) + CR. Checksums cross-checked against the manual's worked
examples (B0 -> D3, N0 -> DF, N2 -> E1) and all matched exactly.

Usage:
    python quick_test.py COM3
    python quick_test.py COM3 --baud 19200
"""
import argparse
import sys
import time

import serial

START = b'$'
END = b'\r'
HOST_ADDR = "00"
PRESS_ADDR = "01"


def checksum(body: str) -> str:
    total = 0
    for ch in body:
        total = (total + ord(ch)) & 0xFF
    return f"{total:02X}"


def build_frame(command: str, subcommand: str, data: str = "") -> bytes:
    body = PRESS_ADDR + command + subcommand + data
    return START + body.encode("ascii") + checksum(body).encode("ascii") + END


def send_and_print(ser: serial.Serial, label: str, command: str, subcommand: str, data: str = ""):
    frame = build_frame(command, subcommand, data)
    print(f"\n--- {label} ---")
    print(f"TX ({len(frame)} bytes): {frame!r}")
    print(f"   hex: {frame.hex(' ')}")

    ser.reset_input_buffer()
    ser.write(frame)

    raw = ser.read_until(END)
    if not raw:
        print("RX: <nothing received - timeout>")
        return None

    print(f"RX ({len(raw)} bytes): {raw!r}")
    print(f"   hex: {raw.hex(' ')}")

    if not raw.startswith(START):
        print("   -> does not start with '$', unexpected")
        return None
    if not raw.endswith(END):
        print("   -> does not end with CR, unexpected (may be truncated)")
        return None

    content = raw[1:-1].decode("ascii", errors="replace")
    body, recv_sum = content[:-2], content[-2:].upper()
    calc_sum = checksum(body)
    ok = "OK" if recv_sum == calc_sum else "MISMATCH"
    print(f"   parsed: address={body[0:2]} cmd={body[2:3]!r} subcmd={body[3:4]!r} data={body[4:]!r}")
    print(f"   checksum: received={recv_sum} calculated={calc_sum} [{ok}]")

    if body[2:3] == "e":
        errors = {"0": "Other comm error", "1": "Receive timeout", "2": "Bad command/subcommand", "4": "Checksum error"}
        print(f"   -> PRESS REPORTED ERROR: {errors.get(body[3:4], 'unknown')}")

    return body[4:]


def main():
    parser = argparse.ArgumentParser(description="Minimal raw JP4 COM protocol test")
    parser.add_argument("port", help="Serial port, e.g. COM3")
    parser.add_argument("--baud", type=int, default=38400)  # confirmed against real hardware
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

    print(f"Opening {args.port} @ {args.baud} bps, 8N1, timeout={args.timeout}s ...")
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=args.timeout,
        )
    except Exception as e:
        print(f"FAILED to open port: {e}")
        sys.exit(1)

    # Explicitly force RTS/DTR high. Some cheap USB-serial cables leave these
    # floating, and JP4's COM port pinout uses RTS/CTS - if the press gates
    # reception on a hardware handshake line, this is the difference between
    # total silence and a response.
    try:
        ser.rts = True
        ser.dtr = True
        print(f"RTS={ser.rts} DTR={ser.dtr} CTS(in)={ser.cts} DSR(in)={ser.dsr}")
    except Exception as e:
        print(f"(could not read/set RTS/DTR: {e})")

    time.sleep(1)  # let the port settle, same as the main driver

    try:
        # B0: System info (static, informational) - also proves connectivity,
        # there's no separate "are you there" command in this press's protocol.
        send_and_print(ser, "B0: System Information Request", "B", "0")
        send_and_print(ser, "B0: System Information Request", "B", "0")

        # N0 / N1 / N2: the actual telemetry we care about
        send_and_print(ser, "N0: Instructed Position Request", "N", "0")
        send_and_print(ser, "N1: Encoder Position Request", "N", "1")
        send_and_print(ser, "N2: Current Load Value Request", "N", "2")

    finally:
        ser.close()
        print("\nPort closed.")


if __name__ == "__main__":
    main()
