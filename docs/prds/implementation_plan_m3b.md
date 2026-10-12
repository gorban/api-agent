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
