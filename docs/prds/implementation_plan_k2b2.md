# Visible local startup and log navigation

[Local startup and shutdown](implementation_plan_k2b.md)

- [x] Paint the Run local screen before starting its controller. Show the current safe startup activity and elapsed time regardless of the selected log source. Refresh elapsed status without rebuilding the retained log buffer each second.
- [x] Publish fixed configuration, view, application, infrastructure, dependency, HTTPS, runtime, listener, tracing and browser checkpoints before waiting on them. Preserve the shared startup deadline; checkpoint labels must contain no configuration values or credentials.
- [x] Keep read-only view preparation and infrastructure planning responsive to Stop. Drain owned worker work before cancellation completes; preserve creating-loop affinity for configuration and application factories. Pending work retains cleanup ownership rather than permitting late resource allocation after reported closure.
- [x] Selecting All logs during startup must only change the log selection. Show startup failures and their sanitized reason without needing a keypress; distinguish settled failure from ordinary stopped state and from active cleanup.
- [x] Close causal regressions, exact applicable unit coverage, independent review, installed/package and public-boundary verification, then external review before delivery.
- [ ] Human retest: observe startup activity, open All logs while startup is pending, and confirm it neither stops the operation nor hides a failure. Earlier cleanup acceptance remains recorded separately.

The reported initiating failure is not yet identified. Controlled reproductions
show that log selection can reveal a failure already published by startup, and
that ordinary All logs selection does not request a stop. Synchronous user
configuration/application factories still execute on their creating loop; this
change does not promise that arbitrary blocking user code is interruptible.

## Collector cleanup follow-through

- [x] A completed failed or cancelled log collector must not block later browser, tracing or operation cleanup. An active collector and its worker must still settle before downstream cleanup; retain a usable pending receipt until acknowledgement. Prove both behaviors with causal regressions.

**Deferred human review**

Implementation, causal regressions, exact unit coverage, package/installed checks,
public-boundary checks and independent/external reviews are complete. The human
retest above remains unchecked; earlier cleanup acceptance stays recorded.
