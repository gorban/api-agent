# Final native cleanup and deterministic fault coverage

[Native readiness controls](implementation_plan_a9h7.md)

- [x] Investigate native SDK descendant cleanup without extending inference or cleanup deadlines. Distinguish live descendants, terminal zombie-only groups and unavailable process evidence. Keep disposal pending when live or unverifiable; preserve positive readiness/liveness and final disposal assertions.
- [x] Retain child identity through existing non-reaping observation and owned-group disposal contracts. Async pipes must not admit an automatic child watcher that reaps before cleanup. Never signal a numeric group after ownership is lost; successful worker exit alone is not a disposal acknowledgment.
- [x] Forward only explicitly owned authority descriptors. Preserve byte framing, credential authority and cleanup leases through launch, cancellation and pipe-attachment failure. Failed disposal must retain a reachable owner and retry receipt, stay registered and block new admissions; ordinary successful cleanup must allow independent concurrent requests.
- [x] Settle executor work even if all ordinary tasks are cancelled. Require pipe-transport closure acknowledgments before disposal succeeds, and shield shared acknowledgments from cancelled close waiters. Sign-in reader teardown and authority-channel close failures must always reach worker disposal. Attempt every owned channel close once without retrying an ambiguous descriptor close; preserve classified primary errors and safe cleanup diagnostics. Under repeated cancellation, retained receipts must belong to the cancellation actually propagated.
- [x] Recheck the original operation budget after successful disposal. Cleanup may settle beyond expiry to preserve ownership, but must not release late terminal success.
- [x] Deterministically exercise recovery EIO-as-EOF handling and non-EIO terminal read rejection on every applicable platform, independently of real PTY exit ordering. Verify private credential delivery, safe errors and actual owned cleanup; reject removal of each intended guard.
- [x] Exercise bounded-schema worker write failures with synchronized doubles that prove the write attempt, unavailable verdict and owned cleanup without scheduling or pipe races. Retain production startup and execution budgets.
- [x] Require independent adversarial and external review, exact full applicable unit line/branch coverage and public-boundary/index/metadata checks before publication. Keep the selected worker policy unchanged.
- [x] Native normal-launcher verification passes after the linked follow-up: 26,887 passed, 16 platform-incompatible tests not applicable, and exact line/branch coverage reported at 16 workers. This verifies the repaired suite without proving the precise original filesystem cause.

Verification: 26,887 Linux unit tests pass at 16 workers with exact 100% line and branch coverage. Independent adversarial and external source reviews pass, including actual-worker failure probes and targeted mutations. Workflow and staged public-boundary checks pass; committed metadata must pass before publication. Subsequent native verification is recorded in the linked follow-up.

Follow-up: [concurrent lock creation and isolated clipboard controls](implementation_plan_a9h9.md).
