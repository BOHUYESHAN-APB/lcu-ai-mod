# LCU Project Tracker

Updated: 2026-07-19

This document is the persistent source of truth for requirements, architecture
decisions, unresolved questions, delivery order, and verification. It exists so
long-running work does not depend on chat context or lose constraints after
context compression.

## Maintenance Rules

- Update this file whenever a requirement, priority, decision, or open question changes.
- Preserve the original intent when splitting work into implementation tasks.
- Mark work `planned`, `active`, `blocked`, `verified`, or `deferred`.
- Do not mark work `verified` until automated tests and the relevant runtime check pass.
- Record unresolved semantics explicitly instead of silently choosing an unsafe default.
- Keep implementation details in linked code or design documents; keep the current decision here.

## Product Goal

LCU is a multi-AI Minecraft companion platform with interchangeable bodies and a
shared personality, memory, planning, Skill, and operations backend.

Target components:

- `lcu-common`: shared protocol, identity, events, Skills, and body contracts.
- `lcu-body-client`: a real authenticated Minecraft client with first-person rendering.
- `lcu-player-client`: a normal-player chat and companion interface.
- `lcu-server`: a future server-side fake-player body.
- `lcu-control`: a modern operations console for models, memory, bodies, tasks, and diagnostics.

The deterministic runtime owns tick-critical movement, crafting, interaction,
safety, and completion checks. Models may interpret language, choose open-ended
goals, chat, summarize, and make long-horizon decisions. A model must not be
required for deterministic crafting, immediate hazard escape, or actuator safety.

## Non-Negotiable Requirements

### Runtime Safety

- Player control is the default. AI control requires explicit F12 arming.
- Disconnect, death, control transfer, and fencing-generation changes revoke stale actions.
- Queued durable runs never resume after reconnect without explicit operator approval.
- Unknown containers may be inspected only for an admitted inventory objective, with bounded
  attempts, menu ownership, snapshot caching, and no unrelated-menu clicks.
- Immediate safety behavior is deterministic and local to the Java body runtime.
- Backend tasks, Java autonomy, gaze, navigation, mining, interaction, and reflexes must not write competing inputs directly.

### Model Admission

- A model cannot be enabled for production use until context-window limits,
  maximum input, maximum output, request-size limits, automatic compression,
  memory retention, and observable usage budgets are implemented.
- Every named agent may have its own provider, endpoint, key, model, and budget.
- Per-agent credentials must work without requiring a default-agent API key.
- The transport must enforce `estimated_input + reserved_output <= context_window`.
- Oversized or invalid requests fail before network transmission with a structured reason.
- Compression cannot silently discard system policy, current task constraints,
  safety policy, tool results required by the active task, or recent user messages.

### Persistence

- SQLite is for automated tests and local development only.
- PostgreSQL is mandatory for production deployments.
- A production startup must not silently fall back from PostgreSQL to SQLite.
- Schema migrations, indexes, transaction behavior, concurrency, backup, and restore
  must be validated against PostgreSQL before a production release.
- JSON files may remain import/export formats, but production operational and memory
  data must have a defined PostgreSQL owner before production readiness.

Current development decision:

- All near-term development, testing, memory management, model governance, UI,
  and body-control work uses SQLite.
- PostgreSQL implementation is deferred until production deployment work begins.
- PostgreSQL is not a blocker for development milestones or local/shared-server testing.
- New storage code should still use repository/domain methods rather than exposing
  SQLite connections, because PostgreSQL differs in SQL syntax, generated IDs,
  migrations, locking, concurrency, and transaction semantics.

### Operations UI

- The first screen is an operational console, not a marketing page.
- Desktop and mobile views must support normal workflows without raw wire commands.
- The console must clearly distinguish backend connectivity, body connectivity,
  armed state, control owner, data freshness, and task state.
- Model and memory controls must expose limits, current use, failures, and destructive-action confirmation.
- The visual design must be modern, restrained, responsive, and information-dense without nested-card clutter.

## Current Delivery Order

### Phase 0: Persistent Planning

Status: `active`

- [x] Create this persistent tracker.
- [x] Record the production PostgreSQL requirement.
- [ ] Confirm all unresolved policy semantics with the user.
- [ ] Keep this tracker synchronized as implementation proceeds.

### Phase 1: Model Resource Governance

Status: `verified` for transport budgets and fallback compaction; durable semantic summaries continue in Phase 2.

Initial audit findings addressed in this phase:

- `LLMService._build_payload()` sends the complete message list without an input budget.
- `max_tokens` limits output only and is not checked against a context window.
- There is no tokenizer integration, conservative fallback estimator, or request-byte cap.
- Prompt trimming is duplicated across agents and uses inconsistent rules.
- There is no automatic summary/compaction transaction or compression failure fallback.
- Planner and Timing Gate can incorrectly reject an agent with its own API key.
- LLM usage shown by the server may come from a different service instance than the active Session.

Implemented:

