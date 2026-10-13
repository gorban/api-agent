# Publication fixture hang protection

[DEBUG publication settlement](implementation_plan_m3b.md).

## Scope

The setup-entry scheduling flake is resolved. This separate test-harness task protects against cancellation-resistant requests, absent handshake signals and blocked event loops. It does not change production deadlines, transport behavior or the already verified ordering requirement.

A startup watchdog does not bound the subsequent cancel-and-await cleanup. Cooperative async timeouts cannot interrupt synchronous code that blocks the event loop. Do not claim complete hang protection from successful scheduling/stress checks alone.

## Requirements

- [ ] Bound every fixture/control handshake and cancellation/drain wait, including waits before cancellation is issued. Keep genuine readiness timeout attribution and startup-entry ordering intact; safety budgets must not manufacture the expected result.
- [ ] Preserve task/resource ownership when drainage exceeds its budget. Fail with a specific fixture diagnostic and retained cleanup evidence; never equate cancellation requested with disposal confirmed or abandon work as successful cleanup.
- [ ] Provide an independent process-level watchdog for the publication fixture/control execution path. It must remain effective when the child event loop is blocked or cancellation is suppressed. Scope supervision and termination to owned disposable test processes/resources; report an explicit failed/timed-out test and retain diagnostics.
- [ ] Make isolated controls cover a request that ignores cancellation, a missing request-start signal, a blocked event loop and slow but bounded successful cleanup. Prove both prompt failure and actual owned-process termination, without affecting unrelated processes or real services.
- [ ] Verify ordinary fixture requests still drain before loop shutdown and shared startup signals survive waiter cancellation. Preserve the settlement, retained-receipt and final-quota assertions; an outer safety guard must not turn premature success or unrelated failures into expected timeout passes.
- [ ] Use focused mutations removing each wait bound, disabling independent supervision and falsely acknowledging cleanup. The intended assertions must fail; restore all changes before acceptance.
- [ ] Run focused repetitions/concurrency proofs and applicable exact unit line/branch coverage, wheel/license and public-boundary gates. Preserve failed attempts; do not accept lower concurrency or retry-to-green as a repair.
- [ ] Obtain independent adversarial review, then human acceptance of the bounded-failure and owned-process-disposal evidence. No new manual TUI workflow, live login, paid inference or real history modification is required.
