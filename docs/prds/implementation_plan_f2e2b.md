# Deferred terminal test diagnostics

[Shared response scrubbing](implementation_plan_f2e2.md)

- [x] Unit, integration and E2E noninteractive execution disclose retained successful-test stderr at `warning`; `info` adds stdout. Omit empty captures and deduplicate test IDs, retaining selection order.
- [x] Emit only after the runner has completed every test and finalized its local evidence, before final suite summaries, coverage and cost accounting. Preserve live progress, authentication interaction and existing failure diagnostics without replaying them. Noninteractive DEBUG captures are deferred too; interactive DEBUG keeps its existing tee behavior.
- [x] Reuse finalized protected streams and schema-projected profile JSON. Check complete terminal records against the current credential authority without reapplying legacy whole-record text heuristics. Keep these local fields outside Allure and ordinary events. Output failures remain visible failures.
- [x] Verify verbosity, selection order, deduplication, finalization ordering, typed JSON preservation, late credential registration, failed tests, cancellation and reporting-disabled execution. Complete applicable coverage, public-boundary and external review.
- [x] Human acceptance of deferred terminal diagnostics, including combined `SCRUB_DIAGNOSTICS_LEVEL` and `SUCCESSFUL_TEST_DIAGNOSTICS_LEVEL` behavior.

Verification: 23,615 unit tests passed with exact 100% line and branch coverage. Serial/parallel terminal DEBUG checks, restored causal credential controls, the canonical affected-module rerun, distribution/relocation checks, workflow provenance, public-boundary and external source review passed. A fresh Studio selection run and Allure publication passed; actual terminal diagnostics followed pytest completion and preceded final summary/cost lines. Unit output likewise placed diagnostics before the final summary/coverage. Human acceptance confirmed, including the two diagnostic verbosity settings together.
