# Complete execution traces and terminal outcomes

[GenAI conformance](implementation_plan_k1a.md) · [Independent content controls](implementation_plan_k1b.md)

This pending extension covers the entire application operation, including work
that returns a typed failure without raising. Existing bounded local tracing is
not full signal or end-to-end request conformance.

Coordinate backend selection and field mappings with the
[resource-pack tracing-provider contract](implementation_plan_k1a2.md), preserving
one shared capture path across built-in and pack-selected implementations.

## Execution boundaries and outcomes

- [ ] Keep one explicitly owned inbound request trace through graph entry,
  recursive components/nodes, LLM calls, tools and final response validation.
  Streaming ends after its actual completion, error or disconnect, rather than
  at initial headers or generator creation. Close every started scope exactly
  once on success, exception, timeout, cancellation and early iterator close.
- [ ] Preserve explicit parent identities across tasks, worker/remote boundaries
  and continuation turns. Never adopt an unrelated ambient span or share another
  runtime/account's context. Trace/span log correlation uses actual SDK context;
  application operation IDs are separate identities.
- [ ] Give post-suite retained DEBUG requests their own tracing owner and exporter
  lifetime. Keep them separate from finalized test traces and Allure accounting;
  preserve manual failures, content controls and exporter-before-collector shutdown.
- [ ] Agree a versioned typed outcome boundary before implementation. An ordinary
  returned object, empty result or HTTP 200 cannot alone prove application success.
  Distinguish terminal failure from retrying, recovered, partial and fallback
  outcomes. Preserve the precise safe cause at the originating scope and final
  request without duplicate error logs or accounting. Do not classify outcomes
  by matching assistant prose, application-specific field names or exception text.
- [ ] Apply the agreed recovered-exception policy to caught structured-output
  parsing and retries, avoiding false terminal failures. Allure's retry attention
  colors are a separate presentation policy, not the final request error policy.
  Preserve genuine provider/tool failures and SDK error status independently of
  content capture permission. Mutating an ended span is never a propagation fix.

## Fields, signals and privacy

- [ ] Maintain a version-pinned field matrix for spans, resources, events, logs
  and metrics. Include provider and response identities, finish reasons, request
  options, cache/reasoning usage when supplied, named tool instance/call identity,
  operation correlation, final usage and safe errors. Unknown source data remains
  unavailable; optional capability must not disappear merely because one run
  omitted its value.
- [ ] Preserve native OTLP scalar/array types, units, omission and placement.
  A string array must not become JSON text. Structural fields remain useful with
  trace content disabled. Distinguish permitted structured message serialization
  from incorrectly stringifying every compound attribute.
- [ ] Qualify GenAI duration/token histograms and configured HTTP/system/process
  signals separately from span token attributes. Preserve metric version,
  resource attribution, aggregation/temporality and exactly-once leaf accounting.
  Environment-based endpoint/protocol/resource settings must obey documented
  precedence without changing the selected tracing backend silently.
- [ ] Keep TRACELOOP_TRACE_CONTENT authoritative for both tracing backends and
  LOG_CONTENT independent for logs/Allure. Gate before SDK queues; retain mandatory
  credential and hidden-reasoning exclusion across attributes, events, links,
  status text, resources, logs and externally instrumented clients. Error messages
  and stack traces are content, not safe structural labels. Treat native backend
  input/output panels as distinct mappings from custom capture events.
- [ ] Expose bounded capture loss and failed delivery without blocking inference
  or acknowledged shutdown. Do not silently label an incomplete trace complete.

## Verification

- [ ] Exercise both parsed-flow executors with independently authored scenarios:
  normal completion, thrown and returned errors, rejected/partial tools, retry
  recovery, exhausted loops, post-graph validation, stream cancellation, worker
  propagation and shutdown/export failure. Check scope closure, ancestry, original
  timing and final statuses on actual serialized OTLP, not only mocks or logs.
- [ ] Cover all content-flag combinations, type/case drift, absent optional fields,
  correlated JSON logs, resource identity and metric units. Require causal controls
  for dropped final errors, incorrect retry classification, detached tools, missing
  correlation and array conversion. Verify source and relocated installation,
  applicable exact coverage, independent/external review and separate authorized
  backend AI-view acceptance. Public synthetic collector proof does not establish
  a deployment's credentials, collector transforms or observed backend behavior.

Public bases: [GenAI conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md),
[error recording](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/)
and [AI monitoring](https://docs.newrelic.com/docs/ai-monitoring/opentelemetry-ai-monitoring/).
Pin supported revisions when implementing rather than following moving heads.

[Integration and retained manual extension](implementation_plan_k1a1b.md) owns the
normal bridge, authenticated propagation and separate manual lifetime requirements.

[Core checkpoint and pinned field matrix](implementation_plan_k1a1a.md) records
its implemented scope and remaining qualification gates.

## Version 1 implementation contract

`telemetry.outcomes.ExecutionOutcome` carries a schema version, final state,
bounded cause category and optional canonical cause identity. Final states are
`success`, `failure`, `cancelled`, `partial` and `unknown`; transition observations
`retrying`, `recovered` and `fallback` never replace an already reported final
failure. Producers explicitly report returned failures at their typed boundaries;
HTTP status, assistant prose and arbitrary output dictionaries cannot establish
application success. A returned failure is retained at its originating scope and
the open operation owner before either span ends. Post-graph validation remains
inside the operation. Ordinary cancellation and client disconnect leave SDK
status unset; timeout and genuine terminal failure set ERROR with a bounded
`error.type`. Successful recovery leaves the request status unset, retaining
failed leaf attempts independently. No exception message or traceback is structural.

Each operation owns an explicit parent, inherited by its tasks through a scoped
context binding restricted to the same runtime. Graph observation adapters attach
to that owner and preserve the journal's wall-clock boundaries. Named tools use
the existing tool lifecycle, not deterministic-call inference. Admitted provider
capture is mapped to native generation/tool input and output attributes before
SDK queuing; it never enters an ungated shared payload cache. Content controls,
credential registration and opaque-state exclusions remain mandatory.

This contract does not by itself close the provider declaration, metrics,
remote continuation or dashboard acceptance requirements. Those remain explicit
unchecked gates until implemented and verified.

## Terminal stream delivery

- [x] Once Studio has emitted its validated final result and its generator resumes after the terminal completion frame is sent, retain the completed application outcome when the client normally closes that stream. Use an explicit request-owned completion signal, never infer success from HTTP status or payload text. Disconnects before completion remain cancelled; cancellation or failure while sending the terminal frame stays cancelled or failed. Genuine post-completion errors remain failures, and completion cannot erase a prior failure or partial result. Record send cancellation before a framework can consume it. Verify the ordering through the actual Django streaming response and retained collector data.

## Human acceptance

- [x] Run local tool tracing, including executed tool calls and their input/output: human-approved November 3, 2026.

Manually constructed integration-fixture tracing remains pending under
[integration operation ownership](implementation_plan_k1a1b.md).

[HTTP trace diagnostics and safe request names](implementation_plan_k1a1d.md)
cover probe filtering and finite structural HTTP metadata without granting content
capture authority.