- Canonical per-agent context, input, output, reserve, request-byte, compression,
  recent-message, and summary-agent settings.
- Configuration version 2 migration from persisted `max_tokens` to
  `max_output_tokens`, with canonical output only.
- Atomic type, range, and cross-field validation in `ConfigStore`.
- One admission path for streaming and non-streaming requests.
- Conservative deterministic token estimation and exact serialized request-byte checks.
- Pre-network rejection with structured reason codes and bounded telemetry.
- Non-mutating fallback compaction that removes only old optional messages while
  preserving system messages, explicitly required messages, and the configured recent window.
- Per-agent request/result/usage attribution, latest rejection, and latest compaction state.
- Named-agent credentials and keyless providers no longer depend on the default API key.
- The server and console now read usage from the active Session LLM service.
- The model settings UI exposes all Phase 1 limits and reports per-agent usage,
  rejections, allocation, and fallback compaction.

Required configuration, with validated per-agent overrides:

- `context_window_tokens`
- `max_input_tokens`
- `max_output_tokens`
- `reserved_output_tokens`
- `max_request_bytes`
- `compression_enabled`
- `compression_trigger_tokens`
- `compression_target_tokens`
- `recent_messages_to_keep`
- `summary_model_agent`
- `summary_max_output_tokens`

Implementation rules:

1. Normalize and validate configuration in `ConfigStore`.
2. Preserve persisted `max_tokens` through an explicit configuration migration to
   `max_output_tokens`; do not maintain ambiguous duplicate runtime behavior.
3. Build a single request-budget path used by streaming and non-streaming calls.
4. Prefer provider/model tokenizer metadata when available; otherwise use a
   conservative deterministic estimate and report that the estimate is approximate.
5. Reserve output tokens before admitting input.
6. Apply semantic priority when reducing input: mandatory system and safety policy,
   active task contract, recent tool results, recent conversation, summaries, then optional history.
7. Reject requests that remain too large after permitted reduction.
8. Record estimated and provider-reported tokens per request, agent, model, and outcome.

Acceptance gates:

- [x] Representative numeric boundaries and invalid cross-field combinations are tested.
- [x] No HTTP request is sent when the local budget rejects it.
- [x] Streaming and non-streaming paths enforce the same admission path.
- [x] Per-agent keys and keyless local providers are independently usable.
- [x] The UI shows context allocation and the latest rejection or compaction.
- [ ] Provider-specific exact tokenizers and model metadata are available.
- [ ] Durable semantic summaries replace fallback-only omission for long histories.

### Phase 2: Automatic Compression And Memory Management

Status: `verified` for the SQLite development loop. PostgreSQL ownership remains deferred until production work.

Memory sources currently include SQLite message history and JSON-backed companion
memory. Bounded recent collections exist, but profiles, relationships, locations,
and experiences can continue growing without a unified retention policy.

Required behavior:

- Compression is an explicit transaction with source range, summary version,
  token counts, timestamps, model identity, and provenance.
- Original records are retained until the summary is successfully stored and validated.
- Repeated compression is hierarchical and cannot recursively erase active constraints.
- A deterministic fallback trims only policy-approved optional history when the
  summarizer is unavailable; it must report degraded context.
- Retention policies are configurable by memory category and scope.
- Operators can inspect, search, filter, export, compact, archive, and delete memory.
- Destructive operations support preview, confirmation, audit records, and scoped deletion.
- Memory remains isolated by configured companion/server/world scope.
- Secrets and raw credentials are never written into summaries or exports.

Implemented:

- A canonical read-only memory catalog projects local messages and JSON memory
  into stable category, scope, revision, content, and provenance envelopes.
- V2 status, filtered/paginated record browsing, detail, and redacted JSON/JSONL
  export APIs are available.
- The operations console has a responsive Memory view with scope/storage status,
  search, category/player filters, pagination, detail/provenance, and export.
- Memory schema version 4 persists immutable durable summaries and injects the
  latest summaries into bounded model context.
- Semantic summary creation uses a two-stage preview/commit contract. Commit
  revalidates the full memory revision and exact selected-source hash.
- Failed persistence rolls back the in-memory summary; repeated commit is idempotent.
- Summary commit retains every source record. Lifecycle actions use reversible
  overlay states and never hard-delete source rows or JSON records.
- Runtime storage policy now rejects production SQLite and blocks production
  startup until the PostgreSQL adapter exists.
- A dedicated SQLite lifecycle overlay implements reversible `active`, `archived`,
  and `deleted` states without changing source rows or JSON records.
- Archive, soft-delete, restore, retention, and audit use transactional repository methods.
- Manual and retention changes require preview, confirmation token/text, full
  revision validation, and exact selected-source hash validation.
- Retention rules are versioned per scope and use optimistic concurrency.
- The Memory UI exposes state filters, per-record archive/delete/restore actions,
  typed confirmation dialogs, retention rule editing, preview, and execution.

Production data decision:

