# LCU Runtime Orchestration

This document defines the runtime boundary between Minecraft, the Python backend,
LLM agents, and future vision adapters. The design goal is rich perception without
turning high-frequency game telemetry into unbounded model context.

## Ownership Boundary

- Java owns deterministic Minecraft actuators, client-thread access, collision and
  screen identity checks, container transactions, cancellation, and authoritative
  observations.
- Python owns the world model, durable task graph, admission, long-horizon planning,
  memory, model calls, retries, and multi-agent coordination.
- Models propose typed intents and tool calls. They do not directly mutate Java state,
  declare completion, bypass permissions, or synthesize unrestricted input.
- One TaskCoordinator/intent arbiter is the only body writer. SDK, chat, schedules,
  autonomy, specialists, and vision all submit through the same admission path.

## State Pipeline

State is split into four layers.

1. `raw`: Java events with sequence, body tick, capture time, dimension, and source.
2. `world model`: Python's latest normalized facts, indexed by stable entity, block,
   inventory, screen, operation, and requester identities.
3. `change journal`: meaningful deltas and lifecycle events with monotonic cursors.
4. `observation slice`: a bounded task-relevant snapshot prepared for one agent call.

Raw state may update frequently. It is never appended directly to conversation history.
The world model replaces superseded facts and applies TTLs. The journal stores semantic
changes, not repeated positions or identical inventory snapshots.

Every fact should carry:

- observation version and source sequence;
- observed wall time and game tick;
- server, world, dimension, and body identity;
- confidence and authority (`authoritative`, `derived`, or `model_inferred`);
- expiry/TTL where staleness matters;
- provenance linking it to a body event, tool outcome, or agent artifact.

## Model Trigger Policy

An LLM call is allowed at a decision boundary, including:

- a newly admitted user objective;
- operation accepted, failed, cancelled, completed, timed out, or materially stalled;
- target, dimension, screen, inventory requirement, threat, or permission change;
- an explicit specialist request for replanning or missing perception;
- a bounded idle/self-prompt trigger;
- state becoming stale enough that the current plan is no longer safe.

Movement ticks, unchanged health, repeated nearby entities, and routine progress do not
trigger model calls. Deterministic controllers consume those locally.

Before a call, the context builder selects only:

- objective and active task graph;
- latest operation/outcome and unresolved blockers;
- requester identity and applicable policy;
- task-relevant player/world/inventory/entities/POIs;
- a bounded recent semantic event window;
- references to optional artifacts such as screenshots or recipe traces.

Each agent has separate input, output, artifact, and call-rate budgets. Context compaction
summarizes completed history, but current objective, safety constraints, active operation,
and unresolved failures remain lossless structured fields.

## State Refresh Classes

- `critical`: health, death, disconnect, control ownership, screen replacement, operation
  terminal state. Deliver immediately and never coalesce across semantic transitions.
- `interactive`: active target distance, path state, selected slot, container revision.
  Coalesce to a bounded rate while an operation owns the related effect.
- `ambient`: nearby entities, POIs, weather, light, roster. Update the world model and
  emit only joins/leaves, threshold crossings, or requested refreshes.
- `bulk`: recipes, storage contents, map surveys, screenshots. Fetch on demand, cache by
  revision/hash, and pass agents references rather than duplicate payloads.

Backpressure drops superseded ambient/raw frames first. It must never drop terminal
outcomes, safety transitions, control changes, or the newest state for an active operation.

## Multi-Agent Coordination

The production architecture uses a supervisor with optional specialists, not independent
agents competing for the body.

- The supervisor owns the objective, task graph, admission priority, and total budget.
- Specialists receive scoped observation slices and return typed artifacts.
- The executor converts admitted intents into durable tool runs.
- A shared blackboard stores artifacts and world-model references by ID and revision.
- Agent messages contain `task_id`, `agent_id`, `observation_version`, artifact references,
  findings, confidence, requested capability, and expiry. Full transcripts are not relayed.
- An artifact produced from an old observation cannot authorize a current action without
  revalidation.
- Only the coordinator may submit body tools. Specialists cannot call `Skills` directly.

Tool manifests declare effects such as `body.move`, `inventory.ui`, `world.break`, and
`entity.attack`. Admission rejects or preempts conflicting effects. Safety preempts all;
explicit authorized player work preempts autonomy; equivalent requests deduplicate.

