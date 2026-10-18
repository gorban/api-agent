# LocalStack session ownership and retained restart

[Shared reporting lifecycle](implementation_plan_h2a2.md) · [Initial routing/reuse](implementation_plan_h2a3.md)

This bounded delivery covers managed LocalStack. Tracing persistence, the Reporting
screen, logs, volume deletion and secondary-history synchronization remain in the
parent requirements.

## Ownership and release

- [x] A workbench joins lazily on its first accepted managed-reporting operation or
  explicit managed S3 viewer access. Idle startup, collection and publication-only
  access allocate no service. Keep a dedicated process-owned session manager for
  each selected reporting binding, separate from each active operation manager.
- [x] Use the canonical engine/endpoint registry and original ownership journal.
  Serialize joins, verified final stops and stopped-instance restart under both
  locks. Keep process boot/birth checks; stale process identities release their
  leases, including PID reuse. Uncertain identity refuses cleanup.
- [x] An operation holds its active-use lease through recording and storage
  publication. Its normal release stops the verified registered container only
  when no live operation or workbench session still leases it. The final consumer
  can be a reuser rather than the original creator. Explicit retaining releases
  remain available to existing owners. Retained cleanup pins the original lease
  IDs; changed registry IDs cannot redirect its authority to a replacement.
- [x] Quit settles the workbench's test/inspection subtree before releasing its
  reporting sessions. Failed cleanup retains retryable ownership. Shutdown receipts
  retain both inspection and reporting duties, in dependency order. Cancellation
  during failed stopping exports that same cleanup duty without losing the
  original interruption.

## Retained objects and recovery

- [x] A registered stopped installation restarts only its pinned immutable container
  ID after checking image/configuration fingerprints, original captured assets,
  controlled bind and durable named volume. Use direct container start, preserving
  the original object and mount source. Reuse/restart does not run a new consumer's
  initialization script or create a replacement snapshot.
- [x] Refuse missing/replaced registrations, changed assets, volumes or image facts,
  unregistered occupied listeners and stopped objects with live leases. Preserve
  existing retry duties after partial startup or stop. Cleanup has an independent
  cancellation budget. Normal stopping never removes containers or volumes.
- [x] Prove final reuser release, two sessions plus an operation, join/stop ordering,
  real controlled process death without exit handlers, PID reuse, uncertain
  identity, canceled cleanup, independent ownership refusals and storage-publication
  lease retention using synthetic engines and disposable journals/processes.

## In-flight viewer ownership

- [x] Own history/selected-preview opening before its first await and recheck the
  navigation generation after reporting preparation. Back/End inspection and
  Quit close admission before draining those operations; storage/provider work
  cannot outlive the reporting lease it needs. Dispose the final late viewer only
  after its opener and managed viewer drains settle.
- [x] Reuse the existing bounded operation-cleanup owner for cancellation and retry.
  Export pending opener/viewer duties even when no viewer exists yet. Cancel each
  opener at most once; a timed observer never abandons its active cleanup task.
  Normal Back permits reopening after successful disposal. Quit/unmount keep
  admission closed, including secondary and publication-only history.
- [x] Apply independent cleanup cancellation to the final provisioning inventory,
  including repeated failure and recovery of a canceled acquisition intent.

## Acceptance and limits

- [ ] Independent review, focused mutation controls and exact applicable unit
  line/branch coverage; installed-resource and public-boundary gates.
- [ ] Human acceptance with two coordinated workbenches and retained data after
  an explicitly approved real service stop/restart.
- [ ] Platform-specific process/engine validation beyond the tested Linux envelope.

A killed final process can leave a running container. The next managed consumer
prunes stale leases and verifies the existing object; no exit handler promises
instant crash shutdown or lossless unflushed writes. The existing macOS process
identity fallback remains subject to its own native acceptance. Native Windows
managed-engine/session support is not claimed.
