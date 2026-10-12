# Shared integration authentication

Ordinary and DEBUG integration runs use one lazy authentication backend for the selected provider and account. Base, resource-pack and selected-provider suites share that backend, including parallel pytest workers. Requesting the `llm_provider` fixture is inert; `ensure_ready()` acquires authentication; `describe()` additionally verifies remote catalog access; `complete(request)` executes an explicit text request without listing first.

## Ownership

The launcher owns a backend thread and its creating event loop. The backend freezes the operation configuration and creates one provider/session owner on demand. Simultaneous readiness requests coalesce. Later checkpoints reuse that owner so the provider can refresh its session. Concurrent discovery requests also share one cached catalog result. Failed setup or discovery remains failed for that operation; a new operation is required to retry it.

Workers receive an opaque readiness acknowledgement, bounded catalog metadata or a bounded text completion result with usage. Provider tokens, password-manager readers and browser objects stay in the backend. No worker credential-retrieval endpoint is exposed: the backend uses the existing scoped, revocable reader for the selected role/account/kind.

Shared authentication runs under its own component deadlines. A short worker request limits that worker's wait, not the authentication needed by a longer-lived sibling. Timing reports still reach every waiting test.

Each worker receives a lease bound to the operation, selected identity, worker and attempt. Invalid, stale or revoked leases fail before provider access. Disconnecting or cancelling one worker revokes its lease and waiter without cancelling shared setup needed by another worker. Ending the operation revokes all leases before disposing authentication and helpers.

## Communication and reporting

The versioned protocol listens on an ephemeral IPv4 loopback port. Its operation capability is delivered only through the existing protected launcher snapshot. Requests and responses have bounded frames and strict shapes. A client cannot select a different provider/account or shut down the operation.

Approved prerequisite and interaction errors retain their actionable explanations. Provider-normalized access errors preserve safe upstream messages, HTTP status, code/type and request identifiers. Unknown failures use a fixed diagnostic. Component timing is relayed to waiting tests with neutral component labels and remapped identifiers; nested budgets and elapsed time remain available without sending provider-supplied identifiers or names.

Ordinary backend disposal precedes publication. DEBUG stops and drains worker calls, captures the final quota, then publishes results while retaining the authentication/inspection owner. Provider disposal afterward leaves only inspected browsers alive. Terminal/TUI consumers own every unacknowledged cleanup handle and the suite lease until confirmed disposal; closing all windows starts the same synchronous cleanup path. Failed cleanup retains the creating loop and retry receipt.

Worker socket disposal and its acknowledgement use fresh bounded cleanup scopes, independent of the readiness request. An expired request cannot consume those cleanup budgets. Successful cleanup preserves the request failure and revokes the disconnected lease before session teardown; genuinely unconfirmed disposal still retains a retry owner.

## Current boundary

DEBUG uses configured parallel workers and one backend-owned inspection service. Its original contexts/pages remain inspectable after workers exit or inspection clients disconnect. A stopped backend loop is restarted for bounded cleanup, inspection waiters are owned independently, and publication callbacks cannot start a second runner operation. Controlled Linux launcher-termination proof covers only recorded owned driver/browser identities; broader platform recovery remains a follow-up. Ordinary integration acceptance and the human E2E gate are unchanged. This backend supports readiness, catalog discovery and text completion. Each completion is a separate owned call; results are never cached. Cancelling or disconnecting a worker stops its inference before acknowledging disposal, without cancelling shared authentication. API execution, tools, structured output and public incremental streaming remain later consumer contracts.

Help, units and collection do not start this backend. Integration startup allocates its communication resources after prerequisite preparation; it loads no provider or browser until a test requests readiness, discovery or completion.

See [contract hardening](../prds/implementation_plan_m3.md) and [lazy setup](../prds/implementation_plan_f2b.md) for remaining work. Synthetic multiprocess tests prove the ownership boundary separately from live authentication acceptance.

Catalogs contain selection IDs, display names and optional family, version,
output-token and context limits. Providers normalize their own documented IDs;
the shared agent never infers a family or version from vendor spelling. Unknown
metadata stays absent. Strict local/wire validation applies the same count, text and aggregate byte
limits before any result reaches a worker. No raw response or key metadata is
exported. Short waiter deadlines and lease revocation cannot poison a sibling's
shared discovery; operation disposal drains the catalog before releasing auth.

## Text-request checkpoint

`LLM_ALLOWED_MODELS` full-matches model IDs to filter the shared catalog and
reject explicit disallowed requests before provider loading or authentication.
`LLM_DEFAULT_MODEL` full-matches that allowed catalog. One match is selected
without comparison; multiple matches require one provider-described family,
comparable versions and a unique highest version. Missing metadata, no matches
and tied highest versions fail explicitly. Regex evaluation has bounded input
and execution; malformed patterns fail configuration validation.

The base defaults allow GPT-6 Luna, Haiku and Sol and choose the newest Luna.
Providers/resource packs and .env/ENV/CLI may override these generic declarations.
`llm_selection` resolves this default for the small shared request integration;
it requires catalog access. Explicit allowed `complete(request)` calls still do
not require discovery permission. `cfg/tests.toml` declares provider test roots,
not hardcoded inference choices. The test sends one short user turn and requires
completed nonempty assistant text. Worker codecs reject unsupported features,
malformed values and aggregate sizes before execution or result return.

The public Django model-list route is still deferred. Its implementation must
use the same filtered catalog and default-selection policy; public inference
must enforce admission even when a caller bypasses the UI picker.

Integration tests may spend tokens. Interactive users start selected tests;
noninteractive integration requires `START=True` (also accepts `START=E2E`) or `DEBUG=True`,
and noninteractive E2E requires `START=E2E`. There is no additional inference
selection acknowledgement. Keep small tests valuable; periodic default-skip
review is a later task.

Authentication stays with the selected operation owner. Completed usage and
attempted user turns reach the suite's CSV/summary artifacts through content-free
worker events; missing token totals remain unknown. Tests do not log prompt or
reply text for accounting. Both serial and parallel recorded checks exercise the
real loaded factory and assert safe error propagation and credential exclusion.
