#!/usr/bin/env python3
"""
deploy — install (or update) the load-based LiteTouch HA integration.

Run with Home Assistant STOPPED.

Default (safe) mode:
  1. Timestamped backup of any existing integration folder
  2. Installs/refreshes the integration files into <ha-config>/custom_components/litetouch
  3. Prints instructions for merging your generated litetouch: block by hand

With --in-place, additionally:
  4. Backs up configuration.yaml and the entity registry
  5. Replaces the litetouch: block in configuration.yaml with the generated YAML
  6. Wipes existing litetouch entries from the entity registry (needed when the
     unique_id schema changes, e.g. migrating from a scene-based integration)

To restore the prior state: use the backup paths printed at the end of the run.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime


def main():
    p = argparse.ArgumentParser(
        description="Deploy the load-based LiteTouch HA integration."
    )
    p.add_argument("--ha-config", required=True,
                   help="Path to your Home Assistant config directory (the one containing configuration.yaml and .storage/)")
    p.add_argument("--integration-src", default="./custom_components/litetouch",
                   help="Source directory of integration files to install (default: ./custom_components/litetouch)")
    p.add_argument("--config-yaml", default="./litetouch_config.yaml",
                   help="Generated litetouch: block, used by --in-place (default: ./litetouch_config.yaml)")
    p.add_argument("--backup-dir", default="./backups",
                   help="Directory in which to write timestamped backups (default: ./backups)")
    p.add_argument("--in-place", action="store_true",
                   help="Also splice the generated YAML into configuration.yaml and clear "
                        "stale litetouch entity-registry entries. Default is files-only.")
    args = p.parse_args()

    ha_config_dir = args.ha_config
    integration_dir = f"{ha_config_dir}/custom_components/litetouch"
    config_yaml = f"{ha_config_dir}/configuration.yaml"
    registry = f"{ha_config_dir}/.storage/core.entity_registry"

    # Sanity
    required = [
        (ha_config_dir, "HA config directory"),
        (args.integration_src, "integration source directory"),
    ]
    if args.in_place:
        required += [
            (config_yaml, "configuration.yaml"),
            (registry, ".storage/core.entity_registry"),
            (args.config_yaml, "generated litetouch YAML"),
        ]
    missing = [(path, label) for path, label in required if not os.path.exists(path)]
    if missing:
        for path, label in missing:
            print(f"ERROR: missing {label}: {path}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = f"{args.backup_dir}/litetouch_backup_{ts}"

    # 1. Backups
    os.makedirs(backup_dir, exist_ok=True)
    if os.path.isdir(integration_dir):
        shutil.copytree(integration_dir, f"{backup_dir}/custom_components_litetouch")
    if args.in_place:
        shutil.copy2(config_yaml, f"{backup_dir}/configuration.yaml")
        shutil.copy2(registry, f"{backup_dir}/core.entity_registry")
    print(f"Backups in: {backup_dir}\n")

    # 2. Remove existing integration
    if os.path.isdir(integration_dir):
        print(f"Removing existing integration at {integration_dir}")
        shutil.rmtree(integration_dir)
    os.makedirs(integration_dir)

    # 3. Install new integration
    print(f"Installing integration from {args.integration_src}")
    for fname in os.listdir(args.integration_src):
        src = os.path.join(args.integration_src, fname)
        dst = os.path.join(integration_dir, fname)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            print(f"  + {fname}")

    if not args.in_place:
        print()
        print("=" * 60)
        print("INTEGRATION FILES INSTALLED (files-only mode)")
        print("=" * 60)
        print()
        print("Next steps:")
        if os.path.exists(args.config_yaml):
            print(f"  1. Merge the litetouch: block from {args.config_yaml}")
            print(f"     into {config_yaml}")
        else:
            print("  1. Generate your config with tools/generate_load_config.py,")
            print(f"     then merge its litetouch: block into {config_yaml}")
        print("  2. Start Home Assistant")
        print()
        print("(Or re-run with --in-place to have this script do the YAML splice")
        print(" and entity-registry cleanup for you, with backups.)")
        print()
        print("To restore the prior integration files:")
        print(f"  Use the files in: {backup_dir}")
        return

    # 4. Replace litetouch: block in configuration.yaml
    with open(args.config_yaml) as f:
        new_block = f.read()
    if "litetouch:" not in new_block or "loads:" not in new_block:
        print("ERROR: generated config doesn't contain a litetouch: block with loads:", file=sys.stderr)
        sys.exit(2)

    with open(config_yaml) as f:
        lines = f.readlines()

    start = end = None
    for i, line in enumerate(lines):
        if line.startswith("litetouch:"):
            start = i
            for j in range(i + 1, len(lines)):
                stripped = lines[j].rstrip('\n')
                if stripped and not stripped[0].isspace() and not stripped.startswith('#'):
                    end = j
                    break
            else:
                end = len(lines)
            break

    if start is None:
        # No existing block; append at end.
        start = end = len(lines)

    if not new_block.endswith('\n'):
        new_block += '\n'
    if end < len(lines) and not new_block.endswith('\n\n'):
        new_block += '\n'

    new_lines = lines[:start] + [new_block] + lines[end:]
    with open(config_yaml, 'w') as f:
        f.writelines(new_lines)
    print(f"\nconfiguration.yaml: litetouch: block replaced (lines {start+1}-{end})")

    # 5. Wipe litetouch entries from entity_registry
    with open(registry) as f:
        reg = json.load(f)
    entities = reg["data"]["entities"]
    before = len(entities)
    kept = [e for e in entities if e.get("platform") != "litetouch"]
    reg["data"]["entities"] = kept
    devices = reg["data"].get("devices", [])
    kept_devices = [d for d in devices
                    if not any("litetouch" in str(i).lower() for i in d.get("identifiers", []))]
    reg["data"]["devices"] = kept_devices
    with open(registry, 'w') as f:
        json.dump(reg, f, indent=2)
    print(f"entity_registry: removed {before - len(kept)} litetouch entities, "
          f"{len(devices) - len(kept_devices)} devices")

    print()
    print("=" * 60)
    print("DEPLOYMENT COMPLETE")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Start Home Assistant")
    print("  2. Wait ~60 seconds for full startup")
    print("  3. Verify in HA logs: grep -iE 'litetouch|error' <ha-log>")
    print("  4. Open Apple Home and confirm new tiles appear")
    print()
    print("To restore the prior state:")
    print(f"  Use the files in: {backup_dir}")


if __name__ == "__main__":
    main()
