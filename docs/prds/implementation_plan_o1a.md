# Approved infrastructure contribution v1

[Conditional prerequisites](implementation_plan_o1.md)

The bounded contract below was approved October 22, 2026. This records design agreement, not implementation or human acceptance.

## Discovery and authority

- [x] Use versioned declarative files from the selected pack and applicable selected providers. Resolve every referenced file against its declared owning root, capture finite regular-file assets and package them for relocated use. Provider discovery uses declaration metadata without creating executable adapters. Reject unknown versions, unsupported fields, ambiguous identities, path escapes and symlinks before startup.
- [x] Evaluate activation from the operation's freshly resolved typed configuration. Omitted/defaulted, explicit blank and nonempty remote values preserve their existing owner-specific meanings. An endpoint alone cannot invent a local service. Disabled/unselected services, help, collection and deterministic units start nothing and require no unrelated settings or credentials.

## Service and engine scope

- [x] Version 1 supports backend prerequisites through a narrow Compose subset. Host UI remains under its existing UI-provider contract. Generic host-process prerequisites require a separately specified extension; unsupported declarations reject before side effects.
- [x] Resolve one explicit Docker-compatible CLI/Compose command and Unix engine transport for validation, startup, inventory, readiness, logs and cleanup. Include conformance with the existing host-engine compatibility path; do not silently change engines between phases or claim support for every engine/frontend.
- [x] Use owner-qualified service identities and an explicit dependency DAG. Validate missing/cyclic dependencies, collisions and incompatible activation before startup. Close dependencies across profiles explicitly rather than assuming profile selection enables them. Capture configuration and all admitted assets before engine execution; avoid ambient dotenv/configuration inputs.
- [x] Preserve the existing ownership labels, immutable container identity checks, compatibility fingerprints, cross-process leases, rollback, retained snapshots/volumes and retryable cleanup duties. Reuse requires matching owner/configuration/assets/engine evidence. An occupied port or matching container name alone proves nothing. Startup and cleanup never reset persistent data implicitly.

## Readiness and delivery

- [x] Publish separately typed host and in-project endpoints only after bounded health/HTTP identity readiness. Host exposure is loopback; endpoints exclude credentials and unexpected redirects. Container addressing names the selected dependency service, rather than a host-only address. Endpoint outputs are operation data and do not rewrite developer configuration files.
- [x] Allow captured owner-supplied idempotent seed assets with explicit completion checks. Compatible reuse validates completion without rerunning initialization or resetting data. Failed or missing completion is actionable; no success is inferred from a process merely starting. Bound seed/readiness work through the shared timeout/progress interface and existing lifecycle.
- [x] Provide safe component status and bounded labeled logs from the same selected engine/ownership scope. Expose only actually available sources; retain identity and privacy policy. Full Run local menus, browser tabs and UI authentication remain in their existing application milestone.

## Implementation and proof

- [*] Freeze the [concrete v1 format](implementation_plan_o1b.md) and public Python interfaces alongside implementation, with finite capture/readiness budgets and source/installed equivalence. Do not add an arbitrary shell or lifecycle-plugin escape hatch.
- [ ] Prove the unchanged core consumes an independently authored external synthetic pack: remote/disabled, local API with external backing, and local API with managed backing. Verify dependency order, endpoint delivery, seeding/reuse, status/logs, occupied ports, rollback, concurrent leases, cancellation and volume preservation with controlled and disposable native proofs.
- [ ] Complete exact applicable unit line/branch coverage, causal mutations, installed/relocated resources, notices, public source/index/metadata, independent adversarial and joined external review before delivery. No live inference is needed.
- [ ] Human acceptance of conditional startup, endpoint/status/log visibility, reuse and Stop/Exit cleanup remains separate. Run local and product-specific consumers keep their later gates.

## Deferred process extension

- [ ] Specify generic host-process prerequisites separately if needed: executable/argv/cwd ownership, captured assets, identity/readiness, logs, bounded cancellation and confirmed disposal. Until that contract is implemented, reject such contributions rather than treating the UI owner as a universal process runner.
