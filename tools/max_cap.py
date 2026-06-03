#!/usr/bin/env python3
"""
max_cap — set per-load maximum-level cap on a LiteTouch controller.

Uses the CINLL + CGMAX command pair to set the controller's "Maximum level"
parameter for each load group. The cap persists in NVRAM and is enforced
by the controller hardware, so any subsequent CINLL or CSLON command above
the cap is automatically clamped.

Use case: protect older incandescent fixtures from being driven at 100% by
software clients (HomeKit, scripts, etc.). Set the cap to, e.g., 80% once,
and every command above that level gets clamped at the controller without
the client needing to know.

Run with Home Assistant STOPPED (single TCP connection limit).

Usage:
  python3 max_cap.py --host <your-controller-ip> --ping
  python3 max_cap.py --host <your-controller-ip> --cap 90 --test 33
  python3 max_cap.py --host <your-controller-ip> --cap 90 --all
"""

import argparse
import socket
import sys
import time


RECV_TIMEOUT = 3.0
CMD_PACING = 0.1
DEFAULT_SHUTOFF_GROUPS = []

# Response prefixes that mean "this is an actual response", not a notification.
RESPONSE_TAGS = ("RCACK", "RDACK", "RQRES", "RTRES")
# Unsolicited notification prefixes we want to silently consume.
NOTIFICATION_TAGS = ("RLEDU", "REVNT", "RMODU")


