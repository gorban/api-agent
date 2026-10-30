# Independent logging and tracing content controls

[Tracing](implementation_plan_k.md) · [Signal conformance](implementation_plan_k1a.md) · [Log and evidence privacy](implementation_plan_j.md)

The bounded manual runtime now implements separate trace and log policies, with technical qualification complete and human review pending below. Preserve accepted credential-redaction behavior and existing diagnostic/Allure capture policy; broader automatic instrumentation remains separate work.

## Configuration and destination policy

- [x] Declare `TRACELOOP_TRACE_CONTENT` as one shared, default-False Boolean controlling permitted trace payload content for both `TRACE_MODE=OTEL` and `TRACE_MODE=LangFuse`. Keep it independent of LOG_CONTENT, including for LangFuse callbacks/masking. Use normal configuration precedence, strict Boolean parsing, value-source provenance and non-hidden configuration review. Only valid, explicitly supplied True permits content; omitted/defaulted/unresolved/invalid configuration cannot enable capture.
- [ ] Keep LOG_CONTENT authoritative for application/SDK diagnostic logs, local console/JSON/SSE diagnostics, Allure attachments/screenshots and recorded workflow content. Trace opt-in must not enable those channels, and log opt-in must not enable trace payloads. These are content controls, not switches for executing the agent or selecting a backend: safe structural spans, timing, token counts and outcomes remain available without payloads; disabled TRACE_MODE stays inert.
- [x] Make both configuration descriptions state their destinations and independence explicitly. LOG_CONTENT help must name logs, local diagnostics and Allure details/screenshots, and direct users to TRACELOOP_TRACE_CONTENT for trace payloads. TRACELOOP_TRACE_CONTENT help must name both OTEL and LangFuse spans and state that it does not enable log or Allure content. Both descriptions must state default False and unconditional credential redaction; configuration review must display this help.
- [ ] Apply the same trace policy to manual spans, automatic instrumentation, provider/tool events, LangFuse observations and background flush. Project the resolved application setting into supported SDK controls before capture/queuing; SDK defaults, another environment switch, decorators or per-context overrides cannot bypass it. Qualify process-global instrumentors before mixing runtimes with different policies; never toggle shared environment state under an active peer.
- [x] Preserve credential, authentication/continuation-state and hidden-reasoning exclusions under every flag combination. Approved user-visible prompts/completions and tool payloads may be confidential and require the destination's explicit opt-in. Metric labels/resource metadata remain structural and allowlisted regardless of trace-content opt-in. OTLP log records remain log content even though their transport is OTEL.
- [x] Admit model trace labels only as exact identities established by a trusted provider catalog or explicit owner-controlled binding; syntax alone cannot establish trust. Omit unclassified labels and use a fixed chat span name before SDK creation; preserve provider request/result semantics for every valid model ID, including spaces and Unicode. Content opt-in does not relax structural classification or credential scrubbing.

| LOG_CONTENT | TRACELOOP_TRACE_CONTENT | Log/local/Allure content | OTEL and LangFuse trace content |
| --- | --- | --- | --- |
| False | False | Withheld | Withheld |
| True | False | Permitted after scrubbing | Withheld |
| False | True | Withheld | Permitted after scrubbing |
| True | True | Permitted after scrubbing | Permitted after scrubbing |

## Implementation join points

- [x] Add the declaration and update LOG_CONTENT help in `cfg/config.toml` together with exporter wiring. Separate trace-content policy resolution in `src/api_agent/telemetry/tracing.py` from the log/evidence policy currently supplied by `src/api_agent/telemetry/privacy.py`, retaining common credential registration and redaction. An injected boundary must not override the selected destination's resolved policy. Use the application's True/False syntax; supply the SDK's documented Boolean option or encoding from that resolved value. Reconcile existing lowercase SDK environment values in migration configuration instead of silently defaulting them. Update review/help, examples and deployment documentation with implementation; do not advertise the switch as delivered beforehand.
- [x] Reuse workflow identities and observations, but create destination-specific content projections before the first journal, IPC queue, SDK queue or emission boundary. A LOG_CONTENT-gated journal cannot supply content already omitted when trace-only capture is enabled. Conversely, trace-permitted content must not be pooled in a shared journal from which disabled log/Allure/diagnostic consumers can read it. Include the LOG_CONTENT-gated `workflows/journal.py` capture and its execution/stateful producers in this design. Freeze bounded capture/fan-out ownership before implementation; do not add an unredacted common payload store or fetch content back from dashboards.
- [x] Propagate the resolved policy and withholding reason through worker/provider boundaries. Preserve provenance, native Allure/accounting consistency, execution semantics, bounded retention and owned exporter shutdown. Already-withheld historical artifacts remain withheld; another destination's flag cannot recover or reinterpret them.
  - [x] Deliver [remote shared-provider tracing](implementation_plan_k1b2.md), with independent capture lanes and a backend-owned exporter, without publishing trace-only content into log/evidence queues. Remote spans correlate by call/attempt ID, not distributed workflow-parent context.

## Verification and acceptance

