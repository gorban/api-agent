# Responsive Stop and acknowledged tracing disposal

[Local startup and shutdown](implementation_plan_k2b.md) · [Tracing lifecycle](implementation_plan_k3.md)

## Approved behavior

- [x] Run interactive Stop outside the TUI input handler so Log sources, All logs, copying and Quit remain usable during genuine cleanup-pending state. Share retained cleanup work across overlapping actions; preserve cancellation receipts, explicit retry, modal decisions and stale-screen navigation safety.
- [x] Distinguish unsuccessful trace delivery from resource disposal. Retain a sticky failed delivery result and one fixed, credential-free warning. Once the SDK worker and connections have acknowledged shutdown, release the tracing owner and dependent leases rather than retrying export forever. Do not replay export automatically.
- [x] Keep genuinely incomplete or failed SDK shutdown retryable and retain its live worker/owner. Never acknowledge disposal merely because a callback exited, raised an exception or aborted with BaseException. Repeated close and caller cancellation must not duplicate export or shutdown work.
- [x] Apply this distinction to the shared API runtime and ASGI lifespan, with a safe error log for non-TUI consumers. Local agent logs also retain the delivery warning. Preserve a stable cleanup-pending indication instead of alternating stopping/pending or repeatedly emitting identical status snapshots.
- [x] Preserve explicit collector targets. Managed Jaeger requires its conventional local OTLP traces endpoint; a saved viewer/UI endpoint is not silently rewritten when TRACE_MODE changes. Empty TRACE_VIEWER_URL derives Jaeger only for the managed local profile.
- [x] Prove real mounted TUI input/modal/retry behavior, failed-export acknowledged disposal through runtime/operation/local owners, true disposal failure and concurrency/cancellation. Exercise native ASGI server SIGTERM with a failing collector; close exact unit coverage, package, independent and external review before delivery.
- [ ] Human retest managed Jaeger startup/destination, responsive logs during Stop and completed Stop with a visible delivery warning when applicable. Earlier approvals remain separate.

The image-level SIGTERM test remains owned by [deployment shutdown qualification](implementation_plan_h4b.md). A native ASGI process check is not a Kubernetes deployment claim.

**Deferred human review**

Implementation, causal regressions, one renewed full unit/coverage gate,
package/installed checks and independent/external reviews are complete. The
human retest above and separate image-level qualification remain unchecked.
