# Bounded inference retries and physical-attempt evidence

[Shared adapters](implementation_plan_f1.md) · [Earlier GPT admission recovery](implementation_plan_f2c.md)

Transient upstream failures may follow an executed, chargeable generation. Recovery
must preserve that uncertainty while keeping application-defined graph retries
independent. This contract supersedes the earlier narrow GPT-only retry allowlist
for the supported shared completion path.

## Recovery policy

- [ ] Add shared typed `LLM_MAX_ATTEMPTS`, default five total attempts including the
  initial request, supported range 1..20. One disables retries. Resolve and validate
  it before authentication or inference; preserve ordinary/test override precedence.
- [ ] Retry normalized upstream HTTP 500..599 and 429. Other HTTP failures, local
  validation, cancellation and statusless transport failures remain terminal in
  this bounded contract. Preserve a known status even if its error body cannot be
  parsed. Preserve status and parsed retry hints before reading a failure body;
  never infer eligibility solely from an unavailable/unknown kind.
- [ ] Nominal waits are `min(30, 0.2 * 5 ** (retry_number - 1))` seconds, giving
  200ms, 1s, 5s and 25s for the default. Apply bounded jitter; a valid Retry-After
  value is a minimum and may exceed the algorithm's cap. Reject malformed/unbounded
  hints safely. Do not wait or dispatch beyond the existing overall deadline.
- [ ] Use one shared completion recovery loop after readiness and selection.
  Preserve selected credentials, identity, endpoint, model, generation settings and
  logical request throughout. Disable competing provider/SDK retry loops and close
  each response before a further request. Never replay authentication, tools, the
  whole workflow, or already delivered streaming output.
- [ ] One provider-owned inference deadline spans attempts and waits, clipped by
  enclosing deadlines; preserve provider-specific defaults. Cancellation interrupts
  waits and prevents another dispatch. Cleanup retains normal ownership and bounds.

## Evidence, accounting and presentation

- [ ] Retain one canonical logical call and a bounded nested physical-attempt ledger
  with distinct identities, ordered indexes, timing, execution disposition, safe
  status/error metadata, admitted retry delay and available usage/cost evidence.
  Snapshot usage/billing before consumer result validation and isolate each attempt
  so an earlier partial response cannot masquerade as the final response.
  Keep existing not_started/possibly_executed/completed semantics; a general 5xx
  must not falsely establish that inference did not start. Exclude confidential
  content and credentials from the ledger; existing content flags remain independent.
- [ ] Preserve completed and partial attempt records incrementally through normal
  application and test IPC. Reconcile a terminal snapshot against its admitted
  prefix. On disconnect or invalid terminal data, retain the prefix, mark evidence
  incomplete and acknowledge possible unobserved attempts; do not claim a zero
  charge or exact physical count beyond available evidence.
- [ ] Keep final response usage and metadata intact. When a ledger exists, account
  for its physical attempts once, without additionally charging the terminal result.
  Preserve known subtotals, unknown usage/charges and provider-owned estimate basis;
  do not turn partial estimates into complete totals or invoices. Preserve historical
  records without a ledger and exact canonical call/turn/conversation attribution.
- [ ] Recovered transport retries produce yellow attention within their logical
  call and graph ancestors. A terminal logical error remains red. Preserve truthful
  physical-attempt diagnostics and tracing without red child status permanently
  poisoning recovered attention. Never alter flow.md.html attempt or Max passes
  values, application retry state, tool grants or the authoritative test outcome.

## Acceptance

- [ ] Verify status/hint admission, configurable bounds, exact nominal schedule,
  jitter, five-attempt exhaustion, deadline clipping, cancellation and unchanged
  request/auth identity. Causal controls reject absent or multiplied recovery.
- [ ] Verify known/unknown usage and quotes across recovery, failure, cancellation
  and broken IPC; reject forged, duplicated, conflicting, reordered or oversized
  ledgers. Prove exactly-once logical and physical accounting and legacy readability.
- [ ] Prove yellow recovery, red exhaustion, unchanged semantic counters and no
  tool replay through normal application/test capture and frozen Allure projections.
- [ ] Complete exact applicable unit coverage, meaningful integration checks,
  source/installed/relocated resources, independent/external review and public
  boundary/license/index/metadata gates. Preserve failed evidence.
- [ ] Human review confirms visible recovery, bounded stopping, accounting and
  unchanged application retry semantics.
