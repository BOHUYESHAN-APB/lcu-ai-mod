# LCU Vanilla Capability Matrix

Updated: 2026-07-19

This document defines the intended vanilla Minecraft capability surface for an
LCU headed body. It is the implementation checklist for deterministic Java
primitives and the composition boundary for Python tasks and agents.

## Reference Baseline

The design is informed by:

- Mineflayer core and plugins for complete primitive APIs, server-backed events,
  inventory windows, pathfinding, collection, combat, equipment, and food.
- Mindcraft for model-visible commands, reactive modes, and skill/world separation.
- Baritone for navigation goals, movement costs, typed transitions, arbitration,
  safe cancellation, and path failure diagnostics.
- AltoClef for hierarchical tasks, acquisition strategies, container caches,
  survival preemption, workstations, farming, portals, and bounded recovery.
- Voyager for a small trusted primitive library with higher-level composed skills.
- TouhouLittleMaid for typed Brain memories, activity groups, registered work-mode
  behavior packages, bounded candidate scanning, cached reachability, farming phases,
  inventory routing, target cleanup, and randomized retry cooldowns.
- Mojang-mapped 1.21.1 menu classes and NeoForge menu synchronization for
  workstation slot layouts and completion evidence.

LCU must not copy arbitrary generated-code execution, unrestricted raw packet
access, command cheating, or text-only success detection from reference projects.
It also must not copy TouhouLittleMaid's server-authority operations such as direct
block mutation, direct item-handler insertion/extraction, Mob navigation, teleport
recovery, health mutation, or server-side attack execution into a client player body.

## Body Blackboard And Activities

TouhouLittleMaid demonstrates a useful Java-side coordination shape: core behaviors
remain installed while one scheduled activity selects a behavior package. LCU should
adapt this into a small typed body blackboard rather than using Minecraft's
`Brain<LivingEntity>` directly.

Required typed records:

```text
active_operation
navigation_goal
walk_target
look_target
block_interaction_target
entity_interaction_target
attack_target
item_target
menu_transaction
recovery_checkpoint
```

Every record carries owner operation ID, observation revision, creation/expiry tick,
validation state, and required effect channels. Activity changes atomically clear
records owned by the displaced activity.

Initial activities:

- `CORE`: disconnect, death, control transfer, state refresh, operation outcomes;
- `SAFETY`: fire/lava/drowning/fall/threat escape;
- `COMMAND`: admitted user or SDK operation;
- `WORK`: farming, logistics, collection, building, animal care;
- `FOLLOW_GUARD`: persistent player-relative behavior;
- `REST`: idle/home/sleep behavior;
- `OPTIONAL_AUTONOMY`: low-priority local behavior.

Only one owner may hold each of locomotion, gaze, hands, inventory, menu, break,
place, or attack effects. Higher-priority activities suspend or cancel according to
the operation contract rather than allowing multiple tick loops to write controls.

## Behavior Phase Pattern

Reusable behaviors follow:

```text
sense -> validate -> claim -> navigate -> interact -> verify -> clear
```

- Sensors produce bounded immutable candidate snapshots at staggered intervals.
- Validation checks permissions, range, reachability, inventory capacity, current
  world/menu revision, and whether another operation already claimed the target.
- Claim writes typed blackboard targets and effect ownership.
- Navigation targets an acceptable interaction region, not the object center.
- Interaction sends one bounded vanilla action or menu transaction.
- Verification waits for authoritative block/entity/inventory/menu evidence.
- Clear removes target and ownership records on success, invalidation, cancellation,
  activity transition, or terminal failure.

Failed candidates receive an expiring reasoned rejection rather than being selected
again every scan. Expensive scans are staggered and reuse one reachability map or
multi-goal search across candidates where possible.

## Capability Layers

### Layer 1: Observation

Read-only, server-backed facts:

- self state, inventory, equipment, effects, health, hunger, air, dimension;
- blocks, block states, block entities, loaded regions, hazards, light;
- entities, players, identity, equipment, movement, effects, lifecycle;
- open menu identity, menu type, container ID/state ID, slots, carried item;
- recipes, workstation properties, merchant offers, furnace/brewing progress;
- path status, active operation, target, blocker, terminal outcome;
- POIs and timestamped container-content snapshots.

Observation APIs update the world model. They do not mutate the body and do not
enter conversation history as repeated raw snapshots.

### Layer 2: Addressable Primitives

Small deterministic operations with explicit targets:

- movement input, navigation goals, look/target, jump, sprint, sneak;
- select slot, equip, move/swap/split/drop inventory items;
- break/place/use a block at a position and face;
- attack/use an entity by current entity ID and durable UUID where available;
- open/read/click/close a specific menu transaction;
- place recipe, select workstation option, load inputs/fuel, take output;
- consume food/item, sleep/wake, mount/dismount, send chat.

