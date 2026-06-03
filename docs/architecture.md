# Architecture

The integration's central design choice: **load-based entities tracked via RMODU broadcasts, driven via CINLL on scene mappings**. This document explains why that's the right model and what trade-offs it forces.

## Background: scene-based vs load-based

Prior community integrations (notably `patmann03/custom_components`) model Home Assistant entities as **scenes**: one HA `light` entity per LiteTouch scene (1-103). Pressing the entity sends `CSLON` or `CINLL`; state is tracked by parsing `RLEDU` keypad-LED broadcasts.

This works in principle but has a fundamental cross-talk problem:

- The LiteTouch protocol binds keypad LEDs to *loads*, not scenes. A single load's LED indicator can appear on multiple keypad buttons (the "Master Bath" load might illuminate LEDs on the master keypad, the hallway keypad, and a global control panel).
- When that load changes state, *every* keypad with a bound LED broadcasts `RLEDU` with the new state.
- A scene-based entity matching on those LED states has no way to distinguish "my scene fired this load" from "some other scene fired this load and we happen to share an LED indicator."

The visible symptom: tapping one HomeKit tile flips the state indicator on three or four other tiles, even though only one physical light actually changed.

## The load-based model

Each HA entity maps to one physical `(module, channel)` — the actual dimmer hardware output. State comes from `RMODU` broadcasts, which report *actual load-level changes* with a specific module address and per-channel level array.

Properties of this model:

- **No cross-talk.** `RMODU` for module `00F5` only causes entities listening to module `00F5` to consider an update. Within that module, only the specific channel that changed has a non-`-1` level. State updates are 1-to-1 with physical reality.
- **Truthful state.** If a physical keypad press fires three loads in a scene, exactly those three entities update — not the entities of every keypad whose LEDs share bindings with those loads.
- **Keypad-press parity.** Hardware keypads, software automation, HomeKit taps, and scene triggers all converge on the same truthful state.

## The driving problem: DSMLV doesn't work

A clean load-based model would ideally drive each load directly: "set module `00F5` channel 0 to level 50." LiteTouch documents a command for this purpose, `DSMLV` (Direct Set Module Level Value).

Empirically, on a 5000LC + C2000:
- `DSMLV` is acknowledged (`RDACK`) and a follow-up `RMODU` broadcast is emitted
- But the `RMODU` reports all channels as `-1` (unchanged)
- No physical output occurs

`DSMLV` appears to write to a programming buffer rather than driving live output. Without the official protocol manual we can't determine the correct usage pattern.

## The compromise: drive via single-load scenes

The integration drives each load by firing a *scene* via `CINLL`, where the scene is one that fires only the desired load. Empirically, most installations have such single-load scenes for 80-90% of loads — they're how the installer wired individual keypad buttons.

The generator (`tools/generate_load_config.py`) discovers these mappings by sweeping each scene and recording which loads it fires (via `RMODU` capture). For each load, it picks the best `drive_scene` using this priority:

1. **Single-load scene** (fires exactly this one load) — clean 1-to-1 control
2. **Small multi-load scene** (2-4 loads) — driving this entity also fires its neighbors, but the side effect is consistent with how the original keypad button behaves
3. **None** — load is aggregate-only (fired only by sweeping "all off"-style scenes); entity exposed as read-only

In a representative installation (~87 physical loads, 103 scenes), this typically produces:
- 75 loads with single-load scenes (clean drive)
- 5 loads with small-group scenes (drive with predictable side effects)
- 7 loads aggregate-only (read-only state tracking, no HA-side drive)

## Read-only loads

The 7-10% of loads with no specific scene to fire them are typically loads the original installer never wanted on a dedicated keypad button — e.g., a stair tread strip that's only meant to come on with the "all main floor" or "house off" sweep.

For these:
- The HA entity is created with `drive_scene: null` and `ColorMode.ONOFF`
- State updates correctly via `RMODU` whenever any aggregate fires them
- `turn_on`/`turn_off` calls from HA are no-ops (logged at debug)
- The user can still physically control them via keypad presses or aggregate scenes

If full control over a specific read-only load is needed, the user can program a new single-load scene in LiteWare (using any unused scene ID — the controller stores up to 256) and add it as the entity's `drive_scene`.

## Brightness and the CGMAX cap

`CINLL` takes a level 0-100. Home Assistant entities map their 0-255 brightness range to this directly.

The controller also supports a per-load `CGMAX` cap that clamps any level command to a programmed maximum. This was originally a fixture-protection feature (incandescent bulbs at 100% in a high-ambient-temperature recessed can degrade quickly). The cap is set non-destructively via the `tools/max_cap.py` utility and persists in controller NVRAM.

When the cap is set (e.g., 80%), the user gets the full HomeKit 0-255 slider range, but levels above 80 are clamped at the hardware level. The integration doesn't need to know or do anything special — it sends 0-100 levels and the controller does the clamping.

## State tracking implementation

The `LiteTouchController` runs a background reader thread on its TCP socket. Inbound `RMODU` lines are parsed into `(module, [L0..L7])` and dispatched to Home Assistant via the asyncio signal `litetouch_module_{MMMM}`. Each `LiteTouchLoad` entity subscribes to its module's signal at setup; when notified, it checks if its channel's level changed (non-`-1` and different from current) and updates its state.

This means:
- Physical keypad press → controller emits `RMODU` → reader thread parses → dispatcher fires → all entities listening to that module reconsider their state → exactly one entity updates (the one whose channel changed)
- HomeKit tap → entity calls `set_scene_level` via `CINLL` → controller executes → emits `RMODU` → same path as above → the originating entity (plus any neighbors fired by the same scene) updates

The state-tracking and state-driving paths are deliberately separated: HA commands don't optimistically set state. Everything flows through `RMODU` so HA's view of state always reflects controller reality.

## Limitations

- **`drive_scene` is read at integration startup.** Changing a scene's behavior in LiteWare won't propagate to HA without restarting or re-deploying. Re-sweep with `tools/map_scene_loads.py` after any LiteWare programming change.
- **Aggregate scenes are not auto-exposed.** The generator emits them as commented-out `scenes:` entries in the YAML. Users decide which to enable based on their use case.
- **One TCP connection.** Stop HA before running any diagnostic that talks to the controller directly (`max_cap.py`, `map_scene_loads.py`, `listen.py`).
