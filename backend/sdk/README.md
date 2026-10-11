# LCU Companion SDK

Apache-2.0 licensed Python and JavaScript clients for integrating an external application with a running LCU Minecraft AI companion.

V1 exposes three interface groups:

- Gateway: persona-aware chat and external context injection
- Observer: status, session, memory, model and configuration reads
- Actuator: authorized low-level Minecraft body commands

V2 adds typed Skill discovery/execution and renewable `external` control
leases. External controllers should prefer Skill runs over
raw actuator commands.

Skill runs are durable UUID resources with response/progress terminal states.
V2 also exposes cursor-based events and persistent wall/game-clock schedules.

The companion backend remains a separate AGPL-3.0 program. See `../../docs/sdk.md` for API, authentication and browser CORS setup.

Optional integrations use discoverable, versioned adapter manifests. Adapters must
declare capabilities, schemas, permission domains, runtime placement, failure semantics,
source URL, and license obligations. Missing capabilities fail explicitly as unsupported.
Voice I/O is a deferred adapter surface and is not part of the current SDK release.
