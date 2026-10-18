# Application chat HTTP contract

[Prepared sessions](implementation_plan_i2c.md) · [Operation lifecycle](implementation_plan_i2c4.md)

The included Studio and alternate UI adapters consume one generic application
contract. Provider transport and authentication values remain internal.

- [x] Return catalog entries with `value`, `label`, description, features, version,
  output/context limits, family and optional reasoning choices. Resolve one exact
  offered default through selection policy. Return `reasoning_default` as the
  explicitly configured effort or null for provider deferral; never infer it from
  supported choices. Preserve omitted reasoning choices versus an empty list.
- [x] Accept a fresh operation `session_id` (canonical UUID or null to allocate),
  optional model, role/content messages and bounded optional generation controls.
  Missing model selects the exact catalog default. Omitted reasoning/cache/output
  settings defer through existing provider semantics; explicit reasoning accepts
  only off/low/medium/high. Reject empty/null reasoning, unknown fields, duplicate
  JSON keys, nonfinite numbers and malformed/oversized bodies before inference.
- [x] Return JSON results with session identifier, response text, outcome, measured
  usage and optional actual model, service tier, serving revision and billing.
  Validate provider results using their existing contract before publication.
- [x] Stream named SSE `session`, `token`, `response_complete`, `result`, `done`;
  safe `error` and `cancelled` are terminal alternatives. Session announces a
  ten-second heartbeat interval and thirty-second lease. Complete-only providers
  emit one honest content chunk; optional managed provider streams emit genuine
  incremental text and must end with matching terminal result/accounting. Bound
  each frame's raw/escaped JSON bytes to 131072, total encoded SSE to 1 MiB
  and event count to 8192, reserving space for a safe terminal error. Request
  bodies and JSON results retain their independent 131072-byte contract.
- [x] Authenticate models/chat/stream/heartbeat/cancel using exact loopback Origin
  and ephemeral nonce, including route-specific preflight. Heartbeat/cancel bodies
  contain only session identifier. Return explicit unknown for stale identifiers;
  cancellation is idempotent and acknowledges completed disposal only. Preserve
  genuine operation deadlines independently of heartbeat liveness.
- [x] Transfer operation/context ownership to the bounded lifecycle engine. Stream
  disconnect, unconsumed response expiry, explicit cancel and shutdown dispose
  actual work before terminal success; retain retryable cleanup receipts and
  authentication dependencies when disposal remains pending. Register exact
  response-source disposal with the ASGI request before creating wrappers;
  disconnect during a blocked network send must settle operation and iterator
  duties before returning, or surface retained typed cleanup ownership. Bound
  never-consumed response ownership and close unscheduled cleanup coroutines.
  Keep first-class callback/task/receipt state: retire acknowledged disposal
  before preserving waiter interruption, follow transferred child retries once
  across distinct callbacks through registry-wide receipt/task identity, and
  settle independent siblings after synchronous or nested failures. Bound generic
  callback waits to a fresh five-second cleanup budget; retain a hung task without
  cancelling or restarting it on retry. Shutdown signals all operations and
  attempts every response/context, retaining authentication until all settle.
- [x] Renew applicable exact coverage, independent adversarial review, installed
  package and public-boundary gates before accepting this delivery.
- [ ] Human review of the joined included Studio and alternate adapter behavior.

Continuation/approval execution and result-set storage are unavailable until
implemented by an actual capability. Existing unavailable routes retain explicit
501 responses. Frontend rendering support does not claim backend execution.

Controlled tests exercise authored catalog/request/result mapping, admission,
named events, managed stream closure, post-header errors, real ASGI disconnect
during both iteration and blocked send (with retained response references),
never-iterated response expiry and retained cleanup. Observer allocation rejection
closes unsubmitted native coroutines and drains
the existing observer heartbeat before surfacing failure; normal deadlines and
clock behavior remain unchanged. This does not renew separate workflow
qualification or authorize a paid pilot.

No live authentication, inference or report-history mutation is required by this contract work.

The joined unit gate passes 10,319 tests and 27 subtest records at exact 100% line and branch coverage. Controlled HTTP checks, the installed wheel from a different working directory, licenses and source-boundary checks pass. The shared callback registry deduplicates transferred receipt duties across different callbacks and overlapping waiters.
