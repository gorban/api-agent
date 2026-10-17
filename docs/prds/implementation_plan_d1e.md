# Results viewer availability and lifetime

[Allure viewers](implementation_plan_d1.md) · [Selected attempts](implementation_plan_d1a.md)

- [x] Refresh contextual actions when a test reaches a terminal state. A single completed integration/E2E test exposes `a View in Allure` while others run, regardless of whether its frozen snapshot arrives before or after its outcome. Opening one preview never regenerates it automatically as other tests finish.
- [x] Remove the results-only `e End inspection` shortcut. Keep owned Allure and DEBUG resources until leaving results, returning to selection or quitting; preserve cleanup failure/retry duties. Details close returns to results without disposal.
- [x] Confirm leaving results once, naming browser inspection, pending recording cleanup, Allure, or their combination concisely according to owned resources. Name the same duties during active execution and update the existing warning as completion or cleanup changes them, preserving its typed response and focus. Capture the present cleanup duties once so a concurrently closing browser cannot invalidate the warning. Cleanup remains shared and does not stop tests accidentally.
- [x] Completion with a confirmation open updates the owning results screen without burying the pending choice or mounting duplicate screens. Cancel reveals completed results; confirm disposes the same owners. Preserve notification identity and leave no orphan dialogs. If execution already completed before confirmation, preserve its operation status and do not signal test cancellation.
- [x] Preserve an unexpired Allure notification and its exact links when live results become completed results; do not reset its expiry or rebuild the open report. Allow generation begun before completion to finish normally, including while a confirmation is open. Deliver its URL without dismissing the choice; history readiness/errors update the owning history page beneath a modal.
- [x] Separate the report URL and Browser CDP label with a blank line on the history page; keep notifications compact.
- [x] Verify live outcome/snapshot ordering, footer visibility, final transition, URL interaction, cancellation and cleanup retry with controlled resources; require exact applicable coverage, installed wheel and independent boundary/review checks.
- [x] Verify immediate same-port reopening after confirmed viewer disposal and real HTTP traffic, without waiting for TCP connection expiry. Preserve an active foreign or managed listener; closing a rejected contender must not affect the owner. Keep Windows exclusive binding semantics.
- [ ] Human review the live shortcut, final notification and combined leave warning. No additional paid inference is required.

Incremental native-history preparation is a separate [late optimization task](implementation_plan_d1f.md).