- PostgreSQL will own conversations, messages, summaries, operational events,
  leases, Task Runs, schedules, and production memory records.
- SQLite adapters remain available only for tests and local development.
- PostgreSQL adapter implementation and migration validation are explicitly deferred
  during the current development stage.
- The exact migration path for existing JSON memory is `planned`; import must be
  idempotent and preserve source metadata.

Acceptance gates:

- [x] Summary retry and failure tests prove no source data is lost.
- [x] Browse, filter, detail, scope, provenance, redaction, pagination, and export are tested.
- [x] Stale preview revisions and changed source hashes are rejected.
- [x] Retention evaluation, optimistic versions, transactional state changes, and audit are tested.
- PostgreSQL integration tests cover migrations, concurrent writers, restart, and rollback.
- Export/import round trips preserve identity, scope, ordering, and provenance.

### Phase 3: Modern Operations Console

Status: `active`; model governance, memory management, durable operations, and the
player-conversation inbox are implemented. Full information architecture and
single-file console decomposition remain.

Required views and workflows:

- Overview: backend/body state, armed state, control owner, telemetry freshness,
  health, hunger, position, online players, current intent, and active task.
- Models: provider and model selection, per-agent credentials, temperature,
  context window, input/output allocation, compression thresholds, live usage,
  request history, test connection, and validation errors.
- Memory: scoped search, timeline, profiles, relationships, summaries, provenance,
  retention, export, compression preview, archive, and delete confirmation.
- Operations: control leases, Skill registry, Task Runs, pause/resume/cancel,
  schedules, durable events, and diagnostics.
- Bodies: current and future body roster with capability and ownership state.
- Settings: database health, migration status, retention, authentication, and audit information.

Implemented:

- The Chat view provides a responsive contacts and conversation-history layout
  backed by the authenticated operator inbox APIs.
- Contacts show presence and message counts, retain selection across refreshes,
  and open the newest conversation on first load.

UI implementation constraints:

- Preserve working APIs while separating the current single-file console into
  maintainable view, state, and style boundaries when implementation begins.
- Use clear tables, tabs, status indicators, segmented controls, toggles, and icons.
- Avoid ornamental hero layouts, nested cards, hidden critical state, and controls
  that shift size as live values update.
- Validate loading, empty, stale, disconnected, unauthorized, failure, and success states.
- Verify representative desktop and mobile viewports before release.

### Phase 4: Unified Body Intent Arbitration

Status: `planned`

Audit finding: Java currently has several independent camera and actuator writers.
`ActionExecutor`, `Pathfinder`, autonomous behavior, human-like gaze, anti-AFK,
follow, collect, storage, mining, and backend commands can overwrite shared state.
Queue priority orders pending commands but does not preempt already-running work.

Decision:

- Add one Java-side body intent arbiter above all physical actuators.
- Backend leases and Task Runs remain ownership/admission boundaries; they are not
  tick-level input schedulers.
- Actuator channels are `locomotion`, `gaze`, `hands`, and `inventory`.
- Every intent carries owner, source, request/run ID, fencing generation, priority,
  purpose, expiry, claimed channels, resumability, and cancellation reason.
- Only the arbiter may commit movement keys, yaw/pitch, attacks, use, digging, or
  body-critical inventory changes.
- Stale actuation epochs are ignored.

Priority order:

1. Disarm, disconnect, death, respawn, dimension transition, and user takeover.
2. Immediate environmental escape.
3. Critical-health retreat.
4. Authorized counterattack.
5. Recovery and safe armor maintenance.
6. Explicit foreground task or fenced external control.
7. Autonomous goals and patrol.
8. Idle gaze and anti-AFK activity.

Resumption rules:

- Resume only intents explicitly marked resumable.
- Never resume across disconnect, death, dimension, disarm, or fencing-generation changes.
- Revalidate world target, path, inventory baseline, and task checkpoint before resuming.
- Mining verifies the target block; navigation recalculates; container operations
  fail closed unless transaction state is proven.
- Emit durable pause, resume, cancel, and preemption reasons.

### Phase 5: Gaze, Mining, And Wall Awareness

Status: `planned`

User requirement:

- The companion must not stare at a wall indefinitely.
- Looking at a wall is valid when mining, interacting with a target block, or when
  navigation requires a short constrained look.
- Autonomous interaction and backend-controlled actions must not fight over the camera.

Policy:

- Every gaze intent declares `navigation`, `mining`, `interaction`, `combat`,
  `social`, or `idle` purpose.
- Mining and interaction gaze requires a concrete target and a renewable short lease.
- Idle/social gaze is revoked after sustained near opaque-wall occlusion without
  progress toward a valid target.
- A deterministic open-ray search selects a replacement idle direction and applies
  hysteresis so gaze does not oscillate.
- Digging owns gaze and hands together; loss of target, reach, line of sight, or
  lease stops digging before another gaze owner is admitted.
- Explicit look commands without a purpose or expiry fail closed.

### Phase 6: Damage Attribution And Defensive Behavior

