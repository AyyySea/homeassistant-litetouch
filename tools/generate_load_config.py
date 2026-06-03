#!/usr/bin/env python3
"""
generate_load_config — turn a scene-load sweep into a Home Assistant litetouch: block.

Inputs:
  - scene_loads_map.json (output of map_scene_loads.py)
  - (optional) an existing configuration.yaml from which to read scene name hints

Output:
  - litetouch_config.yaml — the litetouch: block to merge into your Home Assistant configuration.yaml
  - load_derivation_report.txt — human-readable explanation of how each load got its name

drive_scene logic for each load:
  1. If a single-load scene fires this load -> use that scene's loadid (clean 1:1 drive)
  2. Else if a small (2-4 load) scene fires this load -> use the smallest such scene (side effects)
  3. Else (aggregate-only) -> null (entity is read-only state in HA)
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict, Counter


def title_case(s):
    return ' '.join(w.capitalize() if not w.isupper() else w for w in s.split())


def clean(name):
    """Normalize common abbreviations found in LiteTouch installer programming."""
    if not name:
        return name
    new = name
    new = re.sub(r'\bPerim\b', 'Perimeter', new)
    new = re.sub(r'\bFrpls\b', 'Fireplace', new)
    new = re.sub(r'\bScs\b', 'Sconces', new)
    new = re.sub(r'\bLites\b', 'Lights', new)
    new = re.sub(r'\bSoffet\b', 'Soffit', new)
    new = re.sub(r'\bClos\b', 'Closet', new)
    new = re.sub(r'\bChand\b', 'Chandelier', new)
    new = re.sub(r'\bbrb\b', 'Bedside', new, flags=re.IGNORECASE)
    new = re.sub(r'\bPi$', 'Pillars', new)
    new = re.sub(r'\bOS\b', 'Outdoor', new)
    new = re.sub(r'\bKit\b', 'Kitchen', new)
    new = re.sub(r'\bDown Hall\b', 'Lower Hall', new)
    new = re.sub(r'\bDown Laundr', 'Lower Laundr', new)
    new = re.sub(r'\bDown Bath\b', 'Lower Bath', new)
    new = re.sub(r'\bDown Stairs\b', 'Lower Stairs', new)
    new = re.sub(r'\bUp Hall\b', 'Upper Hall', new)
    return title_case(new)


def parse_rmodu(parts):
    if len(parts) < 3:
        return None
    module = parts[0].upper().zfill(4)
    changes = []
    for ch, lvl_str in enumerate(parts[2:10]):
        try:
            lvl = int(lvl_str)
        except ValueError:
            continue
        if lvl >= 0:
            changes.append((ch, lvl))
    return module, changes


def load_scene_names_from_ha_config(path):
    """Extract scene-id -> name mapping from a Home Assistant configuration.yaml
    that has an existing litetouch: block in the scene-based format."""
    if not path or not os.path.exists(path):
        return {}
    with open(path) as f:
        cfg = f.read()
    names = {}
    for m in re.finditer(
        r'addr:\s*"[^"]+"\s*,\s*name:\s*"([^"]+)"\s*,\s*loadid:\s*"([^"]+)"',
        cfg
    ):
        names[int(m.group(2))] = m.group(1)
    return names


def main():
    p = argparse.ArgumentParser(
        description="Generate a Home Assistant litetouch: configuration block from a scene-load sweep."
    )
    p.add_argument("--scene-map", default="scene_loads_map.json",
                   help="Input JSON from map_scene_loads.py (default: ./scene_loads_map.json)")
    p.add_argument("--ha-config", default=None,
                   help="Optional existing HA configuration.yaml from which to read scene-name hints")
    p.add_argument("--out-yaml", default="litetouch_config.yaml",
                   help="Output YAML file (default: ./litetouch_config.yaml)")
    p.add_argument("--out-report", default="load_derivation_report.txt",
                   help="Output naming report (default: ./load_derivation_report.txt)")
    p.add_argument("--host", default="<your-controller-ip>",
                   help="LiteTouch controller IP to write into the YAML (default: placeholder)")
    p.add_argument("--port", type=int, default=10001,
                   help="LiteTouch controller TCP port (default: 10001)")
    p.add_argument("--no-clean-names", action="store_true",
                   help="Use installer load names verbatim; skip the abbreviation-expansion heuristics")
    args = p.parse_args()

    if not os.path.exists(args.scene_map):
        print(f"ERROR: scene-map file not found: {args.scene_map}", file=sys.stderr)
        print("Run tools/map_scene_loads.py first to generate it.", file=sys.stderr)
        sys.exit(1)

    with open(args.scene_map) as f:
        sweep = json.load(f)

    # Scene names come from HA config if available; otherwise we fall back to "Scene N"
    scene_names = load_scene_names_from_ha_config(args.ha_config)

    # Scene -> set of loads it fires
    scene_to_loads = {}
    for scene_str, data in sweep.items():
        scene = int(scene_str)
        loads = set()
        for ev in data.get("events", []):
            if ev["type"] != "RMODU":
                continue
            parsed = parse_rmodu(ev["parts"])
            if not parsed:
                continue
            module, changes = parsed
            for ch, _lvl in changes:
                loads.add((module, ch))
        scene_to_loads[scene] = sorted(loads)

    load_to_scenes = defaultdict(list)
    for scene, loads in scene_to_loads.items():
        for load in loads:
            load_to_scenes[load].append(scene)

    SINGLE = 1
    SMALL_MAX = 4
    AGGREGATE = 5
    scene_size = {s: len(loads) for s, loads in scene_to_loads.items()}

    load_names = {}
    load_drive_scene = {}
    load_drive_mode = {}
    naming_basis = {}

    for load, scenes in load_to_scenes.items():
        single = sorted([s for s in scenes if scene_size.get(s, 0) == SINGLE])
        small = sorted([s for s in scenes if SMALL_MAX >= scene_size.get(s, 0) >= 2])
        aggregates = [s for s in scenes if scene_size.get(s, 0) >= AGGREGATE]

        chosen_name = None
        drive_scene = None
        drive_mode = None
        basis = "?"

        if single:
            named = sorted([(scene_names.get(s, f"Scene{s}"), s) for s in single],
                           key=lambda x: (len(x[0]), x[0]))
            chosen_name = named[0][0]
            drive_scene = named[0][1]
            drive_mode = "single"
            basis = f"single-load scene {drive_scene}"
        elif small:
            named = sorted([(scene_size[s], scene_names.get(s, f"Scene{s}"), s) for s in small],
                           key=lambda x: (x[0], len(x[1]), x[1]))
            chosen_name = named[0][1]
            drive_scene = named[0][2]
            drive_mode = f"group-{named[0][0]}"
            basis = f"group scene {drive_scene} ({named[0][0]} loads)"
        else:
            module, channel = load
            chosen_name = f"Unnamed {module}-{channel}"
            drive_scene = None
            drive_mode = "readonly"
            basis = f"aggregate-only ({len(aggregates)} aggregate scenes only)"

        load_names[load] = chosen_name if args.no_clean_names else clean(chosen_name)
        load_drive_scene[load] = drive_scene
        load_drive_mode[load] = drive_mode
        naming_basis[load] = basis

    # Disambiguate duplicate names
    name_counts = Counter(load_names.values())
    duplicate_names = {n for n, c in name_counts.items() if c > 1}
    for load in list(load_names.keys()):
        if load_names[load] in duplicate_names:
            module, channel = load
            load_names[load] = f"{load_names[load]} {module}-{channel}"

    aggregates = [
        (s, scene_names.get(s, f"Scene{s}"), scene_to_loads[s])
        for s in scene_to_loads
        if len(scene_to_loads[s]) >= AGGREGATE
    ]

    # Write YAML
    with open(args.out_yaml, "w") as f:
        f.write("# Generated by generate_load_config.py\n")
        f.write("# drive_scene: scene loadid used to fire this load via CINLL\n")
        f.write("#   integer = controllable (single = 1:1, group-N = fires this + N-1 neighbors)\n")
        f.write("#   null    = read-only state (no scene fires this load alone)\n\n")
        f.write("litetouch:\n")
        f.write(f'  host: "{args.host}"\n')
        f.write(f"  port: {args.port}\n")
        f.write("  loads:\n")
        for load in sorted(load_names.keys()):
            module, channel = load
            name = load_names[load]
            drive = load_drive_scene[load]
            mode = load_drive_mode[load]
            drive_str = str(drive) if drive is not None else "null"
            scenes_str = ",".join(str(s) for s in sorted(load_to_scenes[load])[:5])
            f.write(
                f'    - {{module: "{module}", channel: {channel}, '
                f'name: "{name}", drive_scene: {drive_str}}}'
                f'  # mode={mode} scenes={scenes_str}\n'
            )
        f.write("  scenes:\n")
        f.write("    # Aggregate scenes (uncomment to expose in HomeKit)\n")
        for scene_id, name, loads in sorted(aggregates):
            cleaned = name if args.no_clean_names else clean(name)
            f.write(f'    # - {{loadid: {scene_id}, name: "{cleaned}"}}  # affects {len(loads)} loads\n')

    # Report
    with open(args.out_report, "w") as f:
        f.write("LOAD DERIVATION REPORT\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total loads: {len(load_names)}\n")
        mode_counts = Counter(load_drive_mode.values())
        for k, v in sorted(mode_counts.items()):
            f.write(f"  drive_mode {k}: {v}\n")
        f.write("\n\nGROUP-DRIVE LOADS (tapping these fires multiple physical loads together):\n")
        for load in sorted(load_names.keys()):
            if load_drive_mode[load].startswith("group-"):
                module, channel = load
                ds = load_drive_scene[load]
                f.write(f"  {module}_{channel} \"{load_names[load]}\" drive_scene={ds} ({load_drive_mode[load]})\n")
                f.write(f"    also fires: {sorted(scene_to_loads[ds])}\n")
        f.write("\n\nREAD-ONLY LOADS (no drive_scene; controllable only via keypads or aggregates):\n")
        for load in sorted(load_names.keys()):
            if load_drive_mode[load] == "readonly":
                module, channel = load
                f.write(f"  {module}_{channel} \"{load_names[load]}\"  (only fired by aggregates: {load_to_scenes[load]})\n")
        f.write("\n\nLOAD -> NAME / DRIVE_SCENE / BASIS:\n")
        f.write("-" * 70 + "\n")
        for load in sorted(load_names.keys()):
            module, channel = load
            f.write(f"  {module}_{channel} -> \"{load_names[load]}\"  drive={load_drive_scene[load]}  ({naming_basis[load]})\n")

    print(f"Wrote {args.out_yaml}")
    print(f"Wrote {args.out_report}")
    print(f"\nTotal loads: {len(load_names)}")
    for k, v in sorted(mode_counts.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