Every mutating primitive requires preconditions, an operation ID, a deadline,
cancellation, structured failure, and an observed postcondition.

### Layer 3: Stateful Operations

Java-owned state machines composed from primitives:

- navigate to an interaction region;
- mine one block and verify its drop/inventory effect;
- place one block with support, reach, face, and state verification;
- transfer an exact quantity through a menu;
- craft/process one recipe operation;
- eat one selected item;
- sleep in one selected bed;
- trade one selected merchant offer;
- attack one target through one cooldown cycle.

These operations own tick timing, screen identity, packet sequencing, and
authoritative completion. They do not decide long-horizon goals.

### Layer 4: Composed Tasks

Python TaskCoordinator and specialists compose operations into objectives:

- acquire an `ItemTarget` from inventory, indexed storage, drops, mining,
  crafting, processing, mobs, crops, trading, or another dimension;
- craft a tool through a recursive dependency graph;
- retrieve and deliver an item to a player;
- harvest and replant a crop area;
- equip the best policy-approved gear;
- travel through a portal or sleep through the night;
- guard, follow, patrol, build, or maintain a resource reserve.

Models select objectives and resolve ambiguous tradeoffs. They never replace
the deterministic primitive completion contract.

## Common Operation Contract

Each operation must expose:

```text
operation_id
command
owner_task_id
input
effects
preconditions
started_tick
deadline_tick
phase
progress
expected_postcondition
last_authoritative_update
terminal_status
failure_code
evidence
```

Lifecycle:

```text
accepted -> progress* -> exactly one succeeded | failed | cancelled
```

Sending a packet or clicking a slot is not success. Typical evidence is:

- movement: server-backed position satisfies the navigation goal;
- mining/placing: block state changes as expected;
- entity action: entity state, damage, mount, or lifecycle changes;
- inventory: source/destination slots and carried item reconcile;
- crafting: expected output enters inventory and inputs are consumed;
- processing: output slot changes and expected output is acquired;
- eating: food/health/effects and item count change;
- sleeping: player sleeping pose/state, then wake/day transition;
- trading: output acquired, payment consumed, and offer state updated.

## Current Coverage Matrix

Status values: `implemented`, `partial`, `missing`, `unsupported`.

| Domain | Capability | Status | Main gap |
|---|---|---:|---|
| Lifecycle | Wire capability discovery | implemented | Schema/version compatibility enforcement |
| Lifecycle | Accepted/progress/outcome/cancel | partial | Core move/mine/craft/collect/eat paths support outcomes; remaining operations must migrate |
| Movement | Coordinate navigation | implemented | Only one coordinate goal shape |
| Movement | Radius/adjacent/any/flee/follow goals | partial | Formal `NavigationGoal` abstraction missing |
| Movement | Terrain-modifying paths | missing | Break/place transitions and policy costs |
| Movement | Swim/climb/vehicle/portal | missing | Separate locomotion modes |
| Targeting | Look at coordinate/entity | implemented | Durable target tracking and visible face result |
| Blocks | Break current/addressed block | implemented | Headed-body verification pending for addressed mining |
| Blocks | Place requested block at position | partial | Current implementation depends on client hit result |
| Blocks | Use addressed block | implemented | Headed-body verification pending |
| Entities | Use addressed entity | implemented | Durable UUID validation |
| Entities | Attack addressed entity | implemented | Hit verification remains a future stateful combat operation |
| Inventory | Observe inventory/equipment | implemented | Components, reservations, capacity diagnostics |
| Inventory | Select hotbar | implemented | Dynamic tool and Skill are advertised |
| Inventory | Equip by item and slot | partial | Main-hand hotbar selection implemented; inventory swaps/armor/offhand remain |
| Inventory | Exact move/swap/split/drop | partial | Only quick-move and coarse drop behavior |
| Menus | Read scoped slots | implemented | Menu state ID and carried item not exposed |
| Menus | Quick transfer with menu ID | implemented | Exact quantity and partial transfer |
| Menus | Generic serialized click | partial | Pickup/quick-move/swap/throw implemented; drag and verified transaction operation remain |
| Storage | Indexed chest/barrel snapshots | implemented | Persistence and richer slot/components metadata |
| Storage | Vanilla double chest identity | implemented | Headed-body verification pending |
| Storage | Iron Chests/Sophisticated Storage | partial | Dedicated adapters for auxiliary slots |
| Storage | Ender chest/shulker/entity storage | missing | POI and menu adapters |
| Recipes | Recursive crafting graph | implemented | Headed-body route verification pending |
| Recipes | Crafting table operation | partial | Full grid cleanup and exact batch control |
| Processing | Furnace/blast/smoker | partial | Explicit input/fuel adapter and matching busy state |
| Processing | Stonecutter | partial | Menu/slots/button primitive exposed; verified result transaction remains |
| Processing | Smithing | partial | Menu/semantic slots exposed; verified input/result transaction remains |
| Processing | Brewing | partial | Menu/semantic slots exposed; progress and verified bottle transaction remain |
| Processing | Anvil/grindstone | partial | Menu/semantic slots exposed; rename/XP/result transaction remains |
| Processing | Loom/cartography/enchanting | partial | Menu/slots/button primitive exposed; verified result transaction remains |
| Food | Eat suitable hotbar food | partial | Full inventory selection and food policy |
| Equipment | Best mining tool | partial | Hotbar-only, no durability/reservation policy |
| Survival | Sleep/wake | unsupported | Bed selection, navigation, state verification |
| Social | Send chat | implemented | Structured delivery/echo distinction |
| Villagers | Observe/trade offers | unsupported | Merchant entity/menu adapter |
| Combat | One melee attack | partial | Cooldown, target lifecycle, verified hit result |
| Combat | Sustained combat/defense | missing | Policy, pursuit, shield/projectiles, terminal state |
| Farming | Harvest/replant | missing | Mature-state filters and restoration ledger |
| Collection | Storage/drop/block acquisition | partial | General source registry, hazards, capacity, tool durability |
| Safety | Stop/disarm/disconnect | implemented | Per-operation cancellation outcomes |
| Safety | Hazard escape chains | missing | Priority arbiter and deterministic reflex tasks |

