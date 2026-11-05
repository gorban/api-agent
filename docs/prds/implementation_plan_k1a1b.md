# Integration requests and retained manual tracing

[Complete execution traces](implementation_plan_k1a1.md)

## Operation ownership

- [ ] Give the normal prepared Studio HTTP bridge an explicitly selected trace
  runtime and workflow observation factory. Bind the same runtime to final
  outcome producers; no ambient request may grant another runtime authority.
- [ ] Preserve one request through parsed graph/components/nodes, canonical LLM
  calls and named tools on the integration worker path. Reuse the existing journal
  and authenticated setup transport rather than recording execution twice.
- [ ] Carry a bounded, versioned explicit parent context through authenticated
  provider setup requests. Reject malformed, cross-owner or late context without
  adopting a process-global ambient span. Preserve canonical call and physical
  attempt identities, timestamps and exactly-once accounting.
- [ ] Keep trace payloads out of log/Allure queues. Apply independent destination
  policies and the full live credential authority before queues and native views.

## Retained manual ownership

- [ ] After finalized test accounting permits interactive requests, create a
  separate manual trace runtime/exporter and managed-collector lease. Never reopen
  a disposed test exporter or modify finalized test traces or Allure accounting.
- [ ] Capture collector planning bytes in the launch-owned protected snapshot;
  workers must not rediscover configuration or alter selected authentication.
  Remote collectors acquire no local infrastructure. Use existing shared managers
  for readiness, leases, retained storage and bounded retryable disposal.
- [ ] Close active manual requests and provider work before flushing exporters;
  release the collector only after SDK disposal is acknowledged. Optional tracing
  failures remain nonfatal with safe diagnostics; diagnostic logging retains its
  established failure policy. Preserve original manual errors and cancellation.

## Fields and proof

- [ ] Carry supplied provider/response identities, finish reasons, request options,
  cache/reasoning usage and typed safe errors through the shared runtime field
  contract. Preserve native scalar/array types and explicit unknown values;
  never infer provider facts from answer text or model-name substrings.
- [ ] Verify the normal admitted UI bridge, both parsed executors, authenticated
  setup propagation, post-suite manual turns, cancellation and unavailable export
  using serialized OTLP and actual finalized evidence bytes. Include all content
  flag combinations, causal controls, installed propagation and lifetime proof.
- [ ] Complete independent review, joined exact coverage, package/public-boundary
  gates and authorized backend viewer acceptance. Core checkpoint acceptance does
  not accept this extension or the remaining metrics/provider requirements.

## Version 1 source field contract

The validated shared provider owner supplies typed request/result facts to the
existing observation owner. It establishes served aliases separately from the
selected catalog identity. Raw SDK payloads cannot grant model identity. Optional
`telemetry_provider_name` is an explicitly admitted adapter declaration, a bounded
public semantic identifier subject to the live credential boundary. Known built-in
routes declare their provider; arbitrary compatible routes remain unknown. Host
owned generations consume these facts directly. Worker metrics omit labels for
which they lack source authority; model-name inference is forbidden.

The pinned GenAI registry maps maximum output to `gen_ai.request.max_tokens`,
temperature to a double, cache reads/writes to
`gen_ai.usage.cache_read.input_tokens` / `gen_ai.usage.cache_write.input_tokens`,
and reasoning counts to `gen_ai.usage.reasoning.output_tokens`. Counts include no
secret strings, booleans, floats, containers, negative or oversized values. Unknown
counts remain absent. Provider diagnostics admit bounded response identity, native
finish reasons and exact native reasoning level independently of content flags.
Generic result outcomes never invent a native finish reason. Missing native reasons
remain absent; shared outcome and provider response status use application fields.
Standard maximum tokens is recorded only from actual native dispatch diagnostics.
Each physical attempt retains its observed limit; the logical generation uses only
its final physical dispatch. A shared logical limit cannot overwrite that value,
and missing native evidence remains absent.
Safe typed failure categories use `error.type`; raw exceptions and stacks remain
under content authority. Supplied service tier/revision retain bounded application
attributes; unsupported values remain absent.

