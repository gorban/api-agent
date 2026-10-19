# Bounded unit execution and launcher parity

[Unit execution and coverage](implementation_plan_a9.md)

- [ ] Identify registered workflow functions by the actual source file, accepting legitimate filesystem aliases for the same file while rejecting different, missing or inaccessible sources. Keep declaration confinement and source-binding authority; do not bypass validation for tests or DEBUG.
- [ ] Make the synchronous-node timeout fixture fail promptly when compilation or dispatch fails. Bound startup coordination, settle every observer/request task on success, failure and interruption, and preserve the context propagation, event-loop responsiveness, timeout and offloaded-work drainage assertions. Prove early compilation failure cannot cause an infinite observer wait.
- [ ] Qualify the late-prior-reader diagnostic isolation fixture with component progress both enabled and disabled. Isolate its fake thread factory from the shared standard-library module so heartbeat threads do not masquerade as stdout readers. Preserve exact result/status, ownership and diagnostic assertions; a targeted global-patch mutation must reject the active-progress case.
- [ ] Add bounded unit-run hang protection with clear active-test diagnostics and owned process-group cleanup. Use measured slow-test evidence to select limits; preserve completed results and exact coverage failure semantics. A timeout is a failure, never a skip, automatic retry or manufactured successful coverage. Freeze the precise deadline and cancellation contract before implementation.
- [ ] Reproduce launcher conditions, including normal configuration/progress and filesystem aliases, alongside direct pytest. DEBUG must not retain unit browsers or change unit assertions. Preserve the full applicable coverage gate, installed package, license and public boundary checks; obtain independent and external review.

Slow-test analysis must distinguish cumulative test time, parallel wall time and final coverage generation. Keep slow cases visible for optimization; do not weaken privacy or process-lifecycle controls merely to shorten the suite.

## Approved watchdog contract

- [ ] Enforce a fixed 60-second deadline for each started test across setup, call and teardown. Heartbeats and intermediate phases cannot extend it. Concurrent tests have independent deadlines clipped by any enclosing caller deadline; they are not nested siblings. Preserve the shared timeout/progress interface for owned execution.
- [ ] Enforce a separate 60-second budget whenever the execution controller has no active tests: initial pytest startup after process creation, inter-test gaps and final coverage/shutdown. Do not reset it for arbitrary stdout or progress heartbeats. Collection retains its existing separate bounded contract.
- [ ] Read structured reports before bounded raw-output batches and recheck durable completions before declaring timeout. Once timeout is decided, late reports cannot erase it. Abort and acknowledge disposal of only the owned repository process group, retaining existing cleanup receipts; never signal a disposed numeric process identity again.
- [ ] Preserve completed results and explicit earlier phase failures. A passed call without required teardown is incomplete, not passed. Report timeout/unfinished identities as errors with bounded elapsed/limit diagnostics; return failure rather than cancellation, skip or retry. Genuine user cancellation keeps its distinct status.
- [ ] Prove individual/parallel deadlines, completed-before-expiry races, incomplete phases, silent/raw-noisy startup and finalization, cleanup/interruption, and a controlled real process with descendants. Independent mutations must reject omitted enforcement, timer resets, late-success overwrite and lost cleanup. Keep full coverage and launcher parity checks.

## Retained sampling evidence

- [ ] Carry forward only the existing exact workflow profile after independent old/new graph, full request, output and structural-event equivalence establishes that source-file admission is the only change. Preserve all observations, statistics, operating bound and previous provenance receipts. Add the original fixture digest and old/new source digests; do not claim a fresh pilot. The unchanged validator must reject prior/unrelated source hashes and future sampling changes. No live inference is authorized by this source-identity repair.

## Human acceptance

- [ ] Confirm a normal full unit launch finishes with coverage and the bounded-failure diagnostic is understandable. Keep noninteractive integration without DEBUG as a separate pending replay.

## Scheduler-failure fixture parity

- [ ] Qualify allocated chat-operation recovery tests with component progress both enabled and disabled. Inject failure at the intended owned scheduler boundary and wait for a bounded semantic signal rather than assuming a global task ordinal or one event-loop turn. Preserve assertions for failed allocation, retained cleanup, unfinished completion and successful recovery; meaningful mutations must reject loss of the intended behavior.

- [ ] Qualify HTTP callback-scheduler failure tests with active and inactive progress. Reject the intended cleanup-wrapper submission, preserve closure of its unsubmitted coroutine and retained callback retry, and prove an unrelated heartbeat failure cannot satisfy the test. Keep shared asynchronous scheduling intact outside the owned module.
