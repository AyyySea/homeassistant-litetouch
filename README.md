# homeassistant-litetouch

A Home Assistant custom integration for the LiteTouch 5000LC lighting controller (a discontinued but still widely-installed Savant LiteTouch system from the 2000s).

Unlike existing community integrations, this one is **load-based** rather than scene-based — each Home Assistant light entity maps to a single physical dimmer load, and state tracking comes from the controller's `RMODU` module-level broadcasts. The result is flawless 1-to-1 state sync between physical keypads and HomeKit, with no cross-talk between scenes that share LED indicators.

## Hardware requirements

- A LiteTouch 5000LC main controller (or compatible 2000/3000/4000 series with C2000 daughter card)
- TCP/IP connectivity to the controller, typically via a Lantronix XPort or similar serial-to-Ethernet bridge
- The controller's full `.prg` programming dump exported from LiteWare (recommended for reference)

## What this gives you

- One Home Assistant `light` entity per physical dimmer load (typically 50-100 entities for a whole-house install)
- Brightness control 0-100% (the controller's native range)
- Optional hardware-level cap via `CGMAX` (e.g., 80%) to protect older fixtures
- Optional aggregate "scene" entities (e.g., "House Off", "All Outside") exposed via `CSLON`/`CSLOF`
- Robust state tracking: physical keypad presses, scene triggers, and HomeKit commands all converge on accurate state for every entity

## Architecture summary

- The controller exposes loads via numbered scenes (1-103 in a typical install). Each scene fires one or more physical loads.
- Driving a single load cleanly requires a scene that fires *only* that load. Most installs have these "single-load scenes" for ~85% of loads.
- The integration tracks state via `RMODU` events (which report actual load-level changes) rather than `RLEDU` events (which report keypad LED state and exhibit cross-talk).
- Each Home Assistant light entity has a `drive_scene` field that points to the scene used to fire it. Loads with no single-load scene are exposed as read-only (state tracking still works; the entity simply ignores `turn_on`/`turn_off`).

See [`docs/architecture.md`](docs/architecture.md) for the detailed reasoning and [`docs/protocol.md`](docs/protocol.md) for the reverse-engineered command reference.

## Installation

See [`docs/installation.md`](docs/installation.md). High-level flow:

1. Copy `custom_components/litetouch/` into your Home Assistant config's `custom_components/` directory
2. Run `tools/map_scene_loads.py` against your controller to empirically map scenes to physical loads
3. Run `tools/generate_load_config.py` to convert the sweep data into a HA `litetouch:` configuration block
4. Merge the generated block into your `configuration.yaml`
5. Restart Home Assistant

## Tools

| Tool | Purpose |
|---|---|
| `tools/map_scene_loads.py` | Sweeps scenes (default 1-256, the protocol max) firing each via `CSLON` and capturing `RMODU` events. Produces `scene_loads_map.json`, the input for the generator. |
| `tools/generate_load_config.py` | Converts the sweep data into a Home Assistant YAML configuration block, picking the best `drive_scene` per load and disambiguating duplicate names. `--no-clean-names` keeps installer names verbatim. |
| `tools/deploy.py` | Installs the integration files into an HA config directory, with timestamped backups. `--in-place` additionally splices the generated YAML into `configuration.yaml` and clears stale litetouch entity-registry entries. |
| `tools/listen.py` | Listens for `RLEDU` keypad LED events. Useful for diagnosing keypad behavior. |
| `tools/max_cap.py` | Sets the controller's per-load `CGMAX` maximum-level cap (e.g., to protect older incandescent fixtures from full output). |

## Status

This integration is "works in production for one installation." It hasn't been tested across diverse LiteTouch setups. The protocol details should be universal across LiteTouch 5000LC / C2000 hardware, but module addressing and scene numbering will vary with your installer's programming.

## Acknowledgments

This project was developed with extensive assistance from Anthropic's Claude.

## License

MIT — see [LICENSE](LICENSE).
