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
- [ ] Close exact applicable coverage, packaging/public-boundary checks, independent review and external review before human review. Preserve previously qualified sampling evidence only with an independently reviewed execution-equivalence proof; do not claim new live qualification.
- [ ] Human acceptance after restarting Run local: enabled trace content appears for the selected graph; disabled destinations remain withheld. Jaeger conversation-panel mapping and broader instrumentation/metrics retain their separate signal-conformance requirements.

## Separate backend processes

Automatic node I/O admission belongs to both executors wherever their runtime
supplies an observation adapter or recorded journal. Actual provider message
pairs require an active workflow visit at the in-process provider boundary.
The separate integration-test backend currently receives neither that visit nor
independent trace-content authority. Its existing inference evidence is LOG_CONTENT
gated; it cannot supply trace-only content. Keep [worker/provider policy and
identity propagation](implementation_plan_k1b.md) unchecked until qualified;
caller-side raw capture must not bypass backend credential registrations.
