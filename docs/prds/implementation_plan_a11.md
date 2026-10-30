# Pre-start selection cost visibility and group rollups

[TUI requirements](implementation_plan_a.md) · [Checklist](implementation_plan.md) · [Existing forecasts and accounting](implementation_plan_f3.md)

Keep `Est. $` readable before starting integration or E2E tests, including individual tests and every selection grouping. Reuse the existing forecast source and the shared table's right-edge visibility behavior already used for `Execution time`.

## Decisions before implementation

- [ ] Inspect current column layout, forecast availability and grouping/selection semantics. Resolve any blocking choice with the user before dependent implementation, including selected-members versus full-group estimate scope, partial/unavailable rollup presentation and the fallback for extremely narrow terminals. Preserve already agreed semantics; record any new decision here.

## Requirements

- [ ] On both integration and E2E pre-start selection screens, keep the `Est. $` header and its formatted estimate visible when long test identities or group labels would otherwise push the column offscreen. Reserve enough space for the cost display and clip the preceding label area using the existing fixed/preview column pattern. Preserve full identities for navigation, details and copying.
- [ ] Show estimates on individual and parametrized test rows, test-file groups, tag/category groups, `All tests`, and every other supported selection grouping, including contributed or nested groupings where present. Derive membership through the shared grouping/selection machinery rather than a separate hardcoded list of group kinds.
- [ ] Roll up eligible per-test estimates using the agreed scope. Count each unique test once within a group and once in the overall selected total; overlapping file/tag/category groups must not duplicate costs. Do not compute `All tests` by summing overlapping group-row totals. Keep repository-qualified and parametrized identities distinct.
- [ ] Preserve known zero versus unavailable estimates. Do not display a partially known aggregate as a complete zero or fully qualified total; apply the agreed partial/unavailable presentation consistently at every level. Sum underlying numeric values before display rounding and reuse existing currency/precision and forecast-compatibility rules.
- [ ] Refresh affected rows and rollups through existing estimate and selection refresh paths, including group/individual toggles, filters, recollection, return from results and applicable provider/configuration changes. Preserve explicit selection intent and the existing search, range-selection, copy, horizontal-scroll and resize behavior. Keep the accepted `Execution time` visibility behavior working.
- [ ] Populate pre-start estimates from existing forecasts/history without executing tests, starting services, authenticating or making inference calls merely to fill the table. Keep predictions distinct from completed actual costs.

## Acceptance

- [ ] Verify both picker modes at representative widths and after resize with long individual/file/category names. Observe the rendered `Est. $` header and values, not just stored table data; exercise horizontal scrolling and the agreed narrow-width fallback.
- [ ] Use distinct synthetic estimates to verify file/category/All/other supported rollups, overlapping groups, partial selection, filters, parametrized identities, zero/missing/partial history, rounding and refreshed predictions. Assert exact totals and absence of double counting under the agreed scope.
- [ ] Verify ordinary selection/start controls and full-identity copying remain correct. Use controlled TUI tests and focused mutations that expose hidden cost columns or incorrect aggregation, applicable exact unit line/branch coverage, installed/public-boundary checks and independent review.
- [ ] Human-review integration and E2E selection screens before Start, including long names and grouped estimates. Keep this checkpoint open until recorded; synthetic data is sufficient and no paid inference is required for this UI review.