Status: `planned`

User requirements:

- After taking damage, identify the source and respond autonomously.
- Cactus, magma, lava, fire, drowning, suffocation, and similar environmental
  damage trigger movement to a safer position and safer patrol routing.
- An attributed hostile-mob attack triggers counterattack unless critical survival
  requires retreat.
- Before combat, equip available armor when safe and choose an available sword or axe.
- Player attacks require health/threat evaluation and whitelist-aware policy.
- A trusted/whitelisted player must not be attacked.
- At `<= 4` Minecraft health points (2 hearts), disengage and escape without
  counterattacking a player. At `> 4` health points, counterattack is permitted
  only against a confidently attributed attacker outside the trusted whitelist.

Deterministic design:

- Capture client damage packets or equivalent authoritative evidence and emit a
  normalized event with sequence, type/tags, health/absorption delta, causing and
  direct entity, source position, and attribution confidence.
- Never authorize player retaliation from proximity alone.
- Track UUID-based trusted players separately from the existing chat whitelist behavior.
- Environmental escape uses local collision/fluid/hazard evaluation and bounded
  safe-position search; the LLM is not involved.
- Hostile counterattack verifies target identity, reach, line of sight, attack
  cooldown, health policy, and current arbiter ownership.
- Weapon and armor selection uses item attributes/components, enchantments,
  durability, equipment slots, and binding constraints rather than display names.
- Proactive attacks against merely nearby hostiles are a separate option and default off.

Confirmed player-health boundary:

- “Two” means 2 hearts, equal to 4 Minecraft health points.
- `health <= 4`: disengage and escape; do not counterattack a player.
- `health > 4`: retaliation may be authorized only for an attributed attacker
  outside the UUID-based trusted whitelist.
- Unknown or low-confidence attribution still fails closed and cannot authorize
  a player attack at any health level.

Acceptance gates:

- Dedicated-server testing proves attacker attribution is available client-side.
- Tests cover cactus, magma, fire, lava, drowning, blocked escape, and hysteresis.
- Tests cover mob projectile ownership, unknown sources, trusted players, multiple
  attackers, threshold boundaries, target death, line-of-sight loss, and low health.
- Safety preemption proves no stale navigation, mining, gaze, or inventory input executes.

### Phase 7: Multiplayer Governance And Task Admission

Status: `planned`; current whitelist and command permissions are not sufficient for OP operation.

Required identity groups:

- One configurable `master` identity with the highest normal task authority.
- Configurable `friends` with scoped conversation and task permissions.
- Ordinary players default to low-cost, rate-limited replies and cannot assign body tasks.
- Every identity, group, permission, whitelist change, denial, and task admission has durable history and audit provenance.

Task admission must combine arrival time, requester weight, explicit urgency, safety,
resource locks, current-task interruption cost, and starvation prevention. The system
must not hard-code every natural-language task into one fixed priority table. Models
may classify intent and explain tradeoffs, but deterministic policy owns final admission.

OP safety policy:

- AI may be granted server operator privileges, but player chat is never an arbitrary command channel.
- Hard-deny `op`, `deop`, permission/group escalation, credential disclosure, command-block
  privilege escalation, unrestricted `execute`, and equivalent aliases or namespaced forms.
- `kick` and other bounded moderation actions require an enabled capability, authorized
  requester/group, structured target/reason, audit record, and configurable confirmation.
- Prompt injection, impersonation, quoted commands, books, signs, renamed items, and
  third-party messages cannot elevate permissions.
- Unknown commands and unknown permission-provider semantics fail closed.

Operations UI requirements:

- Manage `master`, `friends`, ordinary-player reply policy, permission groups, requester
  weights, rate limits, queue state, audit history, and configuration revisions.
- Runtime-safe settings may be changed through validated backend APIs. Minecraft config
  changes must declare whether they apply live, on reconnect, or only after restart.
- The contacts panel can be collapsed independently from direct backend conversation;
  the two histories and input targets must never be visually or behaviorally conflated.

### Phase 8: Death Recovery And Mod Compatibility

Status: `active` for discovery and design; no automatic tomb recovery is verified.

Current target adapter is Simple Tomb `1.21.1-1.4.4`. Recovery records the death
dimension ID, coordinates, death sequence, key binding, inventory baseline, and tomb
ownership. Modded dimension IDs are first-class strings and are never reduced to the
three vanilla dimensions.

Recovery policy:

- Automatic tomb teleport is disabled by default and requires an explicit backend setting.
- If the key and server configuration permit cross-dimension teleport, use the key directly.
- Otherwise route through a previously confirmed portal into the target dimension before
  using the key or local navigation. Unknown portals are not guessed.
- Tomb interaction requires ownership evidence. Inventory/equipment recovery is verified
  by before/after deltas, followed by deterministic re-equipping and layout restoration.
- Failure, permission denial, wrong dimension, missing key, stale tomb, or ambiguous
  ownership stops the workflow; no repeated teleport/use loop is allowed.