The existing `MultiAgentOrchestrator` is experimental and is not a second production
control path. Its useful agents must be adapted as specialists behind the shared world
model and TaskCoordinator.

## Vision And UI Interaction

Vision is an on-demand capability for screens that do not expose sufficient structured
semantics. Structured container/menu APIs remain preferred.

`capture_frame` should create a local image artifact with:

- artifact ID, SHA-256, MIME type, dimensions, and byte size;
- body sequence, game tick, screen ID/class, menu/container ID, and screen revision;
- window dimensions, framebuffer dimensions, GUI scale, and cursor position;
- optional ROI and redaction metadata;
- creation time, TTL, owner task, and access policy.

Images are not embedded in the normal JSONL state stream or conversation history. The
backend stores them in a bounded local artifact cache and gives a vision-capable agent a
reference only when required. Repeated captures deduplicate by hash and screen revision.

`ui_click` proposals use normalized framebuffer coordinates or a structured widget/slot
identity and must include the source artifact ID and expected screen revision. Java checks:

- the same screen/menu is still open;
- window size, GUI scale, and framebuffer mapping still match;
- the point is inside the allowed screen and not a protected command/chat surface;
- the requesting task owns `inventory.ui` or `screen.input`;
- rate, button, click count, and drag bounds are valid.

After execution, Java emits a new screen/container revision. Python verifies a declared
postcondition using structured state first and a follow-up ROI capture only when needed.
Mismatch, timeout, unexpected screen replacement, or ambiguous visual result cancels the
sequence and returns control for replanning. Blind repeated clicking is forbidden.

Keyboard input follows the same contract. Arbitrary text, chat commands, clipboard access,
global OS input, and clicks outside Minecraft remain unavailable unless a separately
authorized adapter explicitly provides them.

## Tool Discovery And Lifecycle

Wire protocol v3 advertises Java tool descriptors during authentication. A descriptor
contains command, version, input schema, execution class, completion contract,
cancellability, availability, and effects. Python reconciles SkillRegistry against the
connected body and fails unavailable capabilities before dispatch.

The next protocol lifecycle revision should separate:

- `accepted`: command validated and operation created;
- `progress`: nonterminal bounded progress and structured blocker;
- `outcome`: exactly one `succeeded`, `failed`, or `cancelled` terminal result;
- `cancel_operation`: cancellation correlated to the original operation ID.

State snapshots are observations, not completion signals. Replacing, stopping, disconnecting,
or preempting an operation must terminate the displaced operation ID exactly once.

## Item Targets And Containers

Tools distinguish a concrete item registry ID from a category target. Craft outputs are
concrete IDs. Inventory search, storage search, and collection may accept item tags, including
`#minecraft:logs`, `#minecraft:planks`, and the synthetic `#lcu:wood` union. Java resolves tags
against the live item registry; recipe planning uses the recipe's structured `Ingredient`
alternatives rather than language aliases or substring matching.

Container state is a bulk world-model observation, not conversation history. The Java body
records complete item counts, logical position, observation age, and a bounded TTL. A vanilla
double chest has one logical snapshot and retry identity but may expose either block half as
an interaction target. World or dimension replacement invalidates the cache.

The generic container transaction is:

1. Select a logical container from fresh indexed contents or a bounded unknown set.
2. Resolve a reachable standing position and visible interaction block.
3. Send one open request and claim only the resulting eligible menu ID.
4. Read slots with explicit `storage` or `player` scope.
5. Transfer one eligible slot and wait for an authoritative inventory delta.
6. Refresh the content snapshot, then close only the menu owned by the operation.

Wire v3 advertises `get_container`, `take_item`, `put_item`, `close_container`, and
`drop_item` alongside movement, interaction, inventory, collection, and crafting tools.
Container mutation commands require the current container ID and slot, preventing stale
screen actions. Python composes these primitives into retrieval, delivery, sorting, and
future specialist workflows; Java keeps only the deterministic menu and safety mechanics.

## Current Migration Order

1. Advertise and reconcile deterministic Java capabilities.
2. Route chat Planner actions through TaskCoordinator durable runs.
3. Add explicit operation outcomes and correlated cancellation.
4. Introduce the normalized world model, semantic journal, and observation builder.
5. Move autonomy and specialists behind the same admission/effects gate.
6. Add bounded image artifacts, screen identity, capture, and verified UI input tools.
