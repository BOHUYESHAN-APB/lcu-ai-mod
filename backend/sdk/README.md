# LCU Companion SDK

Apache-2.0 licensed Python and JavaScript clients for integrating an external application with a running LCU Minecraft AI companion.

The SDK exposes three interface groups:

- Gateway: persona-aware chat and external context injection
- Observer: status, session, memory, model and configuration reads
- Actuator: authorized low-level Minecraft body commands

The companion backend remains a separate AGPL-3.0 program. See `../../docs/sdk.md` for API, authentication and browser CORS setup.