- Other grave/corpse mods use versioned adapters and remain documented as unsupported
  until encountered and tested.

WATUT compatibility can report successful programmatic actions only when explicitly enabled;
it defaults off and never synthesizes global hardware input. Anti-AFK activity also defaults off.
Inventory Sorter compatibility must prefer a stable API or deterministic inventory clicks;
it must not blindly synthesize R/middle-click events that affect unrelated screens.

### Phase 9: Safe Navigation, Projects, Farming, And Equipment

Status: `planned`; zero-light and confined-space wandering is a current safety defect.

Default autonomous movement must reject or strongly penalize zero-light cells, deep cave
descent, hazards, narrow dead ends, unverified drops, and routes without a bounded return
path. Explicit mining/rescue/project tasks may request a scoped risk lease with target,
purpose, time limit, retreat conditions, and resource budget.

Navigation quality must add the concepts present in mature game AI: node malus by hazard,
walk-target ownership, path recomputation limits, stuck detection, home/range restriction,
random-position scoring, reachable shelter, and safe fallback. LLM output does not directly
override collision, hazard, building, or retreat checks.

Coordinate and farming workflows:

- Track every online player's dimension and last authoritative coordinates.
- A request such as “come help me farm” first resolves the authorized requester's current
  dimension and coordinates, then navigates there and scans a bounded radius up to 128.
- Detect existing farmland before changing terrain. Use crop-compatible seeds and stable
  rows/plots; do not mix crops randomly or overwrite another owned plot without permission.
- Planting has explicit spacing, crop grouping, seed reserve, completion, and cancellation rules.

Large projects and bridging:

- Chunk excavation, slime-chunk excavation, stairs, ladders, scaffolding, and temporary
  bridges are durable projects with surveyed volume, materials, protected-region checks,
  egress plan, cleanup policy, checkpoints, and completion criteria.
- Base areas default to no ad-hoc pillar/bridge placement. Temporary blocks require an
  approved palette and cleanup ledger so autonomous construction cannot clutter the base.
- Ladder versus block stair/bridge selection considers available materials, return travel,
  fall risk, tool access, and project policy rather than model preference alone.

Equipment policy evaluates slot attributes, armor/toughness, enchantments, durability,
curses, current task, and replacement value deterministically. A model may resolve close
tradeoffs or social gifting intent, but cannot give protected equipment away without policy.
Items normally remain with the companion or may be offered to `master`; gifting to friends
requires explicit group permission and inventory reserve checks.

Persona presets may include optional themed profiles such as a cyber catgirl, but persona
text cannot alter safety, permissions, task admission, or credential policy.

Composable intents:

- `follow(player)` owns locomotion but may coexist with `guard(player)`. Guard may preempt
  briefly for a verified threat, then returns locomotion to follow.
- `hold(position)` anchors patrol/guard to a fixed center; `guard(player)` uses the protected
  player's current dimension and position as a dynamic center.
- Observation intents may watch nearby friends feeding animals, farming, building, taking
  damage, or facing hostile groups, then offer rate-limited assistance or warnings without
  taking movement ownership.
- Multiple intents share one arbiter and explicit actuator channels; they are not independent
  tick loops that overwrite movement, gaze, hands, or inventory.

World projects use protected X/Z regions that apply through all heights by default. Inside
protected regions, breaking, temporary pillars, bridges, fluid placement, and farm conversion
require explicit policy. The body reports current chunk X/Z and exact 16 by 16 bounds so a
request to excavate “this chunk” becomes a surveyed durable project rather than a guessed radius.

Recipe and collection tasks are iterative dependency graphs: inspect recipes, inventory,
known storage and workstations; acquire materials; upgrade tools when justified; craft;
verify output; recover or replan on bounded failure. Chat acknowledgement alone is never
a task completion signal.

Item requests distinguish concrete registry IDs from categories. Concrete craft outputs use
registry IDs; collection and inventory queries may use item tags such as `#minecraft:logs`
and `#minecraft:planks`, plus the synthetic `#lcu:wood` union. Recipe `Ingredient` tags remain
authoritative when choosing interchangeable inputs.

Container contents are temporary world observations keyed by logical container position.
Opening a container records a complete item-count snapshot and observation age; confirmed
withdrawals refresh it. Within the five-minute TTL, retrieval ranks containers already known
to contain the target before inspecting unknown containers. Vanilla double chests share one
snapshot/retry identity while retaining both halves as possible interaction points.

Combat tools distinguish assessment, target lock, one-hit test, defense, patrol, and sustained
engagement. Player targeting requires UUID authorization and trusted-player policy. Damage is
estimated from target armor/toughness, absorption, effects, weapon attributes, enchantments,
cooldown and server uncertainty; exact damage is observed after the hit rather than promised.

The operations console must expose hotbar, main inventory, selected/main hand, offhand, armor,
durability, enchantments, attributes, and optional Curios slots. Equipment decisions and gifts
must be explainable from this structured state.

