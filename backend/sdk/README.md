# LCU Companion SDK

Apache-2.0 licensed Python and JavaScript clients for integrating an external application with a running LCU Minecraft AI companion.

The client libraries expose three interface groups:

- Gateway: persona-aware chat and external context injection
- Observer: status, session, memory, model and configuration reads
- Actuator: authorized low-level Minecraft body commands

## Server-side reality (26.3)

The clients implement more than this branch's backend serves. The backend currently mounts only five SDK/V2
routes: `GET /api/sdk/info`, `POST /api/sdk/chat`, `POST /api/sdk/command`, `GET /api/v2/control` (read-only) and
`GET /api/v2/audit` (read-only).

Everything else the client methods call — typed Skill discovery and execution, renewable `external` control
leases, durable UUID skill runs, cursor-based events, wall/game-clock schedules, player pairing, identity —
has **no server implementation on this branch** and returns 404. That is a client/server gap, not a bug in the
clients: the V2 surface was not ported.

Use `get_sdk_info()` and read its `implemented` / `missing` lists rather than assuming a method name has a route
behind it. See `../../docs/sdk.md` for the full endpoint table and the known gaps.

The companion backend remains a separate AGPL-3.0 program. See `../../docs/sdk.md` for API, authentication and browser CORS setup.

Optional integrations use discoverable, versioned adapter manifests. Adapters must
declare capabilities, schemas, permission domains, runtime placement, failure semantics,
source URL, and license obligations. Missing capabilities fail explicitly as unsupported.
Voice I/O is a deferred adapter surface and is not part of the current SDK release.
(No manifest loader ships on this branch yet.)
