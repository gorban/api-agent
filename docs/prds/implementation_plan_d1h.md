# Configurable Allure history display

[Launch history](implementation_plan_d1c.md) · [Selected tests](implementation_plan_d1a.md)

- [x] Declare review-visible positive integer ALLURE_HISTORY_DISPLAY_COUNT, default 20, under normal configuration precedence for integration, E2E and Allure modes. Permit acknowledgement to .env. A count of 1 shows the current launch only.
- [x] Include the current launch within that count: select up to count minus one earlier launches for native test History and up to count minus one on either side for archive Trends. The archive still lists every finalized launch; cost-history lookback and retained artifacts are independent.
- [x] Preserve exact selected results, generated links, parameter/retry identities and source isolation. Derive larger windows from native generated entries despite Allure 2's 20-entry export cap; never invent historical result IDs. Bound statistics to the retained observations, keeping current-test and earlier-run History separate from forward Trend context.
- [x] Preserve byte limits, owned staging, shared deadlines, cleanup and immutable routes. Larger settings can cost more generation time and storage; they do not disable operation budgets or authorize live calls or storage changes.
- [x] Validate every generated status-count delta against the admitted items, then reconcile exported and current-case History statistics with their retained observations. Reject conflicting categories even when totals and native overlap match.
- [x] Prepare the archive in linear space using shared per-mode snapshots and index ranges. Offload construction with cooperative deadline/cancellation checks and bound index bytes before publication. A failed/cancelled build preserves the previous published index and route bindings; keep the complete archive and immutable snapshot/count identity.
- [x] Commit index replacement and retrieval bindings under one publication lock. Failed replacement retains previous bindings; HTTP readers wait for the commit point. Drain cancellation during publication. If an interrupted replacement is proven complete from its owned bytes, reconcile the committed bindings before preserving interruption or recovering a completed I/O operation.
- [x] Verify current-only, smaller/default/larger windows, pass-rate statistics, sparse tests, malformed/native truncated data, exact outcomes and history links with genuine CLI/browser proof, source configuration, mutations, installed package, exact coverage and independent review.
- [ ] Human review a larger history window and its per-test History/pass-rate presentation. Existing history needs no migration or deletion.

- [ ] Complete the [archive generation budget repair](implementation_plan_d1h1.md) before the larger-window acceptance retry.

The owning adapter only changes derived report data. Allure 2 remains an external prerequisite; original finalized artifacts and generated current-test outcomes remain authoritative.
