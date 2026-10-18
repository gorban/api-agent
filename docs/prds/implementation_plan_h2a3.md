# Initial cross-checkout LocalStack reuse

[Shared reporting lifecycle](implementation_plan_h2a2.md) · [Fixed ports](implementation_plan_h2a.md)

Bring this narrow slice forward for test and human-review usability. It does
not implement the complete Reporting UI, persistent tracing stack migration or
last-TUI lifecycle. Complete the separate publication-fixture hang-protection
gate before runtime wiring. Preserve the exclusive suite/accounting guard.

## Routing and administration

- [x] Make ALLURE_S3_USE_LOCALSTACK the managed-local activation decision. True
  selects only its validated configured loopback endpoint. False with an explicit
  S3 endpoint is publication-only, including loopback endpoints: do not query,
  initialize, stop or otherwise administer containers. Publication readiness and
  bucket access still fail honestly through the normal storage client.
- [x] Reuse a compatible new managed LocalStack installation across checkouts
  using one canonical backend/endpoint/engine binding and shared ownership
  directory. Do not change agent/UI ownership or automatically choose a new
  port. Normalize only verified loopback equivalences; reject ambiguous endpoints
  and keep unrelated engines, backends, ports and authorized data scopes apart.
- [x] Use the normal owned lifecycle and exact configuration/image/readiness
  verification. A listening port, healthy service or matching name/label alone
  never grants administration rights. Compatible reuse must have a trusted local
  creation/ownership chain; preserve named-volume identity and source evidence.
- [x] Serialize acquisition and release across checkouts, retain process-birth
  identity checks and pending-cleanup duties, and prevent one operation stopping
  resources leased by another. Registry/lease files require bounded regular-file
  reads and private ownership, with atomic writes and interruption-safe locks.
- [x] Distinguish server compatibility from per-consumer bucket/region selection.
  Different publication namespaces must not silently recreate or reinitialize a
  shared server. Define supported provisioning behavior or explicit incompatibility
  before accepting multi-bucket reuse; never execute a new consumer's mutable
  initialization script inside an unrelated installation.
- [x] Preserve existing checkout-scoped installations and live data. They do not
  automatically participate in the new lease protocol. Detect an occupied legacy
  installation with an actionable explanation and publication-only alternative;
  an actual transition requires checked inactive/settled owners, recoverable
  data/ownership evidence and an approved concrete transition. Never relabel,
  delete or replace it merely to hide the port conflict.

- [x] Detect unregistered listeners on both loopback address families before
  provisioning. Pin managed publication/history clients to the literal IPv4
  listener verified by ownership, including reuse of an already registered
  server. Keep publication-only endpoint routing unchanged. Verify an IPv6-only
  foreign listener and refusal before engine mutation, without relying on hostname
  resolution order; unsupported address families remain portable.

## Verification and limits

- [x] Prove two checkouts converge on the same canonical scope and reuse one
  compatible server. Test join/final-release ordering, canceled acquisition,
  stopped/incompatible/replaced containers, process death/PID reuse, corrupted
  ownership metadata, publication failure and retryable disposal.
- [x] Prove explicit publication-only loopback selection performs zero engine
  commands or initialization. Preserve raw upstream storage failures and ordinary
  remote S3 behavior. Help/collection/picker history reads remain non-starting.
- [x] Independently review isolated guard-removal controls and applicable exact
  unit line/branch coverage, wheel/installed resources, licenses, and public
  source/index/metadata boundaries. Use controlled/disposable services first;
  no paid inference is required to certify storage routing or lifecycle.
- [x] Human-review cross-checkout reuse and fixed-port errors after the exact
  delivered behavior and any existing-installation transition are explained.

Normal test publication continues preserving the managed service after the
operation releases it until the separate session-owned shutdown consumer is
implemented. Do not claim last-TUI stopping from operation leases alone.
A stopped registered container is refused, including a canonical instance stopped
by explicit release(retain=False); this slice never automatically restarts or
replaces it. Session-owned shutdown must first add verified stopped-instance
recovery under the shared registry and original-journal locks.
LangFuse/Jaeger reuse and persistent storage keep their broader lifecycle gates.
Allure's generated report viewer is not a storage service: reuse requires an
exact selected-run binding or an explicitly implemented multi-run viewer, never
silently showing a different run at the same port.

## Delivered slice and remaining gates

The registry keys LocalStack by verified engine generation and loopback endpoint;
bucket and region are compatibility checks within that binding. Different
namespaces refuse before initialization. Agent and UI scopes remain unchanged.
New provisioning records durable intent before engine changes. Pending creation
or cleanup retains the exact manager and retry; a later acquisition cannot replace
it. An interrupted intent requires verified recovery rather than automatic adoption.

Existing installations join only through explicit `enroll_reporting` with trusted
operator-selected physical roots and the original ownership directory. Enrollment
requires a settled journal, exactly one running LocalStack container, pinned image
and configuration, matching captured snapshot bytes and the controlled named volume.
It writes local binding metadata and performs no engine or data mutation. Mixed
service scopes refuse enrollment. Consumers then use ordinary leases in that same
original journal, which existing cleanup owners already consult.

The initial engine envelope is local Unix transport with verified trusted aliases,
socket generation and matching CLI evidence. Native CLI identity must match the
direct response. The no-ID CLI mode instead keys the corroborated Unix transport
and validated stable host/store namespace; it never keys a compatibility response
identifier or mutable inventory. A CLI lacking
an engine ID additionally requires matching nonempty immutable container inventory;
ambiguous empty engines refuse. Remote transports refuse. HTTP headers and bodies
share an absolute deadline even under trickle traffic. Cleanup uses its own bounded
budget while retaining engine and directory identity checks after Stop.

## Verification evidence

The first full run exposed a stale string-based plan double; its repair retains
the dependency assertions. A subsequent 32-worker run passed 8,996 units with
exact measured line/branch coverage. Both attempts remain part of verification.

A real compatibility check then exposed a response identifier changing between
requests. The public [compatibility handler](https://github.com/containers/podman/blob/main/pkg/api/handlers/compat/info.go)
confirms this behavior. The independently authored mode-specific identity policy
keeps native matched IDs and corroborated Unix namespaces separate. Regression
controls rotate response IDs and inventories, prove stable enrollment/lookup,
and preserve transport, namespace and identity refusals. Independent review,
102 focused tests at exact coverage, 11 initial guard mutations and four identity
mutations pass. Three real read-only handshakes yield the same binding.

The failed metadata-only enrollment is preserved. Its single new record was
backed up and removed after exact ownership and unchanged-journal checks; no
container or stored-object mutation was performed. The renewed 32-worker gate passes 9,012 units at exact measured line/branch
coverage. Wheel, installed-resource, license and public-boundary checks pass.
The separately reviewed enrollment succeeds against an existing retained
installation; a distinct consumer resolves the same immutable container and
original journal. Read-only postchecks preserve its start time, mounts, captured
assets, journal generation/bytes, user settings and all 200 stored-object metadata
entries. No service restart, initialization, relabeling or stored-object write
was performed.
Human acceptance remains separate. No paid inference verifies this slice.

Human acceptance of the delivered reporting robustness scope is recorded; future reporting lifecycle administration and platform-specific gates remain separate.