Native panels retain the independently projected provider shape. GenAI messages
select documented text/tool parts from canonical, Completions, Responses and
Converse forms; unknown fields, continuation and opaque reasoning are excluded.
This is one existing capture stream, not a second execution recorder.

## Optional cleanup outcome contract

- [ ] After every nontracing provider operation has stopped, the trusted setup
  owner may report `OptionalTraceCleanupPending`. The backend admits a receipt
  tied to that exact source owner and retains its existing loop, resumer, exporter
  and collector lease. An arbitrary provider exception cannot grant this status.
- [ ] Preserve passed E2E tests and finalized accounting while clearly reporting
  trace-only disposal pending in both interactive and noninteractive runs. Do not
  claim resources closed or release a collector until actual disposal is
  acknowledged. Existing inspection/cleanup handles perform explicit retry.
- [ ] Genuine provider, server and recording cleanup failures retain their failure
  policy and take priority over trace-only pending receipts. Warning/logging
  failures remain fatal. Do not change inference or accounting to hide a tracing
  loss, and do not add an automatic retry scheduler.

The focused proof uses the real admitted binding and SDK shutdown failure through
normal pytest publication in both modes, plus mixed failure priority and forged
receipt rejection. These checks do not replace independent review, installed
qualification, exact coverage or backend viewer acceptance.

Standard GenAI usage appears only on the logical generation: sum each known
physical attempt once, including recovered failures. Unknown executed attempt
usage makes that aggregate field unavailable. Physical spans retain known counts
under `api_agent.transport.usage.*`; backends must not sum parent and child
standard usage. Host logical end uses the terminal timestamp captured before
early result delivery, even when safe evidence projection finishes later. A
physical attempt without a known stop time is explicitly incomplete; its SDK
publication close time does not establish an original operation terminal time.
Interrupted unsettled calls retain the fixed `unavailable` capture reason under
all content permissions and exactly one logical loss. Their physical attempt
retains `api_agent.transport.terminal_known=false`, and both spans remain
explicitly incomplete. Direct publication of an unfinished ledger retains the
fixed `terminal_unknown` reason. Arbitrary provider reason strings remain withheld.
Authenticated internal setup-protocol EOF retains its existing typed contract
failure and logical SDK ERROR; an unfinished physical attempt stays UNSET with
unknown terminal time. Ordinary user HTTP disconnect and administrative
cancellation remain SDK UNSET. This distinction follows the explicit operation
cause, never exception prose.
Ordinary client cancellation uses SDK UNSET and no standard `error.type`.

Runtime disposal and remote fallback qualification also require:

- The HTTP server's lifespan cannot acknowledge a failed exporter shutdown.
  Its Runtime remains an ordered Studio cleanup duty. A stopped UI fixture may
  retain an admitted trace-only receipt and original loop in its existing
  inspection channel, with a safe warning and unchanged successful test result.
  Body failures, cancellation, logging and other cleanup failures retain priority.
  Missing nontrace-settlement or trace-failure acknowledgment remains generic
  pending cleanup; it cannot authorize optional trace-only treatment or release
  the suite lease before actual disposal.
- An authenticated inactive remote receipt owns local trace finalization until
  the existing bounded evidence job settles. Central accounting is immediate;
  trace metrics and the one local generation finalize once with that evidence
  and explicit parent. Granted host spans retain the original host terminal time.
  Synchronous inactive worker CLIENT spans measure through required pre-return
  projection; background early-result spans retain their worker terminal time.
  Inactive spans do not claim exact host execution duration. Granted/unknown handoffs do
  not retain duplicate worker trace buffers. Closed visits reject late work.
- Cancellation during partial SDK allocation retains the exact cleanup owner
  and propagates the original cancellation; explicit retry acknowledges disposal
  before collector release.

## Shared exporter startup

- [ ] Serialize concurrent first-call exporter startup and asynchronous disposal on each tracing runtime. Parallel callers must share one allocation, queued callers must report failed or cancelled startup as unavailable, and cleanup must not release the provider before startup settles. Verify delayed startup, concurrent first calls and close-during-start with deterministic barriers; preserve cancellation and retryable disposal semantics.

Failure response annotation requirements: [Failure content on generation observations](implementation_plan_k1a1f.md).
