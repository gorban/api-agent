# Optional observability availability

[Shared tracing](implementation_plan_k1b2.md) · [Allure reporting](implementation_plan_d.md)

## Required behavior

- [ ] Tracing setup, collector readiness, instrumentation and delivery failures must emit useful credential-scrubbed diagnostics without changing application results, test assertions, inference counts, usage or accounting. Disable an unavailable destination for the operation rather than repeatedly retrying setup on every call.
- [ ] Preserve cancellation and retain genuinely unresolved cleanup owners and retry capabilities. Report pending optional cleanup separately from functional test outcomes; never claim disposal or release a collector still needed by an exporter.
- [ ] Allure preparation, recording and publication have separate reporting outcomes. Reporting failure must not manufacture a failed functional test or change a successful functional exit status. Retain usable local results and expose publication failure in CLI/TUI summaries.
- [ ] Keep mandatory local diagnostics and privacy/evidence integrity separate from optional remote reporting. Reject publication of corrupt artifacts and report local evidence as unavailable or unverified; reported reporting loss does not manufacture a functional test failure. Failure to record a required failure/loss diagnostic may fail execution. Never silently report complete evidence after unrecordable loss, or bypass credential protection to preserve availability.
- [ ] Verify ordinary application and shared integration/E2E paths, including collector/SDK startup, span creation/update/end, shutdown, reporting preparation and upload, concurrent calls, cancellation and unavailable diagnostic sinks. Complete applicable full coverage, resource/boundary checks and external review before human acceptance.

## Current repair

Actionable managed collector mismatch notices and TUI delivery are specified in
[managed collector recovery diagnostics](implementation_plan_k1b4a.md).

- [x] Shared integration collector and SDK-start failures log once and allow the original provider call to continue. Preserve cancellation; failed diagnostic writes remain fatal. Keep existing cleanup ownership until the broader lifecycle change is complete.
- [x] Allure upload failure retains local results and reports its failure separately from functional execution status.
- [ ] Complete the remaining application, instrumentation, cleanup and reporting-preparation paths above. Focused/live proof of one path does not qualify the entire policy.
- [ ] Human acceptance of unavailable tracing and reporting across the completed scope.
