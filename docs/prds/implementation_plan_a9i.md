# Deferred unit-suite performance investigation

[Unit execution and coverage](implementation_plan_a9.md)

Schedule alongside final profiling after application and workflow acceptance. The suite remains fully selected with exact coverage until an independently approved change is delivered; this task does not adopt dependency-aware selection or weaken safeguards.

- [ ] Profile long individual privacy-boundary tests and aggregate fixture cost separately from production request latency. Include large captured-secret/control/UTF-8 inputs, excessive nesting, cycle detection and ordinary-key depth bounds. Preserve real response-size/depth boundary witnesses and their credential-exposure regressions.
- [ ] Profile repeated recorded-inference/catalog joins, integration-runner subprocess/setup work, TUI rendering/navigation/search and browser startup. Separate intentional timeout/cleanup-retry waits from unnecessary polling, repeated startup and scheduler overhead. Use bounded semantic barriers or owner-local clocks only when real integration/process proof remains intact.
- [ ] Report serial and parallel wall time, cumulative case durations, collection, coverage and finalization separately, including CPU contention, active/queued worker utilization and slow tail distribution. Benchmark scheduling/chunking alternatives before proposing a change, preserving collection and isolation. Rank improvements by measured whole-suite savings rather than summing parallel durations as wall time.
- [ ] Benchmark representative repeated runs before/after any optimization. Preserve every distinct behavior, meaningful mutation, real process/browser proof, no-retry fixed guards, exact full line/branch coverage and installed/license/public-boundary gates. Independently review changed fixtures and production paths. A measured no-change decision is acceptable.

The reviewed pre-repair 32-worker gate completed 11,393 cases in 322.93 seconds including collection and coverage, with a 30.69-second slowest case. Some recorded-join groups contain many roughly five-second cases; aggregate cost warrants investigation even when no single case approaches the 60-second guard. These are observations, not proven optimization opportunities or predicted savings.

Parallel unit and integration pytest launches use xdist work stealing so idle workers can take eligible queued cases from a slower worker. xdist retains the running case and one lookahead case on that worker, so an idle worker may still leave that lookahead queued until the running case finishes. Serial execution remains available with an effective worker count of zero. Uneven-workload regressions exercise both runners, preserve per-test outcomes and completion events, and verify serial execution does not launch xdist. E2E execution is not yet implemented; preserve consecutive confirmation ordering before applying parallel scheduling there. Whole-suite savings remain unmeasured, so the performance investigation and independent review above remain open.

Approved slices: [response-bound fixture](implementation_plan_a9i1.md) and
[final command diagnostics](implementation_plan_a9i2.md). Further changes require
separate measurements and approval.

## Current acceptance and deferred GUI work

- [x] Human accepts the latest 20,743-test NO_STD_IN run, runtime, exact line/branch coverage and produced report. This accepts the recent delivered unit repairs; interactive selection remains a reported issue.
- [ ] Complete the [visible-window and desktop-free CI audit](implementation_plan_a9i3.md) after the large-picker plan and before final credential-sanitization profiling.

- [x] Human accepts the delivered parallel integration scheduling improvement. This does not establish future E2E execution or remove xdist lookahead behavior.