### Deferred Voice Adapter

Status: `deferred` to protect the main body/task/safety workstream.

The future voice adapter selects explicit audio input and output devices. Voice-channel playback
is captured from a configured system/virtual audio input, then VAD, STT, and speaker identification
produce attributed conversation events. TTS audio is written to a configured virtual microphone
output consumed by the voice-channel client. Echo cancellation, consent, recording retention,
speaker confidence, interruption, rate limits, and privacy indicators are mandatory. Voice does
not bypass `master`/`friends` permissions or task admission and remains separate from public chat.

The operations console visual direction is a quiet macOS-inspired tool surface: restrained
translucency for navigation and dialogs, strong typography, stable controls, compact information
density, and responsive split views. Visual modernization must not delay or obscure body safety,
task state, permissions, audit history, or failure information.

Runtime state, model trigger, multi-agent artifact, dynamic tool discovery, screenshot,
and verified UI input contracts are specified in `docs/runtime-orchestration.md`. This
document is normative for future orchestration work: raw tick telemetry must not be copied
into model history, and no specialist agent may bypass the shared admission/executor path.

The complete vanilla operation baseline, current coverage, missing primitives, workstation
adapters, and implementation order are tracked in `docs/vanilla-capability-matrix.md`.
New gameplay features must extend this reusable capability surface before adding a
scenario-specific workflow.

TouhouLittleMaid is an additional Java reference for typed target memories, activity/work
packages, bounded sensors, cached reachability, farming phases, and cleanup. Only those
coordination patterns are portable: its server-side Mob navigation, direct world mutation,
direct item-handler access, teleport, health changes, and server-authority combat are not
valid implementations for LCU's multiplayer client player body.

## Existing Verified Foundation

The following foundation already exists and must not regress:

- V2 control leases with Java-confirmed ownership and fencing tokens.
- Durable runs, events, schedules, scope validation, and explicit queued-run resume.
- Disconnect/disarm behavior and `BODY_DISARMED` action rejection.
- Offline deterministic Skill metadata, Wire v3 body capability discovery, and local crafting execution.
- Concrete registry matching plus tag-based collection categories and explicit ore-to-drop mappings.
- Task-bounded storage discovery with world-scoped five-minute content snapshots.
- Initial body snapshot and normalized state/control/body events.
- Revisioned normalized World Model with authoritative snapshot collections, independent
  task/behavior/control overlays, fact provenance/TTL, disconnect staleness, and a bounded
  deterministic observation slice shared by planner, self-prompt, and private conversation.
- Body freshness, armed state, inventory, entities, and online-player telemetry.
- Operations console views for leases, Skills, runs, schedules, events, and resume.
- Operations console task presets, advanced/raw Skill mode, workflow step details,
  run detail pane, and body-availability-aware input forms.
- Runtime roles isolate normal player clients, headed AI body clients, and the
  dedicated-server fake-player placeholder. Fresh configurations default to `body_client`;
  ordinary conversation clients explicitly select `player_client`, while invalid values
  fail closed to the non-actuating player role.
- Common and dedicated-server entrypoints do not load client actuators. A real
  NeoForge dedicated server reaches ready state with fake-player activation disabled.
- Restricted player conversation APIs provide stable direct conversations,
  idempotent message delivery, SQLite history, contacts, and operator inbox access.
- The normal-player client has a restricted conversation screen; body controls and
  the operator token remain unavailable outside `body_client`.
- Client-only deployment is the primary headed-body topology: the AI client may
  join a server that does not install LCU. Other players do not need the mod unless
  they want the optional `player_client` conversation screen.

Latest recorded verification:

- Python tests: 216 passed with 1 intentional opt-in integration skip on 2026-07-19.
- Gradle: `clean build` passed on 2026-07-19.
- Explicit production Java-to-Python wire integration: 1 passed.
- NeoForge `runServer` smoke reached `Done` and logged both side-neutral common and
  dedicated-server startup without `ModLoadingException`, client-class loading, or
  `NoClassDefFoundError`.
- Browser SDK: `node --check` passed.
- Python environment: `pip check` passed.
- Repository whitespace: `git diff --check` passed.
- Dashboard: embedded JavaScript parsed successfully; authenticated player and
  operator conversation contacts/history round trips are covered by tests.
- Memory V2 status, records, export, summary preview/commit contracts, and local
  production-readiness reporting are covered by tests.
- Current headed-test JAR SHA-256:
  `0C811FAECD8C678706CC36B8FAA5D7F5AA878360894428188055DD3968CFD53D`.

The recorded JAR is deployed to the external FTB StoneBlock 4 test instance with
`runtimeRole="body_client"` for headed-body acceptance testing.

Pending headed-body verification, deferred to the next test session:

- A vanilla double chest opens once per logical-container inspection.
- High or otherwise unreachable storage is skipped without repeated path resets or open attempts.
- A one-iron-pickaxe request selects a bounded acyclic chain, prefers indexed intermediate
  materials, and otherwise expands logs to planks to sticks plus supported iron processing.
