# Publication fixture hang protection

[DEBUG publication settlement](implementation_plan_m3b.md).

## Scope

The setup-entry scheduling flake is resolved. This separate test-harness task protects against cancellation-resistant requests, absent handshake signals and blocked event loops. It does not change production deadlines, transport behavior or the already verified ordering requirement.

A startup watchdog does not bound the subsequent cancel-and-await cleanup. Cooperative async timeouts cannot interrupt synchronous code that blocks the event loop. Do not claim complete hang protection from successful scheduling/stress checks alone.

## Requirements

- [x] Bound every fixture/control handshake and cancellation/drain wait, including waits before cancellation is issued. Keep genuine readiness timeout attribution and startup-entry ordering intact; safety budgets must not manufacture the expected result.
- [x] Preserve task/resource ownership when drainage exceeds its budget. Fail with a specific fixture diagnostic and retained cleanup evidence; never equate cancellation requested with disposal confirmed or abandon work as successful cleanup.
- [x] Provide an independent process-level watchdog for the publication fixture/control execution path. It must remain effective when the child event loop is blocked or cancellation is suppressed. Scope supervision and termination to owned disposable test processes/resources; report an explicit failed/timed-out test and retain diagnostics.
- [x] Make isolated controls cover a request that ignores cancellation, a missing request-start signal, a blocked event loop and slow but bounded successful cleanup. Prove both prompt failure and actual owned-process termination, without affecting unrelated processes or real services.
- [x] Verify ordinary fixture requests still drain before loop shutdown and shared startup signals survive waiter cancellation. Preserve the settlement, retained-receipt and final-quota assertions; an outer safety guard must not turn premature success or unrelated failures into expected timeout passes.
- [x] Use focused mutations removing each wait bound, disabling independent supervision and falsely acknowledging cleanup. The intended assertions must fail; restore all changes before acceptance.
- [x] Run focused repetitions/concurrency proofs and applicable exact unit line/branch coverage, wheel/license and public-boundary gates. Preserve failed attempts; do not accept lower concurrency or retry-to-green as a repair.
- [x] Complete independent adversarial review of bounded failure, owned-process disposal and coverage preservation.
- [ ] Human acceptance of the hardening evidence. No new manual TUI workflow, live login, paid inference or real history modification is required.

## Test structure

The original publication cases execute in disposable pytest children. A parent
watchdog owns the exact process group and verifies disposal through the shared
process finalizer. Admission has a separate generous budget; the execution clock
starts after pytest admits the case. Adversarial controls arm their short test
budget only after entering the controlled obstruction. Child assertion failures,
missing admission and watchdog expiry remain distinct failures.

The readiness deadline is still the native rescheduled async deadline, armed
only after the real shared setup entry signal. Cleanup has its own bounded wait;
an incomplete drain retains the unfinished tasks and a bounded retry. The
independent parent also bounds synchronous loop blockage and shutdown that
suppresses cancellation. No production timeout changes are involved.

Child coverage tracing starts before application imports, proves a known
production body and branch data were recorded, and merges into an active parent
coverage session with its source, relative-file and context settings. Missing
tracing cannot produce a successful case. Focused controls also verify actual
child disposal without stopping an unrelated sibling.

Independent review and the isolated 32-worker gate pass: 8,910 units, exact
100% measured lines and branches, wheel build and public-boundary checks.
Mutation controls distinguish a watchdog rejection from normal successful cleanup;
the independent mutation supervisor also verifies every nested child is disposed.
The original failed attempts remain in review evidence. Human acceptance remains
separate from the resolved implementation gate.
