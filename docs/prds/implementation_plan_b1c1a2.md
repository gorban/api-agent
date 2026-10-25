# Complete flow-aligned test evidence

[Recorded execution](implementation_plan_b1c1a.md) · [Evidence contract](implementation_plan_b1c1.md) · [Historical overlays](implementation_plan_b1c2.md)

Integration and E2E tests need equivalent execution data in logs and Allure so a
historical result can be overlaid on its recorded flow.md.html hierarchy. Export
to OTEL or LangFuse from the separate test backend is not an acceptance prerequisite.
Application tracing retains its own requirements.

- [ ] Use one versioned execution model and stable source/graph, component, node, turn, visit, attempt, retry and call identities across log and Allure projections. Preserve actual transitions, outcomes and capture completeness. Do not retrieve missing evidence from a tracing dashboard or infer a complete route from final state.
- [ ] When LOG_CONTENT=True, preserve complete permitted, scrubbed node inputs/outputs and actual visible LLM/tool request/response data in both durable test logs and manifest-hashed Allure artifacts, with equivalent permitted values and occurrence identities. Include deterministic nodes and every supported repeat/turn. Preserve declared structure; exclude credentials, authentication/continuation state and hidden reasoning before IPC, queues or storage. Separate-backend evidence must use its owning credential authority; raw caller-side capture is not a substitute.
- [ ] Keep console presentation independent of durable completeness. Default human console previews may remain short, but durable test logs and Allure must both retain the complete admitted payloads. The existing Run-local-only RUN_LOG_FULL_CONTENT setting does not establish test-log support. A 512-character journal preview cannot stand in for either complete destination. Bound total capture, artifact size and retrieval using explicit budgets or chunked attachments; overflow, unsupported values and drops must identify incomplete fields/ranges and must not claim full evidence.
- [ ] With LOG_CONTENT other than explicit True, retain safe structural identities, timings, token categories and estimated cost/basis while clearly withholding confidential values. TRACELOOP_TRACE_CONTENT neither authorizes nor is required for log/Allure content. Keep the existing independent flags and unconditional redaction.
- [ ] Join per-call usage and frozen cost estimates, provider/model/version/effort attribution, inclusive/exclusive timing and timeout/pass-limit observations to the correct occurrences. Reuse authoritative accounting and retain unknown/partial values; parent summaries must not double count leaf usage. Store execution-time settings and their available resolution provenance rather than reading today's configuration.
- [ ] Bind the artifact to the exact recorded declaration projection, source fingerprints and applicable executed setting values. Historical consumers must use that evidence to overlay the matching definition, never today's mutable flow. Keep native Allure steps consistent with the same identities and accounting; viewer implementation remains separately pending.
- [ ] Verify an actual separate-backend producer-to-IPC-to-published-Allure path using synthetic visible payloads longer than 512 characters, nested deterministic/tool data, multiple turns/retries, failure/cancellation and independent flags. Reopen both the durable test log and finalized Allure artifact and compare their permitted values, identities and accounting with the admitted source data. Use causal controls for preview-only persistence, wrong attribution and privacy bypass, then complete independent/external and human review. No live inference is needed for deterministic qualification.

The delivered shared capture runtime supplies bounded JSON admission and common
node observations. Full durable storage, complete cross-process evidence joins,
execution-setting snapshots and historical overlays remain unchecked here.