- Iron Chests and Sophisticated Storage menus expose real storage slots and permit confirmed
  one-slot-at-a-time withdrawal without touching player or auxiliary slots.
- A permitted body-chat stop preempts an in-flight Planner call and active operation; Java runs
  `stop_all` before older lower-priority work and every discarded request receives one terminal message.

## Release Blockers

- Provider-specific exact tokenizers and model metadata are not implemented.
- Scheduled background retention and legacy import/export round-trip validation are not implemented.
- Hard deletion is intentionally not implemented; development uses recoverable soft deletion.
- Production PostgreSQL storage and migration verification are deferred and remain
  a future production-release blocker, not a current development blocker.
- Java body intent arbitration and deterministic safety reflexes are not implemented.
- The `server_fake_player` role is a fail-closed configuration placeholder; entity,
  navigation, menu interaction, and BodyAdapter execution are not implemented.
- The normal-player conversation screen still requires manual in-game verification.
- A server-relayed, UUID-verified player payload gateway is not implemented; the
  current restricted player API is direct and requires HTTPS outside loopback.
- Real multiplayer acceptance with LCU installed only on the AI client remains a
  manual test gate; target-server mod policies may independently reject client mods.
- The 2026-07-19 crafting, storage, and category-target changes have automated coverage
  and a deployed artifact but still require the headed-body checks listed above.
- The new preset/console organization layer still needs in-browser manual verification even
  though backend and SDK tests cover the APIs and schema validation.
- Multi-step workflow execution is implemented locally but not deployed. Workflow schedules
  remain intentionally unsupported until schedule targets can persist preset versions and
  resolved step definitions atomically.

## Decision Log

### 2026-07-18

- Created this tracker to survive conversation-context compression.
- Restored model governance, memory management, and operations UI as the active workstream.
- Declared SQLite test/local-development only and PostgreSQL mandatory for production.
- Recorded Java-side intent arbitration as the required solution for gaze, mining,
  autonomy, backend-control, and safety conflicts.
- Confirmed the player-combat threshold as 2 hearts: at or below 4 health points
  escape without counterattack; above 4 points, only an attributed non-trusted
  attacker may be eligible for retaliation.
- Implemented and verified model request budgets, pre-network rejection,
  non-mutating fallback compaction, per-agent configuration, usage telemetry, and
  model governance controls in the operations console.
- Implemented memory browsing, provenance, redacted export, source-retaining
  durable summary preview/commit, schema version 4, and production startup
  fail-closed storage policy.
- Confirmed SQLite as the sole active development storage. Deferred PostgreSQL
  implementation until production deployment work begins while preserving storage
  boundaries needed for a later adapter.
- Completed the SQLite memory lifecycle loop: browse, export, summarize, archive,
  soft-delete, restore, retention preview/run, audit, and UI confirmation flows.
- Added `player_client`, `body_client`, and `server_fake_player` runtime boundaries;
  moved actuators and F12 body control behind explicit `body_client` activation.
- Added the restricted player conversation screen/API, persistent contacts/history,
  and the operator contacts inbox without granting player clients command access.
- Verified dedicated-server startup and fixed the runtime-role config validator so
  NeoForge's null validation probe cannot abort mod loading.
- Confirmed and documented that `body_client` and `player_client` do not require
  LCU on the target server; server installation is reserved for the future,
  currently unavailable `server_fake_player` implementation.

### 2026-07-19

- Upgraded the body handshake to Wire protocol v3 with machine-readable tool schemas,
  execution/completion classes, cancellation metadata, effects, and Python reconciliation.
- Replaced encounter-order crafting selection with bounded transactional exploration of all
  supported crafting and furnace recipe alternatives. Cycles reject only their branch;
  failed branches no longer corrupt the inventory ledger or missing-resource plan.
- Changed crafting execution to place and confirm one operation at a time, wait for the
  expected output/inventory delta, bound no-progress and station attempts, and terminate a
  parent craft when acquisition reaches a terminal failure.
- Added logical vanilla double-chest identity, reachable/visible interaction positions,
  owned-menu checks, sequential confirmed withdrawal, world-scoped content snapshots, and
  five-minute storage observation TTLs. Existing Iron Chests and Sophisticated Storage
  discovery remains enabled through generic container-slot inspection.
- Added category targets for logs, planks, and wood; removed fuzzy material-name matching
  and added explicit vanilla ore-to-drop collection edges.
- Advertised generic container read/take/put/close and item-drop primitives so Python can
  compose future retrieval and delivery workflows instead of adding Java scenario scripts.
- Built and deployed JAR SHA-256
  `1F262640A46AF10A689DC0A95F30BF2515325AA182D06991470314CF7A2A7D6F`.
  Headed-body acceptance was intentionally deferred to the next session.
