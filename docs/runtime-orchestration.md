# LCU Runtime Orchestration

This document defines the target runtime boundary between Minecraft, the Python backend,
LLM agents, and future vision adapters. The goal is rich perception without turning
high-frequency game telemetry into unbounded model context.

**Design status.** Everything *below* the 26.3 status block describes a target design. It
is **not implemented in 26.3** except where a passage is explicitly marked
`Exists today (26.3)`. The design is preserved as future work; the status block is the only
description of current architecture.

## 26.3 Status (current)

- **Java side is client-only.** `LCUMod.commonSetup` returns immediately unless
  `FMLEnvironment.getDist().isClient()` (`LCUMod.java:58-61`). There is no dedicated-server
  or fake-player form. The only key binding is `F12` → `toggle_ai` (`AIKeyHandler.java:31-36`);
  there is no in-game P-key conversation screen.
- **Wire protocol version is 1.** `PROTOCOL_VERSION = 1` (`proto/Protocol.java:24`), with 6
  capability bits — `state_push`, `structured_chat`, `container_view`, `server_events`,
  `entity_detail`, `structured_request` (`:43-49`) — and profiles `client` / `server` / `both`
  (`:52-56`). The class doc (`:16`) says the handshake / channel work is deliberately unwritten;
  the file only fixes constants. There is no v2/v3/v4 and no tool-descriptor handshake.
- **The real wire command surface is `ActionExecutor`'s dispatch switch**
  (`ActionExecutor.java:512-668`, 74 labels); unknown commands return
  `Unknown: <cmd>` (`:667`). Container commands `get_container` / `take_item` / `put_item` /
  `close_container` (`:546-549`) and `drop_item` (`:664`) exist today. Commands such as
  `capture_frame`, `ui_click`, `mine_block_at`, `interact_block_at`, `inventory_click`,
  `get_recipes`, `cancel_operation`, and `get_body_request` do **not** exist.
- **The real HTTP surface** is the 5 SDK/V2 routes in `backend/api/http_surface.py` —
  `GET /api/sdk/info` (`:178`), `POST /api/sdk/chat` (`:219`), `POST /api/sdk/command` (`:242`),
  `GET /api/v2/control` (`:289`), `GET /api/v2/audit` (`:324`) — plus the core routes in
  `backend/server.py`. `POST /api/sdk/command` is the only actuator and refuses reserved
  commands (`RESERVED_BODY_COMMANDS`, `http_surface.py:42`). `RESERVED_ARG_PREFIX = "__lcu_"`
  (`:47`) is only an SDK-argument gate, not a wire feature. `GET /api/v2/control` always
  reports `mode = "builtin"` and `coordinator = null` — no TaskCoordinator runs.
- **Endpoints that are ABSENT** (never registered): `/api/v2/info`, `/api/sdk/identity`,
  `/api/v2/body-requests/*`, `/api/v2/skills*`, `/api/v2/task-presets*`,
  `/api/v2/control/leases*`, `/api/v2/player-pairings`, `/api/v2/runs*`, `/api/v2/events`,
  `/api/v2/schedules*`, `/api/v2/memory*`.
- **Dead modules** (file + unit tests exist, nothing in the live app imports them):
  `body_registry`, `task_coordinator`, `decision_scheduler`, `world_model`, `skill_registry`,
  `access_policy`, `storage_policy`, `memory_management`, `memory_catalog`,
  `task_preset_registry`, `identity`, and `body_request_diagnostics.py`. The live memory module
  is `world_memory.py` — a different module from the dead `world_model.py`.
- **Verification baseline (26.3):** 18 `verifyXxx` JavaExec self-check tasks wired into
  `gradle check` (`build.gradle:205-369`; 1307 checks total), and a 1733-test backend suite.

## Ownership Boundary

*Design (not implemented).*

- Java should own deterministic Minecraft actuators, client-thread access, collision and
  screen identity checks, container transactions, cancellation, and authoritative
  observations.