## Required Java Primitives

### P0: Contract And Ownership

- `cancel_operation(operation_id)`
- exactly one `outcome` event for every accepted operation;
- one authoritative tool manifest shared with Python validation;
- `active_operation` state with owner, phase, target, deadline, blocker;
- effect arbitration for `body.move`, `camera.move`, `world.break`,
  `world.place`, `world.interact`, `entity.attack`, `inventory.ui`, and
  `inventory.transfer`.

Implemented locally in the current development batch:

- explicit Wire `outcome` messages;
- `cancel_operation(operation_id)` and correlated backend cancellation;
- durable-run outcome persistence for succeeded/failed/cancelled;
- terminal outcomes for coordinate navigation, addressed/current mining, crafting,
  collection, and eating;
- compatibility progress events retained for the previously deployed body.

### P1: Addressable World And Inventory

- `navigate(goal, policy)` with block/radius/adjacent/any/flee/follow goals;
- `target_block(position, face?)` and `target_entity(entity_id)`;
- `break_block(position, face, tool_policy)`;
- `place_block(position, item_target, face, placement_state?)`;
- `use_block(position, face, hand)`;
- `attack_entity(entity_id, attack_mode)`;
- `select_hotbar(slot)`;
- `equip_item(item_target, destination)`;
- `inventory_click(container_id, slot, click_type, button, expected_revision)`;
- `move_item(source, destination, quantity)` and exact `drop_item`;
- menu state including type, ID, state ID, carried item, and scoped slots.

Implemented locally in the current development batch:

- `attack_entity(entity_id)` now resolves the supplied entity;
- `mine_block_at(x,y,z,face)` and `interact_block_at(x,y,z,face)`;
- `select_hotbar`, main-hand `equip_item`, and exact registry/tag `drop_item`;
- `get_state`, `get_inventory`, `get_container`, and live `get_recipes`;
- validated `inventory_click` for pickup, quick-move, swap, and throw;
- `container_button` and `place_recipe` primitives;
- menu class, adapter, state ID, carried item, and semantic slot roles.

### P2: Vanilla Menu Adapters

Each adapter owns menu validation, slot layout, selection/data properties,
input placement, output extraction, completion, timeout, and cleanup.

| Adapter | Key slots/data |
|---|---|
| Crafting table | result 0, grid 1-9 |
| Furnace/blast/smoker | input 0, fuel 1, result 2, lit/cook data |
| Stonecutter | input 0, result 1, selected recipe data/button |
| Smithing | template 0, base 1, addition 2, result 3 |
| Brewing stand | bottles 0-2, ingredient 3, fuel 4, brew/fuel data |
| Anvil | inputs 0-1, result 2, level cost, rename packet |
| Grindstone | inputs 0-1, result 2 |
| Loom | banner 0, dye 1, pattern 2, result 3, selected pattern |
| Cartography | map 0, material 1, result 2 |
| Enchanting | item 0, lapis 1, costs/clues/seed, option button |
| Merchant | payment 0-1, result 2, offers/select-trade packet |
| Shulker/ender chest | scoped storage slots and transfer reconciliation |

