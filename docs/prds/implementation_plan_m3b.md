# Shared DEBUG authentication and inspection

The integration launcher owns one lazy setup backend for the selected provider,
account, and frozen configuration. Every selected worker, including parallel
workers, receives a revocable operation lease. Credentials, authentication
state, provider objects, and browser objects remain in the backend process.

- [x] DEBUG workers share readiness and catalog setup while retaining one
  backend-owned browser inspection service. Independent worker disconnects
  revoke only their leases and active requests.
- [x] Complete pytest reports and Allure publication before changing the
  backend from run-complete to inspection-open. Retained windows expose their
  original loopback inspection endpoints through the existing inspection owner.
  Stop remaining worker calls and capture the final provider quota before
  usage/accounting and Allure publication; retain authentication and browser
  ownership through publication and dispose them afterward.
  Detached readiness, catalog, calls and shielded authentication initialization
  must stop before the accounting snapshot is frozen. Unconfirmed settlement
  blocks publication and preserves its cleanup receipt; late setup cannot emit
  a new starting-quota observation after finalization.
  Settlement also revokes credential reads and confirms actual helper work has
  stopped. A failed barrier may deliver partial test diagnostics, but must not
  invoke final recording, freeze accounting, or publish storage manifests.
  Consumer retry acknowledges both backend disposal and retained cleanup
  receipts before releasing suite exclusion. Preserve original receipts across
  error-link replacement, include later recording duties, and avoid retrying
  the consumer's own group recursively; failed retries remain recoverable.
  Explicit shutdown or closing all windows disposes the backend and emits the
  existing inspection event, with a retry receipt if cleanup is unconfirmed.
  A stopped creating loop is restarted for bounded inspection wait and cleanup;
  timed waits cancel and drain their own waiter. Shutdown drains concurrent
  waiters and the monitor before acknowledging the suite lease, including
  event callback and monitor startup failures. Publication callbacks cannot
  start a second runner operation while the first still owns its backend.
- [x] Parallel DEBUG requires deterministic multiprocess proof of one actual
  backend setup, independent consumers, original-page identity, browser
  survival after worker exit and inspection-client disconnect, and explicit
  final disposal. Controlled local Chromium proves these lifetimes. A stopped
  backend loop is restarted to dispose its original owner. On Linux, killing
  only the owned launcher PID causes the recorded Playwright driver and
  Chromium descendants to exit without process-name cleanup.
- [x] Verify stale operation/worker/attempt leases, failure and cancellation,
  and finalization order. A failed cleanup keeps the original owner until an
  acknowledged retry. A real remote fixture verifies Allure publication before
  backend transition, including independent worker disconnect. Human
  inspection remains an acceptance gate.
- [x] Independent adversarial review and controlled native browser/worker lifetime checks.
- [x] Relay independent backend timing beneath each waiting worker's display scope.
  Preserve remote parent relationships and elapsed clocks; clip a timed child's
  entry allowance to the tightest local ancestor's remaining time, then freeze
  that allowance across updates. Late subscribers retain the component's existing
  elapsed time. Removing a child restores its ancestor's continuing clock and
  original entry allowance. Display translation does not shorten shared backend
  work or let one waiter's cancellation cancel a sibling's authentication.
- [x] Human acceptance of retained original-browser inspection, nested timing and launcher cleanup, recorded October 12, 2026. Timeout retention, original-page attachment, finalized reporting and closure were observed; the display repair has deterministic transport/ancestor controls.

## Deterministic publication verification

- [x] Synchronize publication tests on entry into real pending authentication before expiring a readiness waiter. Connection/registration scheduling must not determine that prerequisite; native timeout/error and client-disposal paths remain real.
- [x] Bound fixture startup separately from expected expiry, preserve early failures and reject unexpected success. Drain helper requests and settle signals on failure/interruption; assert disposal inside the running loop, closed client/backend loops and stopped backend threads.
- [x] Prove startup delayed beyond the old short deadline, missing-entry/error/cancellation paths, and preserved settlement/cleanup-receipt/quota invariants with focused mutations.
- [x] Verify repeated serial and parallel controls plus full exact unit line/branch coverage at both exercised and configured concurrency; retain failed evidence and require independent review.

The fixture uses a real readiness exchange with native timeout armed only after actual pending setup entry. Independent review detects eight targeted mutations, including missing awaited request drainage before loop shutdown; early-error, watchdog, cancellation and interrupted/deferred teardown controls pass. The four original test bodies remain unchanged. Twenty serial original-case repetitions pass (80 checks), as do 288 focused cases at each of sixteen and thirty-two workers. Full units pass 5,976 cases plus 27 subtests at each worker count, with exact 100% line/branch coverage across 134 files. Offline wheel/license and public-boundary/metadata checks pass. Production deadlines, transport and configuration are unchanged.

Fresh independent PR/QA re-execution confirms the fixture repair: all 5,976 tests and 27 subtests pass at sixteen and thirty-two workers with exact line/branch coverage, without retries. Serial/stress, genuine timeout, live-loop drainage, retained-receipt and quota-ordering probes pass. The prior short startup-window caveat is resolved; production deadlines and configuration remain unchanged.

## Separate fixture hang-protection follow-up

- [ ] Implement and independently review [bounded fixture/control waits and independent process supervision](implementation_plan_m3b1.md). The setup-entry scheduling flake remains resolved; this follow-up covers unbounded cancellation drainage and blocked-loop failure modes beyond the earlier cooperative controls. Production deadlines and behavior remain unchanged.
