#!/usr/bin/env python3
"""
listen — print RLEDU keypad-LED events from the LiteTouch controller.

Connects to the controller, enables LED notifications, and prints every
RLEDU event it receives with a timestamp. Useful for diagnosing keypad
behavior or building a keypad/button map.

Run with Home Assistant STOPPED (single TCP connection limit on port 10001).
Press Ctrl-C to stop.
"""

import argparse
import socket
import sys
import time


def main():
    p = argparse.ArgumentParser(description="Listen for RLEDU events from a LiteTouch controller.")
    p.add_argument("--host", required=True, help="LiteTouch controller IP")
    p.add_argument("--port", type=int, default=10001, help="TCP port (default: 10001)")
    args = p.parse_args()

    print(f"Connecting to {args.host}:{args.port}...")
    sock = socket.socket()
    sock.settimeout(5)
    try:
        sock.connect((args.host, args.port))
    except Exception as e:
        print(f"FAILED: {e}  (stop HA first)")
        sys.exit(1)
    print("connected.")

    sock.sendall(b"R,SIEVN,4\r")
    time.sleep(0.3)
    sock.settimeout(0.1)
    try:
        sock.recv(2048)
    except socket.timeout:
        pass

    print()
    print("=" * 60)
    print("Listening for RLEDU events. Press physical buttons.")
    print("Press Ctrl-C when done.")
    print("=" * 60)
    print()

    buf = bytearray()
    start = time.monotonic()
    try:
        while True:
            try:
                chunk = sock.recv(2048)
                if not chunk:
                    print("(connection closed)")
                    break
                for b in chunk:
                    if b == 0x0D:
                        line = buf.decode("ascii", errors="replace")
                        buf = bytearray()
                        elapsed = time.monotonic() - start
                        if "RLEDU" in line:
                            print(f"  [{elapsed:6.2f}s]  {line}")
                        elif line.startswith("R,"):
                            print(f"  [{elapsed:6.2f}s]  (other) {line}")
                    else:
                        buf.append(b)
            except socket.timeout:
                continue
            except KeyboardInterrupt:
                break
    except KeyboardInterrupt:
        pass
    finally:
        print()
        print("disconnecting...")
        sock.close()


if __name__ == "__main__":
    main()
