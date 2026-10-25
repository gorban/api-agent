# Shared parsed-flow content capture

[Independent content controls](implementation_plan_k1b.md)

Every supported parsed workflow must obtain node capture from the shared runtime,
without application-specific capture wrappers or Python field lists. This applies
to both schema-bound and stateful declarations, including nested components,
retries, fixture graphs and resource-pack selections.

- [x] Capture validated node inputs and outputs in both executors through one bounded JSON-only projection. Respect declared schema fields and explicit restrictive capture contracts. Do not call arbitrary object serialization, repr or str methods. Preserve execution results when content cannot be represented.
- [x] Capture actual visible LLM request messages and completion/tool content at the in-process shared provider invocation boundary, associated with the current node visit and attempt. Pair request and response observations without inventing accounting receipts, multiplying usage or retaining raw request objects. Application/demo adapters must not implement their own capture wrappers. Separate backend processes retain the propagation dependency below.
- [x] Apply LOG_CONTENT independently to retained journals, Allure and live diagnostics, and TRACELOOP_TRACE_CONTENT independently to OTEL and LangFuse. Project before retention or exporter queues; neither destination may retrieve raw payloads from the other. Missing or denied authority withholds content.
- [x] Preserve unconditional credential, authentication/continuation-state and hidden-reasoning exclusions, including nested JSON text. Bound depth, field/item counts, payload sizes and previews; record unavailable/truncated evidence rather than silently suggesting complete capture. Keep structural execution and accounting available with both flags disabled.
- [x] Verify every flag combination for both backends using serialized synthetic collector payloads and actual journal/evidence bytes. Exercise a parsed graph without capture declarations, the bundled planning/review/tool graph, stateful graphs, retries/concurrency, unsupported values, errors and cancellation. Include causal controls for missing shared capture and conflated flags.
- [x] Close exact applicable coverage, packaging/public-boundary checks, independent review and external review before human review. Preserve previously qualified sampling evidence only with an independently reviewed execution-equivalence proof; do not claim new live qualification.
- [x] Human approved the limited shared-runtime tracing scope on October 24, 2026. Jaeger conversation-panel mapping, native observation presentation, complete durable test evidence and broader instrumentation/metrics retain their separate requirements.

## Separate backend processes

Automatic node I/O admission belongs to both executors wherever their runtime
supplies an observation adapter or recorded journal. Actual provider message
pairs require an active workflow visit at the in-process provider boundary.
The separate integration-test backend currently receives neither that visit nor
independent trace-content authority. Its existing inference evidence is LOG_CONTENT
gated; it cannot supply trace-only content. Keep [worker/provider policy and
identity propagation](implementation_plan_k1b.md) unchecked until qualified;
caller-side raw capture must not bypass backend credential registrations.

Integration/E2E acceptance does not require the separate test backend to export
OTEL or LangFuse traces. Its required destinations are logs and Allure, using
the same flow-aligned execution data. This does not waive separate application
tracing requirements or authorize trace-only data to enter log/evidence stores.

## Capture admission ordering

- [x] Reserve journal admission before reading capture permission or projecting JSON. A stricter policy cannot attach during an active first capture and receive data authorized only by the original policy. Verify the interleaving with two threads, inspect retained and live output, and use a causal control that removes only the capture-start guard.

## Complete test evidence follow-through

- [ ] Deliver [complete flow-aligned test evidence](implementation_plan_b1c1a2.md) for logs and Allure before accepting historical input/output overlays. Shared node capture and its current 512-character retained previews establish a capture seam, not full durable payload storage. Keep this requirement separate from remote tracing export and preserve the shared destination gates.
