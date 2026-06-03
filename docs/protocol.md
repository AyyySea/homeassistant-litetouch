# LiteTouch 5000LC Protocol Reference

Reverse-engineered from packet captures, LiteWare 3.8.5 observation, and direct experimentation. Not from official documentation.

## Connection

- TCP port 10001 (typical Lantronix XPort default)
- **Single connection only** — the XPort accepts one TCP client at a time. Disconnect cleanly before another client connects.
- 9600 baud equivalent on the serial side (the XPort handles serial translation)

## Command and response format

All commands and responses are ASCII text terminated by `\r` (carriage return, 0x0D).

Commands start with `R,` (response prefix). Responses come back in the same format with various second-token verbs indicating type.

### Outbound commands (client → controller)

| Command | Format | Description |
|---|---|---|
| `CSLON` | `R,CSLON,N\r` | Turn scene N on at its programmed level. N is **0-indexed** (scene 1 in UI = `N=0` on the wire). |
| `CSLOF` | `R,CSLOF,N\r` | Turn scene N off (sets all loads in the scene to 0). |
| `CINLL` | `R,CINLL,N,L\r` | Fire scene N at level L. L is 0-100 decimal. Used for brightness control. |
| `CGMAX` | `R,CGMAX,MM,C,L\r` | Set the per-load maximum-level cap. `MM` = module address (2 hex digits, leading zero stripped from 4-char form), `C` = channel 0-7, `L` = max level 0-100 decimal. Persists in controller NVRAM. |
| `CTGSW` | `R,CTGSW,KKKB\r` | Simulate a keypad button press. `KKK` = keypad hex address, `B` = button 0-indexed. |
| `SIEVN` | `R,SIEVN,M\r` | Set event-broadcast mask. `M=0` silences, `M=7` enables RMODU + RLEDU + REVNT. |

### Inbound responses (controller → client)

| Response | Format | Description |
|---|---|---|
| `RCACK` | `R,RCACK,CMD\r` | Acknowledgment of a `C*` command. |
| `RDACK` | `R,RDACK,CMD\r` | Acknowledgment of a `D*` command. |
| `RCNAK` | `R,RCNAK,CMD,\r` | Negative acknowledgment (command rejected). |
| `RSACK` | `R,RSACK,CMD\r` | Acknowledgment of a `S*` command (e.g., SIEVN). |
| `RMODU` | `R,RMODU,MMMM,FF,L0,L1,L2,L3,L4,L5,L6,L7\r` | Module-level change broadcast. `MMMM` = 4-char hex module address, `FF` = a marker byte (always `FF` in our captures), `L0..L7` = new level per channel where `-1` means "unchanged". |
| `RLEDU` | `R,RLEDU,KKK,SSSSSSSSSSSSSSSS\r` | Keypad LED state broadcast. `KKK` = 3-char keypad hex address, 16 bits of LED state. |

## Module addressing

Loads are identified by `(module, channel)`:

- **Module address** is a 4-character hex identifier like `0001`, `0007`, `00F1`, `00F8`. Modules `0001`-`0007` are typically 8-channel dimmer modules; modules `00F1`-`00F8` are typically 4-channel modules. (Numbering reflects an internal addressing scheme, not physical sequence.)
- **Channel** is 0-7 for 8-channel modules, 0-3 for 4-channel modules (sometimes 0-7 with unused channels).
- In outbound commands like `CGMAX`, the module address has its leading zeros stripped to 2 chars (`0007` → `07`, `00F5` → `F5`).
- In inbound `RMODU` broadcasts, the full 4-char form is used.

## Scene numbering

- The controller stores up to 256 scenes; a typical install uses 100-103.
- Scene IDs are 1-indexed in LiteWare's UI but **0-indexed on the wire**. Scene 30 in LiteWare is `CINLL,29,L` on the wire.
- Scenes are programmed in LiteWare to fire one or more loads at one or more levels. A "single-load scene" fires exactly one `(module, channel)`. A "multi-load scene" fires several together. An "aggregate scene" (e.g., "All Off", "House Off") fires many loads at once.

## Why DSMLV doesn't work for driving loads

LiteTouch documentation suggests `DSMLV` ("Direct Set Module Level Value") for directly setting a single load. In practice on a 5000LC + C2000:

- `R,DSMLV,MM,C,L\r` returns `R,RDACK,DSMLV\r` (acknowledged)
- The subsequent `R,RMODU,MMMM,FF,-1,-1,...,-1\r` shows all channels as `-1` (unchanged)
- No physical output change occurs

The command appears to write to a programming buffer rather than driving live output. Possibly it's meant to be paired with a separate "apply" command we haven't identified. Either way, **don't use DSMLV for live control**.

The reliable path to drive a specific load is `CINLL` on a scene that fires only that load. This is why the integration's load configuration includes a `drive_scene` field.

## Keypad LED bindings

`RLEDU` broadcasts report the state of *keypad LEDs*, not load output. A single load may have its LED indicator bound to multiple keypad buttons, so a single `RMODU` change can trigger several `RLEDU` broadcasts. **Don't use RLEDU for state tracking** — it produces cross-talk where pressing one button updates the LED indicators for multiple scenes that share LED bindings.

## Useful one-off commands

| Goal | Command sequence |
|---|---|
| Subscribe to all events | `R,SIEVN,7\r` |
| Silence event broadcasts | `R,SIEVN,0\r` |
| Turn scene 30 on at 50% | `R,CINLL,29,50\r` |
| Turn scene 30 off | `R,CSLOF,29\r` |
| Cap module `0007` channel 2 at 80% | `R,CGMAX,07,2,80\r` |
| Simulate pressing button 5 of keypad `015` | `R,CTGSW,0155\r` |

## Behavior quirks

- **Single TCP client.** If Home Assistant is connected, no other client can connect. Stop HA before running diagnostic scripts.
- **Scene 40 "all off" actually turns lights on** in at least one captured installation. The scene's programmed levels override its name. Don't trust scene names without empirical verification.
- **High-address keypads (0xE1-0xE3)** appear in `RLEDU` broadcasts but have no physical hardware. They're internal virtual keypads, likely artifacts of prior Control4 or other third-party integrations.
