# External upstream API extension requirements

Implement system-specific upstream behavior in a separately owned external provider, not core or the included GraphQL provider. This is a generic integration task list; endpoints, signing fields, identity rules, fixtures and implementation evidence belong to that provider's own documentation/tests.

## Checklist

- [ ] Implement the versioned [GraphQL extension contract](implementation_plan_m1.md), selected by an explicit upstream factory. Another API implementation may define its own extension protocol without changing core.
- [ ] Contribute typed authentication/endpoint/metadata configuration, applicability and secret declarations. A resource pack may override provider defaults through declared overrides; do not commit private defaults in the public core/demo.
- [ ] Implement upstream authentication/signing and identity relationships only where its actual protocol requires them. Do not bind API principals to an unrelated personal GPT credential; access-adapter identity checks remain scoped to that adapter. Reject invalid/expired credentials before execution and test the intended rejection with otherwise valid inputs and focused mutations.
- [ ] Expose optional route registration for upstream authentication/recovery before successful login. Use generic runtime services; prevent route/configuration collisions and do not make resource-pack browser hooks prerequisite to route availability.
- [ ] Provide reusable UI cookie/session preparation if the selected resource pack needs it. The pack chooses its mode strategy and calls it from the pack's `authenticate` hook; core passes the existing session and never learns cookie contents or signing rules.
- [ ] Keep integration/test identity preparation separate from UI navigation. When a pack uses a deterministic test identity, implement and verify it in that owner; it must not become an auth requirement for unrelated packs or the demo renderer.
- [ ] Register secrets before diagnostic emission, respect verified TLS and own all transport/background cleanup. Do not export private protocol details or fixtures through generic exceptions, help examples or public test artifacts.
- [ ] Run extension-owned protocol tests and generic composition conformance checks. Review actual selected settings and pack identity strategy before live upstream operations; preserve public synthetic tests as independently runnable.
