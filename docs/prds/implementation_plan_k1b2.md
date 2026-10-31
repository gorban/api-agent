# Remote shared-provider tracing

[Independent content controls](implementation_plan_k1b.md)

## Design and requirements

- [x] Retain explicit trace-setting provenance from launch through immutable test profiles into each shared backend. Missing, invalid, defaulted or conflicting provenance cannot authorize trace content. Disabled tracing remains inert.
- [x] Own one manual trace runtime in the shared provider host, with per-call and physical-attempt spans. Carry an optional canonical logical-call ID in the authenticated setup request, separate from inference semantics, so remote spans join existing accounting and Allure identities.
- [x] Capture trace payloads in a separate, bounded per-call lane. A LOG_CONTENT veto must not veto trace permission; trace permission must not admit log/Allure content. Existing provider-worker and disposable projection IPC carry admitted data; do not bounce trace-only payloads through test event streams or artifact stores.
- [x] Before trace SDK queuing, project against the complete provider/operation/caller credential authority, rechecking its generation at publication. Preserve secret, encrypted/internal-state exclusions, source/attempt attribution, fallback precedence, truncation and unavailable/withheld reasons. Neither content flag nor an exporter grants credential authority.
- [x] Deliver equivalent trace-content policy through OTEL and LangFuse without process-global configuration changes. Close per-call spans on success, provider failure, disconnection and cancellation. Finish trace delivery before publication; retain retryable cleanup ownership when shutdown is unacknowledged and report confirmed delivery loss without reopening disposed resources.
- [x] Preserve original results, usage, retries and accounting. Optional payload projection failures produce bounded loss markers, never replacement inference or secret-bearing exception text. Normalized-only adapters cannot supply discarded original reasoning.

## Verification and acceptance

- [x] Exercise all four content-flag combinations through real setup/provider-worker/projection IPC and serialized loopback collectors for both backends, including final Allure artifacts and log event streams.
- [x] Check absent/defaulted/invalid provenance, trace-disabled behavior, credentials acquired during a call, caller-only secrets, fallback/partial/large payloads, physical retries, failure/cancellation, flush/shutdown and cleanup retry ownership. Verify no duplicate inference/accounting or trace-only content in log/Allure queues.
- [x] Use causal mutations for a LOG_CONTENT veto of trace-only capture, trace content entering the report stream and credential-union bypass. Complete scoped coverage, public-boundary and independent review gates.
- [ ] Human review; complete joined delivery/publication gates before committing or publishing.

Standalone local trace inspection is specified in [Standalone local trace viewer](implementation_plan_k1b3.md).

## Remaining integration lifecycle requirements

- [x] Before integration/E2E inference exports to a configured managed local collector, acquire and verify that collector through the existing shared infrastructure owners. Keep reporting storage and tracing leases independent; remote targets must not start local services. Release the tracing lease only after all shared provider exporters finish, including failure and cancellation.
- [x] Human accepted automatic managed integration collector startup/readiness, shared leases, exporter-before-collector shutdown and retained trace data on October 30, 2026. This approval does not close native backend payload views, full GenAI conformance or the separate delivery-loss summary requirement.
- [ ] Surface confirmed trace-delivery loss in the final suite summary and retained diagnostics, separately from test outcomes and Allure publication. Preserve successful tests when optional export fails and disposal is acknowledged; do not silently discard the warning or reopen disposed resources. Verify reachable, unavailable and rejected collectors plus shared ownership and shutdown ordering.

## Implementation and evidence

The backend owns `telemetry/remote_inference.py`; the two destination collectors
share only live credential authority and identities. Trace rows remain outside
test evidence channels. Disposable projection workers recheck the full union
before SDK queuing. Interrupted active collections export only content-free
ledger identities and an unavailable marker, never concurrently changing rows.
Attempt span/event times use recorded attempt boundaries. Remote calls correlate
by ID, not distributed workflow-parent context.

Provider-only plaintext field admission also crosses buffered projection,
journal validation, live log callbacks and direct workflow traces consistently;
generic workflow-field rules remain unchanged. Encrypted/internal state remains
excluded before schema filtering or clipping.

The focused trace suite passed 72 tests, with exact 100% line/branch coverage
(67 statements, 22 branches) in the new trace-owner module. Process-local
mutations of each destination gate and the credential union triggered the intended
regressions; no mutation remains. The broader unit sweep passed 21,319 cases and
27 subtests, exposing 14 test doubles with obsolete client signatures; all 211
tests in the repaired modules subsequently passed. This incremental evidence is
not a fresh whole-project coverage or installed-artifact qualification. The final
joined affected-module regression run passed 685 tests, including direct live
destinations, fallback precedence and separate buffered/legacy capture lanes.

External PR-style review closed live-destination field filtering and attempt/event
timestamp findings, including serialized OTLP checks. Scoped public-boundary
checks passed; complete intended-index/history/publication checks remain separate.
No paid calls, account changes, commits or publication were needed.

Managed collector lifecycle verification passes the exact three-case integration
launch with Allure publication and no trace-delivery warning. Authenticated
LangFuse readback confirms new inference observations. The final full unit run
passes 21,870 tests with 100% lines and branches. Regression checks cover local
versus remote/disabled planning, readiness ordering and inherited deadlines,
exporter-before-service disposal, failure retention and cleanup retry. Isolated
causal controls detect skipped acquisition, extended deadlines and early release.
Independent and external source review are complete; human acceptance
of this automatic integration lifecycle was granted on October 30, 2026. Final-summary visibility of
confirmed delivery loss is the separate unchecked requirement above.
