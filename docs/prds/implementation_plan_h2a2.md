# Shared reporting lifecycle and management

[Runtime ownership](implementation_plan_m.md) · [Identity and publication access](implementation_plan_h2a1.md) · [Infrastructure](implementation_plan_h.md) · [Cleanup](implementation_plan_l.md).

## Approved direction

A managed reporting installation is shared across launches and checkouts. Select each reporting backend with its endpoint URL alone; do not require a user-supplied installation ID, registration record or ownership mode. Compatible launches reuse locally managed services; an occupied port or healthy endpoint alone never establishes local container ownership. Custom/production endpoints remain outside local lifecycle management. Normal shutdown stops containers after the last session and active operation have released them, while retaining named volumes and data. Explicit volume deletion is one confirmed action that handles its container prerequisites automatically.

These are requirements; registering them does not deliver runtime wiring or authorize changes to existing services, volumes or history.

The [initial LocalStack routing/reuse slice](implementation_plan_h2a3.md) is
pulled forward for current test review, after fixture hang protection. It does
not complete last-session shutdown or the management UI.

## 1. Session ownership before runtime wiring

- [ ] Use the selected backend and canonical endpoint for routing. Keep local installation identity/session records internal and independent of checkout paths; do not require a trusted remote registration or UUID match before publishing. Validate locally managed configuration, service versions, enabled profiles and ownership labels before reuse. Reject incompatible or unrelated listeners with clear diagnostics; preserve fixed application ports.
- [ ] Give each TUI a process-owned session lease and each noninteractive operation an active-use lease. Verify process identity across PID reuse; uncertainty refuses destructive cleanup. Keep publication-only access separate from lifecycle administration.
- [ ] Coordinate acquisition, final release and shutdown under one lifecycle protocol. A joining launch cannot race the final owner's stop. One TUI quitting does not stop services still needed by another TUI or active operation.
- [ ] Finish or synchronously cancel the quitting session's operations, settle accounting/publication and flush reporting before releasing its lease. Stop only verified managed containers when no remaining consumer needs them; retain every named volume. Respect dependency order and report incomplete cleanup with recoverable retry duties.
- [ ] Process death releases ownership without requiring an exit handler. A crash may leave containers running; the next launch and management screen discover verified leftovers. Do not claim automatic shutdown after the last process is killed or lossless unflushed writes after a crash.

For tracing, `TRACE_MODE` selects the backend and `TRACE_TARGET` supplies its address. Production collector and LangFuse addresses can be Kubernetes service URLs. Emit application/environment attribution through [endpoint-only tracing requirements](implementation_plan_k1.md); sharing a collector does not require separate targets for each application. Backend authentication remains conditional on its documented protocol.

## 2. Durable reporting storage

- [ ] Preserve S3 history and tracing data across normal stops and container recreation. Inventory versions and storage formats before changes; obtain recoverable backups for migrations and verify restored identities and bytes.
- [ ] Configure Jaeger with a supported persistent storage backend and named volumes, including any supporting service. Pin compatible versions and verify traces survive stop/restart and recreation. Do not rely on its default in-memory storage or a volume without a configured durable backend.
- [ ] Verify the selected LangFuse stack's database/object-store persistence and graceful flush. Start the configured tracing backend; do not implicitly enable both tracing alternatives. Reporting container shutdown never implies data deletion.

## 3. Reporting and container management

Bring the bounded [selected-test Allure viewer](implementation_plan_d1a.md)
forward for result/screenshot review. It reuses managed browser/process ownership
and current storage access; it does not complete tracing or last-session shutdown.

- [ ] Replace the Allure-only menu entry with Reporting: serve Allure and the configured tracing stack, display stable endpoints and join the same managed installation. Keep immutable run selection and publication attribution.
- [ ] Expand container management to inventory managed running/stopped containers and volumes, inspect logs and stop eligible containers. Keep the screen accessible when no containers are running. Reuse shared selectable, scrollable, copyable log pages from Run local instead of duplicating them.
- [ ] Offer Delete volumes with a preview of the affected managed installation, containers and volumes and a `y` confirmation. Cascade graceful stop, removal of eligible containers referencing those volumes, then volume deletion. The user need not perform intermediate steps. Normal Stop retains containers/volumes unless the chosen action requires container removal.
- [ ] Recheck ownership and active-operation/publication leases at the destructive action, serialize against new acquisition and refuse to disrupt another active operation. An idle management session must not block its own authorized cleanup; coordinate idle sessions and invalidate stale bindings before later reuse. Never stop the host execution container or delete unrelated/external Docker resources. Engine-wide pruning is outside this scoped action.
- [ ] Report progress, partial failures and retryable remaining work accurately. Cancellation before confirmation performs no mutation; cancellation after it must settle initiated work without claiming a completed purge.

## 4. Restore missing history from secondary storage

- [ ] Add an explicit synchronization action from `ALLURE_SECONDARY_DIR` to the selected S3 history destination. Inspect finalized manifests and verify artifact hashes, original run UUIDs and destination identity. Reuse immutable conditional writes and manifest-last publication.
- [ ] Upload only missing runs/artifacts. Existing equal bytes are idempotent success; differing bytes or invalid/incomplete manifests are reported without overwrite or combining runs. Handle partial uploads, cancellation, late manifests and retries. Protect active producers throughout synchronization.
- [ ] Show progress and actionable storage errors without credentials. Refresh the viewer/history index after successful synchronization while preserving original evidence. No history reset or automatic deletion of the secondary store is implied.

## Acceptance and dependencies

- [ ] Establish endpoint-routing/session contracts first; then persistent storage, last-session shutdown and runtime reuse; then management/logs/volume cleanup and secondary synchronization.
- [ ] Prove two checkouts, quit/join races, active noninteractive consumers, PID reuse, uncertain liveness, crash leftovers, interrupted shutdown and retry using synthetic services. Test publication-only refusal independently of lifecycle authority.
- [ ] Prove each enabled backend's retention through stop/recreation, cleanup cascade with stopped-container volume attachments, active-use refusal, unrelated-resource preservation and partial deletion recovery. Verify logs/copy/navigation and zero-running-container access.
- [ ] Test duplicate/conflicting/late uploads, malformed manifests, corrupt artifacts, failed storage and cancellation; retain finalized failed runs. Keep unit tests outside Allure history.
- [ ] Complete applicable exact unit line/branch coverage, wheel/license/public-boundary checks and independent adversarial review before code commits. Live persistence, menu/logs, explicit cleanup and restore need separately coordinated human acceptance using disposable data and backups.