- Python should own the world model, durable task graph, admission, long-horizon planning,
  memory, model calls, retries, and multi-agent coordination.
- Models should propose typed intents and tool calls. They must not directly mutate Java state,
  declare completion, bypass permissions, or synthesize unrestricted input.
- A single TaskCoordinator / intent arbiter should be the only body writer. SDK, chat,
  schedules, autonomy, specialists, and vision would all submit through the same admission path.
  No such arbiter runs in 26.3 (`/api/v2/control` reports `coordinator: null`).

## State Pipeline

*Design (not implemented).*

State should be split into four layers.

1. `raw`: Java events with sequence, body tick, capture time, dimension, and source.
2. `world model`: Python's latest normalized facts, indexed by stable entity, block,
   inventory, screen, operation, and requester identities.
3. `change journal`: meaningful deltas and lifecycle events with monotonic cursors.
4. `observation slice`: a bounded task-relevant snapshot prepared for one agent call.

Raw state may update frequently. It must never be appended directly to conversation history.
The world model would replace superseded facts and apply TTLs. The journal should store semantic
changes, not repeated positions or identical inventory snapshots. In 26.3 the live session uses
`backend/agent/world_memory.py`; the `world_model.py` module that would own this pipeline is dead.

Every fact should carry:

- observation version and source sequence;
- observed wall time and game tick;
- server, world, dimension, and body identity;
- confidence and authority (`authoritative`, `derived`, or `model_inferred`);
- expiry/TTL where staleness matters;
- provenance linking it to a body event, tool outcome, or agent artifact.

## Model Trigger Policy

*Design (not implemented).*

An LLM call should be allowed at a decision boundary, including:

- a newly admitted user objective;
- operation accepted, failed, cancelled, completed, timed out, or materially stalled;
- target, dimension, screen, inventory requirement, threat, or permission change;
- an explicit specialist request for replanning or missing perception;
- a bounded idle/self-prompt trigger;
- state becoming stale enough that the current plan is no longer safe.

Movement ticks, unchanged health, repeated nearby entities, and routine progress should not
trigger model calls. Deterministic controllers should consume those locally.

Before a call, the context builder would select only:

- objective and active task graph;
- latest operation/outcome and unresolved blockers;
- requester identity and applicable policy;
- task-relevant player/world/inventory/entities/POIs;
- a bounded recent semantic event window;
- references to optional artifacts such as screenshots or recipe traces.

Each agent should have separate input, output, artifact, and call-rate budgets. Context
compaction would summarize completed history, but the current objective, safety constraints,
active operation, and unresolved failures would remain lossless structured fields.

## State Refresh Classes

*Design (not implemented).*

- `critical`: health, death, disconnect, control ownership, screen replacement, operation
  terminal state. Deliver immediately and never coalesce across semantic transitions.
- `interactive`: active target distance, path state, selected slot, container revision.
  Coalesce to a bounded rate while an operation owns the related effect.
- `ambient`: nearby entities, POIs, weather, light, roster. Update the world model and
  emit only joins/leaves, threshold crossings, or requested refreshes.
- `bulk`: recipes, storage contents, map surveys, screenshots. Fetch on demand, cache by
  revision/hash, and pass agents references rather than duplicate payloads.

Backpressure should drop superseded ambient/raw frames first. It must never drop terminal
outcomes, safety transitions, control changes, or the newest state for an active operation.

## Multi-Agent Coordination

*Design (not implemented). No `MultiAgentOrchestrator` class exists in the repository (0 hits).*

The proposed architecture is a supervisor with optional specialists, not independent agents
competing for the body.

- The supervisor would own the objective, task graph, admission priority, and total budget.
- Specialists would receive scoped observation slices and return typed artifacts.
- The executor would convert admitted intents into durable tool runs.
- A shared blackboard would store artifacts and world-model references by ID and revision.
- Agent messages should contain `task_id`, `agent_id`, `observation_version`, artifact
  references, findings, confidence, requested capability, and expiry. Full transcripts are
  not relayed.