def recv_line(sock, timeout):
    """Read bytes until we see a \\r terminator. Returns line (no \\r) or None."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            chunk = sock.recv(64)
            if not chunk:
                return None
            for b in chunk:
                if b == 0x0D:
                    return buf.decode("ascii", errors="replace")
                buf.append(b)
        except socket.timeout:
            continue
    return None


def send_cmd(sock, cmd, timeout=RECV_TIMEOUT, verbose=False):
    """
    Send a command and return the first line that's a genuine response,
    silently dropping any unsolicited notifications that arrive before it.
    """
    sock.sendall((cmd + "\r").encode("ascii"))

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if verbose:
                print(f"    >> {cmd}\n    << <timeout>")
            return None
        line = recv_line(sock, timeout=remaining)
        if line is None:
            if verbose:
                print(f"    >> {cmd}\n    << <no data>")
            return None
        if any(tag in line for tag in RESPONSE_TAGS):
            if verbose:
                print(f"    >> {cmd}\n    << {line!r}")
            return line
        if verbose:
            print(f"    (skipped notification: {line!r})")


def ack_ok(resp, expected_cmd_word):
    return bool(resp) and "RCACK" in resp and expected_cmd_word in resp


def connect(host, port):
    print(f"Connecting to {host}:{port} ...")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5.0)
    try:
        s.connect((host, port))
    except (OSError, socket.timeout) as e:
        print(f"  CONNECTION FAILED: {e}")
        print(f"  Hint: stop Home Assistant first (port {port} single-connection).")
        sys.exit(2)
    print(f"  connected.")
    return s


def quiet_notifications(sock):
    """Stop the controller from broadcasting unsolicited LED/module/event updates."""
    print("Silencing unsolicited notifications (R,SIEVN,0)...")
    sock.sendall(b"R,SIEVN,0\r")
    drain_deadline = time.monotonic() + 0.5
    while time.monotonic() < drain_deadline:
        try:
            sock.settimeout(0.2)
            chunk = sock.recv(256)
            if not chunk:
                break
        except socket.timeout:
            break
    print("  notifications silenced.")


def ping(sock):
    print()
    print("Pinging controller (asking for clock)...")
    r = send_cmd(sock, "R,DGCLK", verbose=True)
    if r and "DGCLK" in r:
        print("  controller is responsive.")
        return True
    print("  no usable response; aborting.")
    return False


def cap_one_group(sock, group_ui, cap):
    g = group_ui - 1  # 0-indexed on the wire
    r1 = send_cmd(sock, f"R,CINLL,{g},{cap}")
    time.sleep(CMD_PACING)
    r2 = send_cmd(sock, f"R,CGMAX,{g}")
    time.sleep(CMD_PACING)
    return ack_ok(r1, "CINLL"), ack_ok(r2, "CGMAX"), r1, r2


def shutoff(sock, off_groups):
    if not off_groups:
        return
    print()
    print(f"Sending CSLOF to shutoff groups {off_groups} ...")
    for g_ui in off_groups:
        g = g_ui - 1
        r = send_cmd(sock, f"R,CSLOF,{g}")
        ok = ack_ok(r, "CSLOF")
        print(f"  {'ok  ' if ok else 'FAIL'}  CSLOF group {g_ui}: {r!r}")
        time.sleep(CMD_PACING)


def main():
    ap = argparse.ArgumentParser(description="Cap LiteTouch max levels per load group.")
    ap.add_argument("--host", required=True, help="LiteTouch controller IP")
    ap.add_argument("--port", type=int, default=10001, help="TCP port (default: 10001)")
    ap.add_argument("--cap", type=int, default=None, help="Max-level cap (1..100)")
    ap.add_argument("--all", action="store_true", help="Sweep every load group --start..--end")
    ap.add_argument("--test", type=int, metavar="GROUP", help="Single group (1..256)")
    ap.add_argument("--start", type=int, default=1, help="First group for --all (default: 1)")
    ap.add_argument("--end", type=int, default=103, help="Last group for --all (default: 103)")
    ap.add_argument("--ping", action="store_true", help="Verify connection only")
    ap.add_argument("--off-groups", type=int, nargs="+", default=DEFAULT_SHUTOFF_GROUPS,
                    help="After sweep, send CSLOF to these group numbers (no defaults; specify per install)")
    ap.add_argument("--yes", action="store_true", help="Skip --all confirmation prompt")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.ping:
        s = connect(args.host, args.port)
        try:
            quiet_notifications(s)
            ok = ping(s)
        finally:
            s.close()
        sys.exit(0 if ok else 1)

    if args.cap is None:
        ap.error("--cap is required (unless --ping)")
    if not (1 <= args.cap <= 100):
        ap.error("--cap must be 1..100")

    if args.test is not None:
        if not (1 <= args.test <= 256):
            ap.error("--test must be 1..256")
        targets = [args.test]
    elif args.all:
        targets = list(range(args.start, args.end + 1))
    else:
        ap.error("specify --test <group> or --all")

    print("=" * 60)
    print(f"LiteTouch max-level cap")
    print(f"  Controller : {args.host}:{args.port}")
    print(f"  Cap        : {args.cap}")
    print(f"  Groups     : {len(targets)}  ({targets[0]}..{targets[-1]})")
    print(f"  Shutoff    : {args.off_groups if args.off_groups else '(none)'}")
    print("=" * 60)

    if args.all and not args.yes:
        print()
        print("Every load group will cycle on->cap->off across the sweep range.")
        if input("Type YES to proceed: ").strip() != "YES":
            print("Aborted.")
            sys.exit(0)

    s = connect(args.host, args.port)
    try:
        quiet_notifications(s)
        if not ping(s):
            sys.exit(1)

        print()
        print("Starting sweep...")
        print()

        ok_groups, fail_groups = [], []
        for g_ui in targets:
            cinll_ok, cgmax_ok, r1, r2 = cap_one_group(s, g_ui, args.cap)
            print(f"  [{g_ui:3d}/{targets[-1]:3d}] "
                  f"{'ok  ' if cinll_ok and cgmax_ok else 'FAIL'}  "
                  f"CINLL={'+' if cinll_ok else '-'}  "
                  f"CGMAX={'+' if cgmax_ok else '-'}"
                  + (f"   CINLL={r1!r} CGMAX={r2!r}" if args.verbose else ""))
            (ok_groups if (cinll_ok and cgmax_ok) else fail_groups).append((g_ui, r1, r2))

        print()
        print(f"Sweep complete: {len(ok_groups)} ok, {len(fail_groups)} fail")

        shutoff(s, args.off_groups)
    finally:
        s.close()

    if fail_groups:
        print()
        print("Failed groups (could be empty/dead scenes — usually harmless):")
        for g, r1, r2 in fail_groups:
            print(f"  group {g}: CINLL={r1!r}  CGMAX={r2!r}")

    print()
    print("Done. Start HA again. The cap is now enforced at the hardware level.")


if __name__ == "__main__":
    main()
