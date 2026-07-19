# Server Policy And Anti-Cheat Safety

LCU cannot guarantee admission to a public server. A technically valid vanilla action may still
violate rules against bots, macros, AFK avoidance, combat automation, inventory automation, or
enhanced telemetry. Check the target server's rules and obtain permission before enabling a
`body_client`.

## Category Controls

The generated `config/lcumod-common.toml` separates capabilities so a private-world developer
does not need to disable the whole body to change one policy boundary.

| Option | Default | Category |
|---|---:|---|
| `allowMovementAutomation` | `true` | Explicit movement, follow, jump, and movement controls |
| `allowWorldAutomation` | `false` | Breaking, placement, block/entity interaction, collection |
| `allowInventoryAutomation` | `false` | Item use, crafting, equipment, and menu mutation |
| `allowAutomatedCombat` | `false` | Attack commands and autonomous hostile attacks |
| `allowChatAutomation` | `true` | Automated Minecraft chat output |
| `enableAutonomousBehaviors` | `false` | Unattended Java survival/wander behavior |
| `enableActivitySignals` | `false` | Anti-AFK movement/look pulses |
| `reportProgrammaticActivity` | `false` | WATUT activity reporting |
| `runInBackground` | `false` | Continue while Minecraft is unfocused |
| `autoRespawn` | `false` | Automatic respawn packets |
| `collectSurroundings` | `false` | Nearby player/entity/block/storage telemetry |

`stop_all`, cancellation, control fencing, and read-only self state remain available when action
categories are disabled. Existing config files retain explicit old values, so review them after
upgrading instead of assuming new defaults replaced persisted settings.

## Suggested Profiles

For a public server that permits limited assistance, begin with the defaults and disable movement
or chat too if its rules prohibit them. Do not enable anti-AFK, combat, broad telemetry, background
execution, schedules, or unattended behavior without explicit permission.

For a private server or development world, categories may be enabled independently. Enabling a
category changes policy admission only; it does not relax protocol-safety checks.

## Non-Configurable Safety Floor

LCU always limits the wire backlog and executes at most one queued command per client tick. Jump
requires ground contact. Attack requires the real crosshair entity, line of sight, and a recovered
attack cooldown. Coordinate block actions require the real unobstructed crosshair block and face;
continuous digging stops when that target is lost. Client-only inventory mutation and unconfirmed
item dropping are rejected instead of being exposed as permissive options.

These checks are not anti-cheat bypasses and must not be weakened to imitate human input. Live
acceptance still needs a test account and server-owner-approved test environment. Validate movement
corrections, rotation/action ordering, mining cancellation, menu acknowledgement, queue bursts, and
disconnect behavior. A successful test on one anti-cheat product or configuration does not certify
another server.

## Remaining Restrictions

Automated crafting and workstation flows still need one-click-per-acknowledged-menu-revision
sequencing before they should be enabled on strict public servers. Resource scanning and navigation
also require headed testing against latency and server corrections. Keep the corresponding category
disabled where those risks are unacceptable.
