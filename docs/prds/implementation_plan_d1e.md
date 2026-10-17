# Results viewer availability and lifetime

[Allure viewers](implementation_plan_d1.md) · [Selected attempts](implementation_plan_d1a.md)

- [ ] Refresh contextual actions when a test reaches a terminal state. A single completed integration/E2E test exposes `a View in Allure` while others run, regardless of whether its frozen snapshot arrives before or after its outcome. Opening one preview never regenerates it automatically as other tests finish.
- [ ] Remove the results-only `e End inspection` shortcut. Keep owned Allure and DEBUG resources until leaving results, returning to selection or quitting; preserve cleanup failure/retry duties. Details close returns to results without disposal.
- [ ] Confirm leaving results once, naming browser inspection, Allure, or both concisely according to owned resources. Cleanup remains shared and does not stop tests accidentally.
- [ ] Preserve an unexpired Allure notification and its exact links when live results become completed results; do not reset its expiry or rebuild the open report. Allow generation begun before completion to finish normally.
- [ ] Separate the report URL and Browser CDP label with a blank line on the history page; keep notifications compact.
- [ ] Verify live outcome/snapshot ordering, footer visibility, final transition, URL interaction, cancellation and cleanup retry with controlled resources; require exact applicable coverage, installed wheel and independent boundary/review checks.
- [ ] Human review the live shortcut, final notification and combined leave warning. No additional paid inference is required.

Incremental native-history preparation is a separate [late optimization task](implementation_plan_d1f.md).