### P3: Core Stateful Operations

- `acquire_item(ItemTarget, count, source_policy)`;
- `craft_recipe(recipe_id, operations)`;
- `process_recipe(recipe_id, operations, station_policy)`;
- `withdraw_item(container_ref, ItemTarget, count)`;
- `deposit_item(container_ref, ItemTarget, count)`;
- `eat_item(ItemTarget)`;
- `sleep_in_bed(block_pos)`;
- `trade_offer(entity_id, offer_index, count)`;
- `harvest_and_replant(region, crop_policy)`;
- `attack_until(entity_id, stop_condition)`.

### P4: Work-Mode Behavior Packages

LCU's equivalent of TouhouLittleMaid `IMaidTask` is a registered work-mode manifest:

```text
mode_id
required_capabilities
allowed_activities
claimed_effects
sensors
candidate_predicates
operation_factory
entry_setup
interruption_policy
cleanup
configuration_schema
```

Initial packages should cover farming, pickup/logistics, follow, guard, combat,
animal care, sleep/home, and resource collection. A package contributes sensors
and deterministic operation factories; it does not directly write movement or
inventory state.

## Farming Transaction

Adapt TouhouLittleMaid's separation of crop recognition, movement, harvest, and
planting, while replacing direct server mutation with client operations:

1. Snapshot available seeds and free inventory capacity.
2. Scan bounded farmland/crop candidates with crop adapters.
3. Require maturity, headroom, policy permission, and reachable interaction pose.
4. Claim the farmland/crop pair and navigate.
5. Revalidate crop state and target revision at arrival.
6. Harvest through addressed break/use and verify crop/drop/inventory change.
7. Reserve/select a compatible seed.
8. Plant through addressed block use/place and verify the new crop state.
9. Pick up drops or route overflow to indexed storage.
10. Clear the claim and record restoration completion.

Crop adapters define seed tags, mature-state predicates, harvest style, substrate,
planting action, and postcondition. Initial adapters: standard `CropBlock`, nether
wart, cocoa, sweet berries, sugar cane, cactus, melon/pumpkin stems, and mod-provided
handlers registered through an extension boundary.

## Item Target Model

One item parameter must support:

```text
exact registry ID
item tag
ordered alternatives
required count
component/durability constraints
protected/reserved quantity
```

Examples:

```text
minecraft:iron_pickaxe
#minecraft:logs
#minecraft:planks
#lcu:wood
any_of(minecraft:coal, minecraft:charcoal)
```

Acquisition source order should normally be:

1. usable inventory and equipment slots;
2. fresh indexed storage;
3. nearby dropped items;
4. directly collectible blocks/entities/crops;
5. crafting and workstation recipes;
6. trading or dimension-specific sources;
7. bounded exploration/reconnaissance;
8. structured failure.

## Navigation Model

Replace single-coordinate navigation with goals:

- exact block, radius, X/Z radius, Y level;
- interaction adjacency and visible-face goals;
- follow entity with distance band;
- any-of and all-of composite goals;
- flee from entities/positions;
- place/break approach goals;
- dimension/portal transition goals.

Movement policy must declare whether the body may sprint, jump, parkour, swim,
fall, break, place scaffolding, open doors, enter hazards, or leave a protected
region. Paths report `success`, `partial`, `no_path`, `stuck`, `target_changed`,
`chunk_unavailable`, `missing_tool`, `missing_scaffold`, or `unsafe`.

## Implementation Order

1. Fix operation lifecycle and tool-contract mismatches before adding commands.
2. Add addressable block/entity/inventory primitives and generic menu transactions.
3. Convert crafting, storage, mining, eating, and follow to the common operation model.
4. Add furnace-family adapters, then stonecutter, smithing, brewing, and merchant.
5. Add sleep, equipment, exact drop/delivery, farming, and combat composition.
6. Add terrain-modifying navigation, portals, vehicles, and broader survival chains.
7. Persist tested composed workflows in Python only after primitive postconditions
   and regression tests are stable.

## Acceptance Rule

A capability is not `implemented` merely because Java has a switch case. It is
implemented only when:

- its schema is advertised and reconciled;
- preconditions and conflicts are checked;
- progress and exactly one terminal outcome are emitted;
- cancellation is correlated to the operation;
- authoritative postconditions are verified;
- failure codes are structured and bounded;
- automated tests cover success, timeout, stale target/menu, cancellation, and
  server correction where applicable;
- the relevant headed-body scenario has passed.
