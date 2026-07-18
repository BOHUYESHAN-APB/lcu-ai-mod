# LCU Project Tracker

Updated: 2026-07-18

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
- Unknown containers are not opened merely to discover their contents.
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

## Existing Verified Foundation

The following foundation already exists and must not regress:

- V2 control leases with Java-confirmed ownership and fencing tokens.
- Durable runs, events, schedules, scope validation, and explicit queued-run resume.
- Disconnect/disarm behavior and `BODY_DISARMED` action rejection.
- Offline deterministic Skill metadata and local crafting execution.
- Strict namespaced item matching and inventory-delta crafting counts.
- Known-container-only storage behavior.
- Initial body snapshot and normalized state/control/body events.
- Body freshness, armed state, inventory, entities, and online-player telemetry.
- Operations console views for leases, Skills, runs, schedules, events, and resume.
- Runtime roles isolate normal player clients, headed AI body clients, and the
  dedicated-server fake-player placeholder. The default role is `player_client`.
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

- Python tests: 129 passed with 1 intentional opt-in integration skip on 2026-07-18.
- Gradle: `test --no-daemon` passed.
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
- Shared-server JAR SHA-256:
  `21325CC799DD7A0ECB96E4278832BE00941ACDDF55E8CF141F8813FDB014F8A4`.

The recorded JAR is deployed to the external FTB StoneBlock 4 test instance with
`runtimeRole="body_client"` for headed-body acceptance testing.

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