- An artifact produced from an old observation must not authorize a current action without
  revalidation.
- Only the coordinator should be able to submit body tools. Specialists would not call
  `Skills` directly.

Tool manifests should declare effects such as `body.move`, `inventory.ui`, `world.break`, and
`entity.attack`. Admission would reject or preempt conflicting effects. Safety preempts all;
explicit authorized player work preempts autonomy; equivalent requests deduplicate.

## Vision And UI Interaction

*Design (not implemented). Neither `capture_frame` nor `ui_click` exists in the Java command
surface or the backend.*

Vision is intended as an on-demand capability for screens that do not expose sufficient
structured semantics. Structured container/menu APIs remain preferred.

`capture_frame` should create a local image artifact with:

- artifact ID, SHA-256, MIME type, dimensions, and byte size;
- body sequence, game tick, screen ID/class, menu/container ID, and screen revision;
- window dimensions, framebuffer dimensions, GUI scale, and cursor position;
- optional ROI and redaction metadata;
- creation time, TTL, owner task, and access policy.

Images would not be embedded in the normal JSONL state stream or conversation history. The
backend would store them in a bounded local artifact cache and give a vision-capable agent a
reference only when required. Repeated captures would deduplicate by hash and screen revision.

`ui_click` proposals should use normalized framebuffer coordinates or a structured widget/slot
identity and must include the source artifact ID and expected screen revision. Java would check:

- the same screen/menu is still open;
- window size, GUI scale, and framebuffer mapping still match;
- the point is inside the allowed screen and not a protected command/chat surface;
- the requesting task owns `inventory.ui` or `screen.input`;
- rate, button, click count, and drag bounds are valid.

After execution, Java would emit a new screen/container revision. Python would verify a declared
postcondition using structured state first and a follow-up ROI capture only when needed.
Mismatch, timeout, unexpected screen replacement, or ambiguous visual result would cancel the
sequence and return control for replanning. Blind repeated clicking is forbidden by design.

Keyboard input would follow the same contract. Arbitrary text, chat commands, clipboard access,
global OS input, and clicks outside Minecraft would remain unavailable unless a separately
authorized adapter explicitly provides them.

## Tool Discovery And Lifecycle

*Design (not implemented, except where noted).*

A future protocol revision should advertise Java tool descriptors during authentication; the
current protocol is version 1 (`Protocol.java:24`). A descriptor would contain command,
version, input schema, execution class, completion contract, cancellability, availability, and
effects. Python would reconcile SkillRegistry against the connected body and fail unavailable
capabilities before dispatch.

The next protocol lifecycle revision should separate:

- `accepted`: command validated and operation created;
- `progress`: nonterminal bounded progress and structured blocker;
- `outcome`: exactly one `succeeded`, `failed`, or `cancelled` terminal result;
- `cancel_operation`: cancellation correlated to the original operation ID. (26.3 has no
  `cancel_operation`; it has `stop_all` and `stop_digging`, dispatched at
  `ActionExecutor.java:538` and `:532`.)

State snapshots are observations, not completion signals. Replacing, stopping, disconnecting,
or preempting an operation must terminate the displaced operation ID exactly once.

## Item Targets And Containers

*Mixed: the wire commands exist today; the target/category semantics are design.*

Tools should distinguish a concrete item registry ID from a category target. Craft outputs are
concrete IDs. Inventory search, storage search, and collection should be able to accept item
tags, including `#minecraft:logs` and `#minecraft:planks`; the synthetic `#lcu:wood` union is a
proposal, not an implemented tag. Java should resolve tags against the live item registry, and
recipe planning should use the recipe's structured `Ingredient` alternatives rather than
language aliases or substring matching. `Exists today (26.3)`: recipe planning already consumes
structured `Ingredient`s (`CraftingPlanner.java:149`), and `#`-prefixed item tags are recognized
in recipe query matching (`RecipeQuery.java:552`).