- [x] Verify all four flag combinations for both backends, including omitted/defaulted/invalid values, source precedence, SDK overrides, callbacks, nested tool/model work, streaming, errors and shutdown. Inspect serialized spans before queuing/export plus real log/recorded-artifact outputs. Use distinctive synthetic payloads and credentials to prove permitted trace-only content reaches traces while other channels stay withheld, and the reverse for log-only capture. Normalized-only streaming cannot supply discarded original reasoning.
- [x] Use causal mutations for flag conflation, LangFuse retaining the old log gate, injection/SDK override bypass, wrong pre-queue fan-out and credential leakage. Complete scoped privacy/public-boundary checks and independent/external review. No paid inference or production export is required solely for this policy proof.
- [ ] Renew complete joined coverage, installed/package and intended-index/history/publication gates before committing or publishing; scoped evidence does not waive them.
- [ ] Human acceptance of independent controls and equivalent OTEL/LangFuse trace policy. Existing backend/credential approvals do not accept this pending policy change.

[OpenLLMetry's public content-control documentation](https://traceloop.com/docs/openllmetry/privacy/traces) documents the environment flag and selective overrides. The application requires explicit opt-in rather than inheriting a library default.

## Bounded manual implementation

The manual runtime now resolves the trace switch separately from the log policy.
An injected credential boundary shares live secret registrations but cannot
change the resolved trace permission. No global SDK instrumentation or process
environment mutation is installed.
Known internal-reasoning, encrypted and opaque-continuation field aliases and
explicitly internal/redacted carriers are excluded recursively, including JSON
text. Provider-returned plaintext reasoning/summaries and ordinary visible analysis/explanation fields stay permitted by
the destination policy; the boundary does not infer hidden content from unlabelled
prose. Explicit GenAI token attributes require bounded integers and requested-effort
attributes use their finite metadata vocabulary; generic evidence fields retain
their owner-defined meanings, including fractional currency category values.
See [the inference-evidence revision](implementation_plan_f2f3d.md) for omission guidance and safe Allure diagnostics.

`WorkflowObservationFactory` wraps one bounded workflow journal as the identity
issuer. Its synchronous fan-out projects classified values independently before
retention: the journal keeps its existing 512-character log previews; the live
log branch and trace branch permit at most 65,536 characters per declared scalar.
Both branches retain the same event identity. The [shared parsed-flow runtime](implementation_plan_k1b1.md)
now admits bounded JSON values from node schemas or resolved state annotations,
without application-specific capture wrappers. Existing explicit `plain_io_fields`
and `plain_io_contracts` remain restrictive overrides. Unknown runtime objects
are not serialized. In-process shared provider calls also capture actual visible
messages; separate worker policy/identity propagation remains unchecked above.

`test_workflow_observations.py` checks all four policy combinations for both
backend routes against serialized loopback OTLP payloads, shared ancestry,
central-call deduplication, dynamic credentials and immutable log callbacks.
Installed-package checks, causal mutations and external review close the manual
checkpoint. Broader automatic instrumentors, cross-process policy propagation and
streaming qualification remain unchecked above.

## Sampling source continuity

- [x] Independently compare the instrumentation-only execution change against the previously qualified source before carrying observations forward. Twelve deterministic scenarios preserve all 31 requests, routes, retries, format outcomes, exhausted paths and catalog/default behavior. Retain the 20 observations, minimum eight plus four margin and cap12 under 44 strict source bindings. Only the execution source hash changes; unrelated source or reasoning drift still rejects. No fresh reliability observations or paid pilot are claimed.

## Manual checkpoint verification

- [x] Independently verify the manual destination fan-out, serialized collector ancestry/usage, cancellation outcomes and centrally published call identities, including positive content controls and causal rejection mutations. Preserve ordinary user content and fractional cost metadata while excluding explicitly classified internal state.
- [x] Close the full coverage, installed/package and external-review gates for the joined manual runtime.
- [ ] Human acceptance of this bounded checkpoint; automatic instrumentors and remote propagation remain separate.

## Recorded-policy admission repair

- [x] Admit the resolved log policy once before an existing journal retains events. Its explicit log gate may further restrict capture; a common credential boundary does not grant or revoke the destination permission. Preserve both live credential authorities, independently project trace-only content, withhold denied raw values before journal capture, and reject late wrapping without changing historical evidence. Verify actual recorded publication and attachments through both graph engines for denied provenance and explicit opt-in.

## Absent observation adapter

- [x] Recorded execution without an observation factory must still require resolved content-policy authority before permitting content; a raw True Boolean alone is insufficient. Withhold when authority is absent or denied. Verify both graph engines and final attachment bytes, preserving explicit external opt-in, trace-only capture and dynamic credential authorities.

## Capture and attribute consistency

- [x] Reserve model identity attributes across initial payloads, later attributes and events; content opt-in never bypasses trusted identity admission.
- [x] Apply identical schema admission to capture validation and unavailable-payload classification, including immutable nested mappings. Unsupported or oversized supported-state inputs must yield safe unavailable evidence without changing graph execution.

- [x] Scope live-row suppression to its own capture operation. Concurrent capture/capture and capture/observe producers must retain and emit each event exactly once with shared identities under both log flags; no shared flag may suppress another producer.

## Shared parsed-flow capture

- [x] Deliver [shared node and provider content capture](implementation_plan_k1b1.md) for all supported parsed flows, replacing application-specific wrappers. Prior manual-checkpoint evidence does not establish capture coverage for newly selected graphs.
