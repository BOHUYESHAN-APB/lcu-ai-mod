# Feature One: Real Player Body

## Scope

Feature one controls an authenticated Minecraft player already accepted by the
server. It does not create a `FakePlayer`, register a mob, or grant additional
world permissions. Every game effect must be produced through an ordinary player
action and accepted by the server.

Feature two may later provide a server-side fake-player body. It should reuse the
Python task and skill contracts through another body adapter, not change feature
one's execution model.

```text
Python objective, task graph, policy, memory
                    |
          typed body capability protocol
                    |
       +------------+-------------+
       |                          |
real_player_client          fake_player_server
Feature one                 Feature two (later)
```

## Authority Model

The real player already has an entity identity, inventory, hunger, experience,
claims, teams, advancements, and server permissions. No maid-owner or fake-player
permission layer is required.

The client is not authoritative over world state. Java must not directly set a
block, manufacture a drop, change a server inventory, heal the player, or declare
an action successful from a local animation. It sends vanilla movement, attack,
use-item, use-on-block, and menu actions, then verifies the resulting server state.

## Runtime Responsibilities

### Java client

Java owns mechanics that require current Minecraft state or client-thread access:

- capture authoritative observations and stable target identities;
- drive movement, look, hotbar, hands, mining, block use, and menus;
- calculate paths and reachable interaction positions;
- arbitrate local safety and effect ownership;
- execute one bounded operation with deadline and cancellation;
- suspend and resume resumable work without losing its logical objective;
- verify effects from block, inventory, health, status-effect, menu, and position
  changes received from the server;
- emit exactly one terminal outcome for each accepted operation.

Java does not choose long-term goals, call an LLM, invent missing materials, or
silently turn one requested operation into an unrelated workflow.

### Python backend

Python owns long-horizon and durable behavior:

- understand player intent and emit typed skill proposals;
- authorize, deduplicate, prioritize, and persist task runs;
- compose primitive operations into collection, crafting, farming, and delivery;
- select targets from revisioned observations;
- classify failures and choose bounded retries or alternate plans;
- maintain player, place, task, recipe, and experience memory;
- trigger model calls only at decision boundaries;
- evaluate task completion from authoritative observations, never model claims.

### Server

The server remains the final authority for movement correction, interaction reach,
block changes, drops, inventory, damage, healing, claims, and command permissions.

## Java Operation Contract

Before expanding autonomous behavior, body actions should share one operation
kernel with these fields:

- operation ID and body ID;
- operation kind and typed arguments;
- claimed effects such as `body.move`, `hotbar.select`, `hand.use`,
  `world.break`, or `inventory.transfer`;
- accepted observation revision and target identity;
- current phase, deadline, retry budget, and cancellation state;
- suspend/resume checkpoint;
- expected postcondition and collected evidence;
- one terminal `succeeded`, `failed`, or `cancelled` outcome.

Safety may temporarily claim effects from a resumable operation. It must not erase
the operation ID or objective unless the operation is unsafe to resume. A user stop,
disarm, policy revocation, dimension change, or unrecoverable target loss terminates
the operation explicitly.

## Feature-One Capability Order

### Phase 1: body reliability

1. Stable arm, disarm, disconnect, death, and background semantics.
2. Persistent player following with UUID/name identity, distance hysteresis,
   moving-target replanning, target-loss timeout, and safety suspension.
3. Reliable eating with inventory-to-hotbar transfer, selected-slot synchronization,
   vanilla use, and item-count/hunger/absorption/effect evidence.
4. Equipment selection that obeys inventory policy and does not steal a slot or item
   reserved by foreground work.
5. Navigation outcomes based on progress, bounded replanning, live collision checks,
   and explicit failure reasons.

### Phase 2: verified world primitives

1. `inspect_block` and bounded `scan_blocks` return block-state properties, revision,
   distance, and visibility.
2. Addressed look and reachable interaction-position navigation.
3. Verified `break_block_at`, `use_block_at`, and `place_block_at` operations.
4. Inventory reservations, free-capacity diagnostics, and selected-item evidence.
5. Container operations bound to menu ID, state ID, slot identity, and inventory delta.

### Phase 3: basic workflows

Python composes the verified primitives into collect, craft, smelt, retrieve, store,
follow, eat, sleep, and deliver workflows. Java retains only bounded mechanical
sub-operations and local safety.

### Phase 4: farming

Farming is a durable Python workflow over deterministic Java crop transactions.
It is not an LLM-generated sequence of blind clicks.

```text
scan region
  -> classify crop and maturity
  -> reserve seed and inventory capacity
  -> claim candidate revision
  -> navigate to interaction pose
  -> re-inspect candidate
  -> harvest using normal player input
  -> verify server block/inventory change
  -> collect nearby drops when required
  -> select compatible seed
  -> plant using normal player input
  -> verify planted block state
  -> release claim and select next candidate
```

Java should expose a crop-adapter registry for deterministic mechanics:

- whether a block is a supported crop;
- maturity from structured block-state properties;
- harvest interaction type and target block;
- compatible seed or planting item;
- legal planting support and face;
- expected post-harvest and post-plant states.

Initial support should be deliberately narrow: vanilla age-based crops on farmland
(`wheat`, `carrots`, `potatoes`, and `beetroots`). Nether wart, cocoa, berries,
gourds, vertical crops, right-click-harvest mods, and modded crop adapters follow
after the transaction and recovery model is proven.

The farm operation must preserve a seed reserve, detect full inventory, avoid immature
crops, revalidate after navigation, and report partial progress. If harvest succeeds
but replant fails, it records a restoration obligation instead of reporting success.

## Reference Findings

- `minecraft-numen` contributes typed asynchronous tasks, bounded recovery, failure
  categories, moving targets, and suspend/resume behavior. Its server fake-player
  world mutations are not applicable to feature one.
- `mindcraft` is closest to the real-player execution model. Its separation of
  reactive modes, long-running actions, Mineflayer mechanics, and LLM planning is
  useful. Its action results and farming support are too weak to copy directly.
- `TouhouLittleMaid` contributes sensors, typed behavior memory, work modes, crop
  adapters, target revalidation, and harvest/replant state flow. Its mob Brain,
  owner checks, direct inventory mutation, healing, and world mutation are not used.

The shallow clones are local research material under `references/` and are excluded
from this repository's tracked source.

## Immediate Acceptance Tests

- A named player can request follow; the body starts, keeps following a moving target,
  pauses for a safety reflex, and resumes the same operation.
- A lost or dimension-changed follow target produces a bounded terminal failure.
- Food in main inventory can be moved to the hotbar and consumed; success requires a
  server-observed item or player-state change.
- A golden apple can be consumed for a configured low-health condition even when the
  hunger bar is full, with item/effect/absorption evidence.
- User stop and disarm terminate active actions exactly once and release all controls.
- The first farming slice harvests and replants one mature vanilla crop using only
  normal real-player actions and verifies both server outcomes.