Container state is intended as a bulk world-model observation, not conversation history. The
Java body should record complete item counts, logical position, observation age, and a bounded
TTL. A vanilla double chest would have one logical snapshot and retry identity but might expose
either block half as an interaction target. World or dimension replacement should invalidate the
cache.

The generic container transaction should be:

1. Select a logical container from fresh indexed contents or a bounded unknown set.
2. Resolve a reachable standing position and visible interaction block.
3. Send one open request and claim only the resulting eligible menu ID.
4. Read slots with explicit `storage` or `player` scope.
5. Transfer one eligible slot and wait for an authoritative inventory delta.
6. Refresh the content snapshot, then close only the menu owned by the operation.

`Exists today (26.3)`: the wire already exposes `get_container`, `take_item`, `put_item`, and
`close_container` (`ActionExecutor.java:546-549`), plus `drop_item` (`:664`), alongside
movement, interaction, inventory, collection, and crafting tools. Requiring the current
container ID and slot on every container mutation — so stage screen actions are rejected — is
design. Python is meant to compose these primitives into retrieval, delivery, sorting, and
future specialist workflows; Java keeps only the deterministic menu and safety mechanics.

## Durable Workflows

*Design (not implemented). `/api/v2/runs*` is absent; there is no Task Run parent/child model.*

A multi-step preset should create one workflow parent run and one queued child Skill run. The
parent would store the resolved immutable step specification, current step, active child, scope,
and control lease identity. Only child runs would be dispatched to the body.

Child terminalization, parent progress/status propagation, and creation of the next queued
child should occur in one SQLite transaction. A successful child would advance exactly once;
duplicate terminal events could not create duplicate steps. Failure and cancellation would stop
the parent. Disconnect, timeout, cancellation rejection, or backend restart while a child is in
flight should mark both child and parent `unknown`, preserving at-most-once execution rather
than replaying an operation whose body-side result is uncertain. Queued parents would require
explicit resume.

## Normalized World Model

*Design (not implemented). `Session` does not own a revisioned World Model in 26.3; the
`world_model.py`, `decision_scheduler.py`, and `task_coordinator.py` modules that would
implement this are dead.*

`Session` should own one revisioned World Model as the ingestion boundary for body observations.
Production `state_update` payloads would be authoritative for player, world, inventory,
equipment, entities, online players, nearby blocks, workstations, storage, and integration
snapshots. Snapshot-owned collections omitted by a later valid snapshot should be cleared rather
than retained as phantom state. Task, behavior, and control state would be independent overlays
and survive a snapshot that does not contain them.

Each accepted fact should record its revision, observation time, source, and TTL. Malformed
fields must not replace the last valid fact and must not refresh global player/world freshness.
Disconnect should mark last-known facts stale without erasing them. Unknown top-level producer
fields would remain a last-known compatibility projection for existing adapters; they are not
authoritative facts.

A `session.runtime` compatibility projection is intended to be consumed by modes, commands,
schedules, APIs, and dashboard code. Planner, self-prompt, and private-conversation LLM paths
should use one deterministic observation builder instead of copying runtime fields
independently. The builder would canonicalize dictionaries, sort and deduplicate collections,
clip untrusted values, and enforce a character budget before prompt construction. Observation
metadata should include schema, revision, age, source, and stale state. Request input capacity
should be calculated against the output tokens requested by that specific model call.

The World Model is also intended to keep a bounded semantic journal. It would ignore ordinary
position/tick churn, aggregate inventory count deltas, and record meaningful health, hunger,
dimension, weather, task, behavior, control, and connection changes. Only dangerous health loss,
low hunger crossings, dimension changes, task terminal/blocking states, and established control
changes would enter the bounded decision-trigger queue. Consumers should explicitly acknowledge
triggers by sequence. Creating a trigger must not call the model or bypass the TaskCoordinator;
the asynchronous decision scheduler would be the sole consumer. In 26.3 no decision scheduler
runs — `decision_scheduler.py` is dead.

