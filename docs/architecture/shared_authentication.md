# Shared integration authentication

Ordinary integration runs use one lazy authentication backend for the selected provider and account. Base, resource-pack and selected-provider suites share that backend, including parallel pytest workers. Requesting the `llm_provider` fixture is inert; `ensure_ready()` is the authentication boundary.

## Ownership

The launcher owns a backend thread and its creating event loop. The backend freezes the operation configuration and creates one provider/session owner on demand. Simultaneous readiness requests coalesce. Later checkpoints reuse that owner so the provider can refresh its session. A failed setup remains failed for that operation; a new operation is required to retry it.

Workers receive an opaque readiness acknowledgement. Provider tokens, password-manager readers and browser objects stay in the backend. No worker credential-retrieval endpoint is exposed: the backend uses the existing scoped, revocable reader for the selected role/account/kind.

Shared authentication runs under its own component deadlines. A short worker request limits that worker's wait, not the authentication needed by a longer-lived sibling. Timing reports still reach every waiting test.

Each worker receives a lease bound to the operation, selected identity, worker and attempt. Invalid, stale or revoked leases fail before provider access. Disconnecting or cancelling one worker revokes its lease and waiter without cancelling shared setup needed by another worker. Ending the operation revokes all leases before disposing authentication and helpers.

## Communication and reporting

The versioned protocol listens on an ephemeral IPv4 loopback port. Its operation capability is delivered only through the existing protected launcher snapshot. Requests and responses have bounded frames and strict shapes. A client cannot select a different provider/account or shut down the operation.

Approved prerequisite and interaction errors retain their actionable explanations. Unknown failures use a fixed diagnostic. Component timing is relayed to waiting tests with neutral component labels and remapped identifiers; nested budgets and elapsed time remain available without sending provider-supplied identifiers or names.

The backend is disposed before suite publication. Failed disposal retains a cleanup handle and the creating loop for retry. Existing terminal/TUI cleanup consumers own that handle until disposal is acknowledged; it does not represent a retained browser.

## Current boundary

DEBUG keeps the existing serial worker-local browser inspection path. Moving retained debug browsers to the shared backend remains a separate task. Ordinary integration acceptance and the human E2E gate are unchanged. This backend supports setup readiness, not remote inference or API execution; those consumers need their own ownership contracts.

Help, units and collection do not start this backend. Integration startup allocates its communication resources after prerequisite preparation; it loads no provider or browser until a test requests readiness.

See [contract hardening](../prds/implementation_plan_m3.md) and [lazy setup](../prds/implementation_plan_f2b.md) for remaining work. Synthetic multiprocess tests prove the ownership boundary separately from live authentication acceptance.
