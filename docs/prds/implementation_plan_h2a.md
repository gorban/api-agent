# Persistent shared reporting storage and fixed application ports

[Shared resources](implementation_plan_h2.md).

## Fixed listeners

- [ ] Preserve configured UI and Django ports and bookmarked URLs. Report an occupied address clearly and stop the affected startup; never choose a replacement port automatically.
- [ ] Verify the current bind-error path before adding coordination. Introduce an additional launch guard only if it resolves a demonstrated ambiguity, and keep idle launches independent when no conflicting listener is requested.
- [ ] Confirm rejection does not stop another listener, mutate its data or leave newly owned workers running. Help and collection remain inert.

## Shared reporting service

- [ ] Define a canonical reporting-service owner and persistent identity independent of checkout paths. Support repeated launches and multiple developers publishing into the selected service.
- [ ] Use the selected backend endpoint URL alone for publication routing. Do not require an installation-ID/registration handshake or another ownership selection setting. Validate local management evidence separately; successful health checks or matching ports never authorize adopting an unrelated service.
- [ ] Separate service administration from test execution. Publication access cannot administer services. A separate lifecycle owner stops managed containers after the last TUI/session and active operation release, retaining volumes; see [ordered lifecycle and management requirements](implementation_plan_h2a2.md).
- [ ] Coordinate lifecycle leases by canonical shared-service identity across checkouts. Owner stop, upgrade or recreation must refuse active consumers unless disruption is separately authorized; publication clients release their access without removing shared containers or data; the last-session lifecycle owner may stop containers after all active uses settle. Recheck leases at the administrative action, including cancellation and crashed-client recovery.
- [ ] Preserve named volumes and report data across stops and container recreation. Volume removal, data reset and retention changes require separate explicit authorization. Confirmed volume deletion cascades necessary managed container stop/removal; users need no separate prerequisite step.
- [ ] Preserve immutable run UUIDs, per-run object keys, byte-checked idempotent retries and manifest-last publication. Concurrent sources must not overwrite or combine each other's runs. Define stable project attribution independently of checkout basenames and keep test/source compatibility checks.
- [ ] Preserve finalized failed/interrupted runs and exclude incomplete uploads from history. Keep viewer selection pinned to the requested run and never substitute a concurrently completed run.

## Migration and acceptance

- [ ] Inventory existing owned storage and its persistence format without starting replacement services. Document compatibility, version pins, credentials/access scope and administrator responsibilities.
- [ ] Back up existing history before a requested move. Verify manifest identities, artifact digests and restored bytes; retain recoverable backups and original run identities. Do not delete the former storage automatically.
- [ ] Independently review ownership, concurrent publication, interrupted upload, retained volumes and migration failures using synthetic data.
- [ ] Human-review fixed bookmarked URLs, clear conflict behavior and durable shared history after an explicitly approved service restart/recreation.