The decision scheduler would copy one bounded context and run the model on a daemon worker. The
model could only return a strict `none` or `run_skill` proposal; it would never receive a body
executor. Each request should be bound to a world scope, body connection epoch, observation
revision, semantic sequence, completion timestamp, and proposal TTL. Disconnect would invalidate
in-flight work. Model or parse failures should retain the trigger and retry with bounded
exponential backoff; terminal dispositions would explicitly acknowledge it.

Automatic execution is intended to be fail-closed. The first policy would admit only the pinned
`general.eat` manifest contract and verify current hunger again before execution. Combat,
movement, resource mutation, chat, UI operations, and `core.stop` would not be model-automatic.
The latter belongs in deterministic safety arbitration. The TaskCoordinator would perform atomic
automatic admission under its dispatch lock and create no resumable queued run when the body is
busy. Decision request, failure, invalidation, rejection, expiration, and dispatch records would
enter the durable event stream.

Chat Planner execution is meant to use the same boundary. The model would emit one complete
top-level `tool(name, JSON)` action, which would become a typed `SkillProposal`; Planner must
never invoke Skills directly. The TaskCoordinator should admit only durable, non-combat
manifests and record admission or rejection. Legacy substring actions, fenced actions, multiline
action syntax, prose containing action syntax, and responses with multiple actions should fail
closed before any proposal is emitted. An interrupt generation would invalidate an LLM result
produced after a newer stop intent.

`core.stop` is intended to remain deterministic and to bypass a busy planning lock. A permitted
in-game chat stop would be recognized in the wire receive callback, admitted through the
TaskCoordinator, and marked so normal event draining records and acknowledges it without
planning again. Java would queue `stop_all` at CONTROL priority, atomically drain lower-priority
queued work, preserve FIFO within CONTROL, and emit a terminal response or cancellation for each
discarded request.

## Wire Admission

*Design (not implemented).*

A future protocol revision should require a nonblank request ID for new commands. The body would
admit one immutable command fingerprint per ID, suppress in-flight duplicates, replay a cached
terminal response or outcome for an identical retry, and reject ID reuse with different content.
The ledger should be bounded and retained across controller handoffs so old requests cannot be
replayed as new work.

Body clients are intended to publish `body_world_epoch` and `body_control_epoch` in both
control-state and state-update metadata. The Python wire client would attach a coherent pair as
`__lcu_world_epoch` and `__lcu_control_epoch` to later commands. Java would validate supplied
pairs immediately before effects; missing pairs would remain accepted during protocol migration.
`stop_all`, `disarm`, and `control_builtin` would remain epoch-exempt revocation commands. These
epoch fields do not exist in the 26.3 wire; the only `__lcu_` occurrence in the tree is the SDK
argument prefix `RESERVED_ARG_PREFIX` (`backend/api/http_surface.py:47`).

## Proposed Migration Order

*Not started. Every step below is future work; none of it is implemented in 26.3.*

1. Advertise and reconcile deterministic Java capabilities.
2. Route chat Planner actions through TaskCoordinator durable runs. **Not done** — nothing in the
   live app routes chat through a TaskCoordinator, and `task_coordinator.py` is dead.
3. Add explicit operation outcomes and correlated cancellation.
4. Persist parent/child workflow execution and atomic step advancement.
5. Route open-ended Planner goals through typed proposals and TaskCoordinator instead of direct Skills.
6. Move autonomy and specialists behind the same admission/effects gate.
7. Add bounded image artifacts, screen identity, capture, and verified UI input tools.

**What already exists (26.3):** the 18 `verifyXxx` Java self-check tasks wired into `gradle check`
(`build.gradle:205-369`) and the 1733-test backend suite. These verify the current deterministic
actuators and backend, not the orchestration design above.
