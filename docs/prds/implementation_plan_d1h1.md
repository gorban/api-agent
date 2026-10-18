# Archive history generation budgets

[History display](implementation_plan_d1h.md)

- [x] Give archived-run generation one two-minute budget per batch of up to 20 actual reports, including current, prior History and subsequent Trend context. Derive the budget from the admitted window, not the configured maximum. The HTTP waiter and generation scope must agree; preserve enclosing deadlines.
- [x] Keep each CLI generation command at two minutes, version checks at 15 seconds, archive listing/count requests at their original budgets and cleanup at 15 seconds. A stalled command must still stop; increasing history must not extend unrelated operations.
- [x] Apply the admitted-report generation budget to both archive HTTP requests and direct history viewing. Direct history discovery/index preparation retains its separate two-minute budget; generation begins with the admitted window and remains clipped by any enclosing deadline.
- [x] Prove larger-window success beyond the former aggregate deadline, correct HTTP waiting, boundary sizes, stalled work cancellation/drainage and unchanged command limits. Renew applicable exact unit coverage, installed-wheel and public-boundary gates, then independent review.
- [x] Human acceptance of the larger archived-window repair is recorded. This approval does not claim an additional manual replay; no paid inference or history migration is required.
