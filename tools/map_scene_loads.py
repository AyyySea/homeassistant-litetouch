#!/usr/bin/env python3
"""
map_scene_loads — sweep every scene and record which physical loads it fires.

For each scene in the range [--start, --end]:
  1. CSLOF baseline (ensure scene is off)
  2. CSLON, capture RMODU module-update broadcasts
  3. CSLOF cleanup

Output: JSON file mapping scene -> list of (module, channel, level) events.

Used as input to generate_load_config.py.

Run with Home Assistant STOPPED. The sweep takes roughly 2.5 minutes per
100 scenes (~6 minutes for the full default 1-256 range; narrow with
--start/--end if you know your install's scene count). Physical lights
will visibly flicker during the sweep.
"""

import argparse
import json
import socket
import sys
import threading
import time


PRE_OFF_WAIT = 0.3
POST_ON_WAIT = 0.8
POST_OFF_WAIT = 0.3


class Reader:
    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()
        self.events = []
        self.lock = threading.Lock()
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        try:
            self.thread.join(timeout=2.0)
        except Exception:
            pass

    def _run(self):
        self.sock.settimeout(0.15)
        while not self.stop_flag:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return
                for b in chunk:
                    if b == 0x0D:
                        line = self.buf.decode("ascii", errors="replace")
                        self.buf = bytearray()
                        with self.lock:
                            self.events.append((time.monotonic(), line))
                    else:
                        self.buf.append(b)
            except socket.timeout:
                continue
            except Exception:
                return

    def drain_since(self, start_ts):
        with self.lock:
            return [(ts, line) for ts, line in self.events if ts >= start_ts]

    def clear(self):
        with self.lock:
            self.events.clear()


def main():
    ap = argparse.ArgumentParser(
        description="Sweep LiteTouch scenes and record which physical loads each fires."
    )
    ap.add_argument("--host", required=True, help="LiteTouch controller IP")
    ap.add_argument("--port", type=int, default=10001, help="TCP port (default: 10001)")
    ap.add_argument("--start", type=int, default=1, help="First scene to sweep (1-indexed)")
    ap.add_argument("--end", type=int, default=256,
                    help="Last scene to sweep (1-indexed, inclusive; default 256 = protocol max)")
    ap.add_argument("--test", type=int, help="Sweep a single scene only (overrides --start/--end)")
    ap.add_argument("--output", default="scene_loads_map.json",
                    help="Output JSON path (default: ./scene_loads_map.json)")
    ap.add_argument("--cleanup-scene", type=int, default=None,
                    help="After the sweep, send CSLOF for this scene as final cleanup "
                         "(useful if you have a known 'house off' scene). Optional.")
    args = ap.parse_args()

    scenes = [args.test] if args.test else list(range(args.start, args.end + 1))

    print(f"Connecting to {args.host}:{args.port}...")
    sock = socket.socket()
    sock.settimeout(5.0)
    try:
        sock.connect((args.host, args.port))
    except Exception as e:
        print(f"  FAILED: {e} (stop HA first)")
        sys.exit(2)
    print("  connected.")

    # SIEVN=7 enables module updates (RMODU) and all other events
    print("Enabling SIEVN=7 (all events including module-level changes)...")
    sock.sendall(b"R,SIEVN,7\r")
    time.sleep(0.4)
    sock.settimeout(0.3)
    try:
        sock.recv(2048)
    except socket.timeout:
        pass

    reader = Reader(sock)
    reader.start()

    print(f"Sweeping {len(scenes)} scenes...")
    print()

    results = {}
    raw_sample = None

    try:
        for scene_ui in scenes:
            scene_wire = scene_ui - 1

            sock.sendall(f"R,CSLOF,{scene_wire}\r".encode())
            time.sleep(PRE_OFF_WAIT)

            reader.clear()
            on_start = time.monotonic()
            sock.sendall(f"R,CSLON,{scene_wire}\r".encode())
            time.sleep(POST_ON_WAIT)

            events = reader.drain_since(on_start)

            if raw_sample is None and events:
                raw_sample = [line for _, line in events]
                print(f"  Sample raw events from scene {scene_ui}:")
                for line in raw_sample[:10]:
                    print(f"    {line!r}")
                print()

            loads_observed = []
            for ts, line in events:
                if "RMODU" in line:
                    parts = line.strip().split(",")
                    if len(parts) >= 4:
                        loads_observed.append({"type": "RMODU", "parts": parts[2:]})
                elif any(t in line for t in ("RLDLV", "RMLDL", "RCMOD", "RSMLV")):
                    parts = line.strip().split(",")
                    loads_observed.append({"type": parts[1], "parts": parts[2:]})

            sock.sendall(f"R,CSLOF,{scene_wire}\r".encode())
            time.sleep(POST_OFF_WAIT)

            results[scene_ui] = {
                "events": loads_observed,
                "raw_event_count": len(events),
            }

            event_summary = (f"{len(loads_observed)} module events"
                             if loads_observed
                             else f"{len(events)} raw, no parseable module events")
            print(f"  scene {scene_ui:3d}  ->  {event_summary}")

        if args.cleanup_scene is not None:
            print()
            print(f"Final cleanup: CSLOF on scene {args.cleanup_scene}...")
            sock.sendall(f"R,CSLOF,{args.cleanup_scene - 1}\r".encode())
            time.sleep(0.5)

        # Restore SIEVN to 4 (standard event mask for HA usage)
        print("Restoring SIEVN=4...")
        sock.sendall(b"R,SIEVN,4\r")
        time.sleep(0.3)

    finally:
        reader.stop()
        sock.close()

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {args.output}")

    have_loads = sum(1 for r in results.values() if r["events"])
    print(f"\n{have_loads} of {len(results)} scenes have parseable module events.")


if __name__ == "__main__":
    main()
