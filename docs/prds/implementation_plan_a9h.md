# Bounded unit execution and launcher parity

[Unit execution and coverage](implementation_plan_a9.md)

- [x] Identify registered workflow functions by the actual source file, accepting legitimate filesystem aliases for the same file while rejecting different, missing or inaccessible sources. Keep declaration confinement and source-binding authority; do not bypass validation for tests or DEBUG.
- [x] Make the synchronous-node timeout fixture fail promptly when compilation or dispatch fails. Bound startup coordination, settle every observer/request task on success, failure and interruption, and preserve the context propagation, event-loop responsiveness, timeout and offloaded-work drainage assertions. Prove early compilation failure cannot cause an infinite observer wait.
- [x] Qualify the late-prior-reader diagnostic isolation fixture with component progress both enabled and disabled. Isolate its fake thread factory from the shared standard-library module so heartbeat threads do not masquerade as stdout readers. Preserve exact result/status, ownership and diagnostic assertions; a targeted global-patch mutation must reject the active-progress case.
- [x] Add bounded unit-run hang protection with clear active-test diagnostics and owned process-group cleanup. Use measured slow-test evidence to select limits; preserve completed results and exact coverage failure semantics. A timeout is a failure, never a skip, automatic retry or manufactured successful coverage. Freeze the precise deadline and cancellation contract before implementation.
- [x] Reproduce launcher conditions, including normal configuration/progress and filesystem aliases, alongside direct pytest. DEBUG must not retain unit browsers or change unit assertions. Preserve the full applicable coverage gate, installed package, license and public boundary checks; obtain independent and external review.

Slow-test analysis must distinguish cumulative test time, parallel wall time and final coverage generation. Keep slow cases visible for optimization; do not weaken privacy or process-lifecycle controls merely to shorten the suite.

## Approved watchdog contract

- [x] Enforce a fixed 60-second deadline for each started test across setup, call and teardown. Heartbeats and intermediate phases cannot extend it. Concurrent tests have independent deadlines clipped by any enclosing caller deadline; they are not nested siblings. Preserve the shared timeout/progress interface for owned execution.
- [x] Enforce a separate 60-second budget whenever the execution controller has no active tests: initial pytest startup after process creation, inter-test gaps and final coverage/shutdown. Do not reset it for arbitrary stdout or progress heartbeats. Collection retains its existing separate bounded contract.
- [x] Read structured reports before bounded raw-output batches and recheck durable completions before declaring timeout. Once timeout is decided, late reports cannot erase it. Abort and acknowledge disposal of only the owned repository process group, retaining existing cleanup receipts; never signal a disposed numeric process identity again.
- [x] Preserve completed results and explicit earlier phase failures. A passed call without required teardown is incomplete, not passed. Report timeout/unfinished identities as errors with bounded elapsed/limit diagnostics; return failure rather than cancellation, skip or retry. Genuine user cancellation keeps its distinct status.
- [x] Prove individual/parallel deadlines, completed-before-expiry races, incomplete phases, silent/raw-noisy startup and finalization, cleanup/interruption, and a controlled real process with descendants. Independent mutations must reject omitted enforcement, timer resets, late-success overwrite and lost cleanup. Keep full coverage and launcher parity checks.

## Retained sampling evidence

- [x] Carry forward only the existing exact workflow profile after independent old/new graph, full request, output and structural-event equivalence establishes that source-file admission is the only change. Preserve all observations, statistics, operating bound and previous provenance receipts. Add the original fixture digest and old/new source digests; do not claim a fresh pilot. The unchanged validator must reject prior/unrelated source hashes and future sampling changes. No live inference is authorized by this source-identity repair.

## Human acceptance

- [x] Confirm a normal full unit launch finishes with coverage and the bounded-failure diagnostic is understandable. The user also accepts integration with and without DEBUG and NO_STD_IN.

## Scheduler-failure fixture parity

- [x] Qualify allocated chat-operation recovery tests with component progress both enabled and disabled. Inject failure at the intended owned scheduler boundary and wait for a bounded semantic signal rather than assuming a global task ordinal or one event-loop turn. Preserve assertions for failed allocation, retained cleanup, unfinished completion and successful recovery; meaningful mutations must reject loss of the intended behavior.

- [x] Qualify HTTP callback-scheduler failure tests with active and inactive progress. Reject the intended cleanup-wrapper submission, preserve closure of its unsubmitted coroutine and retained callback retry, and prove an unrelated heartbeat failure cannot satisfy the test. Keep shared asynchronous scheduling intact outside the owned module.

## Deadline transition and successful-exit reconciliation

- [x] Before a structured event replaces an idle clock or completes an active test, compare its recorded event time with the preceding fixed deadline, including any enclosing caller limit. Latch an overdue violation so later events cannot erase it. A durable completion delivered late may win only if it was recorded before expiry. Preserve already timely-completed results and genuine cancellation; do not publish a false passed result for the violating test.
- [x] Reconcile the no-active finalization deadline when observing process exit, including successful exit before the next loop poll. Exit code zero must not bypass an expired startup/finalization budget. Add controlled late-start, late-teardown, already-exited child, enclosing-budget and delayed-successful-exit regressions; require consumer-level mutations and real owned-process proofs.

## Verification

The joined source passes 11,393 unit tests at 32 workers through the production runner with active progress, with exact 100% line and branch coverage across 194 measured files (27,030 statements and 10,162 branches). Collection, execution and JSON/HTML coverage took 322.93 seconds; the slowest test took 30.69 seconds. Actual launcher replay, installed-package loading, licenses and public-boundary checks pass. Independent adversarial and external reviews close the deadline and fixture repairs, including real owned-process, interruption, delayed-event and successful-exit controls. Earlier failed attempts remain diagnosed; the source-identity receipt preserves existing qualification observations without claiming a new pilot.

## Recorded human acceptance

The user confirms another full unit launch passed at exact 100% coverage and accepts integration behavior with and without DEBUG and NO_STD_IN. The separately reported multithreaded-fork warnings are tracked in [process fixture startup](implementation_plan_a9h1.md); the accepted hang repair remains complete.
