# Installation

## Prerequisites

- LiteTouch 5000LC controller reachable on TCP port 10001 (typically via a Lantronix XPort serial-to-Ethernet bridge)
- A static IP for the controller, or a DHCP reservation by MAC. The integration needs a stable address.
- Home Assistant running and able to reach the controller's IP
- (Recommended) LiteWare 3.8.5 installed somewhere with TCP-from-CCU capability, and a `.prg` programming dump exported from your controller for reference
- Installer programming that includes "single-load scenes" (scenes firing exactly one load) for the loads you want to control. Most installs have these for ~85% of loads; the sweep and its derivation report (Steps 2-3) show exactly what your programming supports before you commit to anything

## Step 1: Install the integration

Copy the `custom_components/litetouch/` directory from this repo into your Home Assistant configuration's `custom_components/`:

```bash
cp -r custom_components/litetouch /path/to/homeassistant/config/custom_components/
```

If you're using HACS, you can also add this repo as a custom HACS repository.

### Choosing an install path

- **Manual copy** (above) — simplest; right for a first install.
- **`python3 tools/deploy.py --ha-config <path>`** — refreshes the integration files after pulling repo updates, taking a timestamped backup of the previous files first.
- **`python3 tools/deploy.py --ha-config <path> --in-place`** — full deploy: also splices your generated `litetouch_config.yaml` into `configuration.yaml` and clears stale litetouch entries from the entity registry. Use this when you've re-generated the config, or when migrating from a scene-based integration (whose `unique_id` schema differs). Backs up everything it touches.

Stop Home Assistant before running `deploy.py` in either mode.

## Step 2: Sweep your controller

To know which scenes fire which physical loads, run `tools/map_scene_loads.py` against your controller. This connects, fires each scene in the sweep range (default 1-256, the protocol max) in sequence, and captures `RMODU` broadcasts to record which loads each scene affects.

**Stop Home Assistant first** (single TCP-connection limit). Then:

```bash
python3 tools/map_scene_loads.py \
  --host <your-controller-ip> \
  --port 10001 \
  --output scene_loads_map.json
```

Takes roughly 2.5 minutes per 100 scenes (~6 minutes for the full default range; narrow with `--start`/`--end` if you know your install's scene count). Lights will visibly flick on and off during the sweep. Output is a JSON file containing every scene's load activations.

Every tool takes `--help` for the full flag list.

## Step 3: Generate the HA configuration block

Run `tools/generate_load_config.py` to convert the sweep into a Home Assistant configuration block:

```bash
python3 tools/generate_load_config.py \
  --scene-map scene_loads_map.json \
  --host <your-controller-ip>
```

Useful flags: `--ha-config <path>` reads scene-name hints from an existing scene-based `litetouch:` block if you're migrating; `--no-clean-names` keeps your installer's load names verbatim instead of expanding common abbreviations; `--out-yaml` / `--out-report` change output paths.

Output: `litetouch_config.yaml`, containing a `litetouch:` block with one entry per discovered load and a commented-out `scenes:` section listing aggregates — plus `load_derivation_report.txt`. **Read the report before merging**: it shows which loads got clean single-load drive scenes, which are group-driven (and what their side-effect neighbors are), and which end up read-only.

Review the output. Each entry looks like:

```yaml
- {module: "00F5", channel: 0, name: "Master Bath", drive_scene: 30}
```

The `name` is derived from your installer's original scene naming (read from your HA config or from the `.prg` defaults). Disambiguated duplicates get a `MMMM-C` suffix. Aggregate-only loads get `Unnamed MMMM-C` placeholders.

## Step 4: Merge into your `configuration.yaml`

Open the generated YAML and copy its `litetouch:` and `homekit:` sections into your Home Assistant `configuration.yaml`. The typical structure:

```yaml
litetouch:
  host: "<your-controller-ip>"
  port: 10001
  loads:
    - {module: "0001", channel: 0, name: "Guest Bath Vanity", drive_scene: 4}
    # ... etc
  scenes:
    # - {loadid: 90, name: "House Off"}  # uncomment to expose
    # ...

homekit:
  - advertise_ip: <your HA host IP>
    filter:
      include_domains:
        - light
```

The `homekit:` block exposes everything to Apple Home via a HomeKit Bridge. If you don't need that, omit it.

Alternatively, run `deploy.py --in-place` (see Step 1) to have the splice and entity-registry cleanup done for you.

## Step 5: Restart Home Assistant

After the restart, you should see one Home Assistant `light` entity per physical load. Apple Home (if configured) will show them as tiles after the HomeKit Bridge re-syncs.

## Verification

1. **Tap a HomeKit tile** — the corresponding light should turn on; the tile's state should reflect actual hardware state within ~1 second
2. **Press a physical keypad button** — exactly one tile (per load actually changed) should update in HomeKit; no cross-talk to unrelated tiles
3. **Check HA logs** for any `litetouch` errors:
   ```bash
   grep -iE 'litetouch|error.*loading' /path/to/homeassistant.log
   ```

## Optional: Set a per-load brightness cap

If you have older fixtures that you don't want driven at 100%, use `tools/max_cap.py` to set a hardware-level cap. This persists in controller NVRAM.

```bash
# Stop HA first. Verify connectivity:
python3 tools/max_cap.py --host <your-controller-ip> --ping

# Cap one load group at 80% (the group number is the load's drive_scene):
python3 tools/max_cap.py --host <your-controller-ip> --cap 80 --test 30

# Cap every group in a range (cycles each group on -> cap -> off; lights will flash):
python3 tools/max_cap.py --host <your-controller-ip> --cap 80 --all --start 1 --end 103
```

After capping, the HomeKit brightness slider still goes 0-100%, but any level above 80 is clamped at the controller.

## Troubleshooting

**Lights don't respond at all.** Verify TCP connectivity:
```bash
nc -zv <your-controller-ip> 10001
```
If that fails, the controller's IP or the network path is wrong.

**Lights turn on but state doesn't update.** Most likely cause: `SIEVN` event broadcasts aren't enabled. The integration sends `R,SIEVN,7\r` at startup; if you've manually changed it (e.g., via LiteWare or another tool), reconnect or restart HA.

**Wrong loads turning on when you tap a tile.** Your `drive_scene` mapping is incorrect for that load. Re-sweep with `map_scene_loads.py` after any LiteWare programming change. If the sweep shows the load is in a multi-load scene, the side effect is expected — see [`architecture.md`](architecture.md) for why.

**"Untested integration" warning in HA logs.** Expected and harmless. The integration isn't HACS-published.