- Researched Mineflayer, Mindcraft, Baritone, AltoClef, Voyager, Mojang-mapped
  workstation menus, and TouhouLittleMaid. Added the normative vanilla capability
  matrix and adopted typed targets, behavior phases, bounded sensors, work-mode
  packages, and authoritative postconditions as the Java-body direction.
- Implemented locally, but did not deploy, explicit operation outcomes and correlated
  cancellation plus addressed block/entity controls, generic validated menu clicks,
  recipe/button primitives, live recipe discovery, and semantic workstation slot roles.
- Added a declarative `TaskPresetRegistry`, preset list/detail/run APIs, SDK bindings, and
  control-console support for running preset-backed durable Skills or advanced/raw Skills.
  The initial preset catalog covers iron-pickaxe crafting, generic crafting, log collection,
  generic resource collection, coordinate travel, and eating.
- Added durable multi-step preset execution with persisted workflow parent/child runs,
  atomic next-step creation, progress and terminal propagation, correlated cancellation,
  explicit queued resume, restart-to-unknown safety, and per-step Operations UI details.
  Added `workflow.starter_chest` as the first composed preset.
- Added the first normalized World Model layer while preserving `session.runtime` as a legacy
  projection. Malformed facts no longer replace valid observations or refresh player/world
  freshness; omitted authoritative collections clear; LLM observations are canonicalized,
  deduplicated, nearest-first, and bounded before prompt construction.
- Added a bounded semantic journal and explicitly acknowledged decision-trigger queue. Snapshot
  churn is suppressed; inventory deltas remain informational while safety thresholds, dimension
  transitions, task terminal/blocking states, and established control transitions become typed
  decision boundaries without directly invoking a model or executor.
- Added an asynchronous proposal-only decision scheduler with world/body epoch binding, actual
  completion TTL, bounded retries, disconnect invalidation, durable lifecycle events, pinned
  Skill contracts, and atomic TaskCoordinator admission. Automatic execution is intentionally
  limited to revalidated `general.eat`; broader Planner actions remain blocked pending typed
  effect admission and deterministic safety arbitration.
- Routed structured chat Planner actions through typed proposals and TaskCoordinator durable-run
  admission. Removed direct Skills and legacy substring execution; malformed, fenced, multiline,
  prose-embedded, and multi-action model output now fails closed before body access.
- Added Planner generation invalidation and lock-free stop admission for permitted body chat.
  Java now runs `stop_all` at CONTROL priority, drains lower-priority work atomically, preserves
  CONTROL FIFO, and returns terminal messages for preempted requests. This batch is automated-
  test verified but remains undeployed pending headed-body acceptance.
- Audited public-server and anti-cheat exposure. Added independent policy controls for movement,
  world mutation, inventory/menu automation, combat, chat, autonomous behavior, anti-AFK, WATUT,
  background execution, auto-respawn, and surroundings telemetry. High-risk categories default off.
- Added a non-configurable protocol-safety floor: one queued command per tick, bounded wire backlog,
  grounded jumps, crosshair/visibility/cooldown-verified attacks, real block-hit/face validation,
  target-loss mining cancellation, and fail-closed client-only inventory mutation/drop paths.
- Added bounded, non-durable raw body-request diagnostics with correlated response/progress/outcome,
  secret redaction, timeout/disconnect uncertainty, SDK lookup methods, and a safe PowerShell headed-
  test driver for status, discovery, raw primitives, Skills, and presets.
- Upgraded the restricted player screen to a phone-style contacts/history view with persisted thread
  loading, scrolling, presence/status, responsive compact layout, and player+server scoped reads.
  Operator-issued HMAC pairing tokens now bind access to one player UUID + server ID scope; a future
  server relay is still required to derive that identity directly from an authenticated ServerPlayer.
- Made the unimplemented server fake-player configuration fail startup when explicitly enabled;
  no placeholder wire listener or executor is advertised.
- Unified one-build role selection around physical side plus explicit runtime role. The same JAR
  now defaults fresh client configurations to the primary headed `body_client` use case, keeps
  player conversation and server fake-player activation mutually exclusive, and stops the client
  body runtime during game shutdown. Future server multi-AI remains a per-body runtime registry,
  not a reuse of the client singleton executor.
- Scoped player-message idempotency receipts by conversation, added content-hash conflict checks,
  and atomically migrated existing SQLite receipts so one player's client message ID cannot expose
  another player's reply. Conversation history now returns the newest bounded page.
- Made raw body-request terminal diagnostics immutable to late events. Player conversation HTTP
  construction failures now surface asynchronously in the screen, while serialized read/send state
  and lifecycle generations prevent duplicate sends and stale callbacks after the screen closes.
- Hardened the first P0 body-safety slice: remote `toggle_ai` and hidden `shutdown` commands are
  rejected, `stop_all` and one-way `disarm` have emergency queue priority, active control leases
  do not block those safety commands, WireServer rejects blank tokens and isolates outbound frames
  by authenticated connection, pending sockets are closed during replacement/shutdown, and client
  world/logout/player lifecycle events invalidate active body work before reuse.
