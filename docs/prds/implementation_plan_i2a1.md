# Parallel DEBUG inspection ownership

[Studio requirements](implementation_plan_i2a.md)

- [x] Keep the creating pytest worker, event loop, browser context and original
  page alive after test evidence and controller reporting finish with two workers.
- [x] Publish each retained endpoint explicitly, supervise worker liveness and
  aggregate per-worker acknowledgement before releasing suite exclusion.
- [x] Route explicit end-inspection attempts to each retained owner. Preserve
  cleanup-pending ownership and retries; distinguish unexpected worker loss.
- [x] Preserve serial behavior, cancellation cleanup and ordinary concurrency.
  Unsupported lifecycle integration must fail actionably.
- [x] Prove controlled success, failure, cancellation, worker loss and cleanup
  retry through actual parallel runners without external authentication/inference.

The controller defers teardown only for its own xdist node manager; retained
workers finish their inspection service on the original creating thread after
xdist has delivered their terminal test report. Separate worker command/event
files carry lifecycle facts; browser objects never cross the process boundary.

Controlled headed Chromium checks preserve the original page and JavaScript
marker through finalized reports, CDP attachment/disconnection and explicit
cleanup with jobs 0 and 2, including a failing test. Two-worker cleanup retry,
worker SIGKILL and group cancellation are exercised separately. Worker process
polling catches loss even when descendants keep the execnet channel open.

- [x] Independently review lifecycle seams and aggregate suite exclusion.
- [x] Complete exact applicable coverage and package/public-boundary gates.
- [ ] Human review parallel original-browser retention and explicit cleanup.

Managed included UI hosts opt into parent-stdin EOF liveness. The worker owns
the sole writer; worker death stops its listener and active connections. Normal
disposal closes the writer once and retains direct-child termination/kill retry
ownership. Standalone manual hosts do not opt in. Native process-kill and socket
proofs accompany Python and Node controls, including failed writer-close retry.
