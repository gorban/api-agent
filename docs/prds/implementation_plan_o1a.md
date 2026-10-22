# Approved infrastructure contribution v1

[Conditional prerequisites](implementation_plan_o1.md)

The bounded contract below was approved October 22, 2026. Its bounded technical implementation and conformance are complete; human acceptance remains separate below.

## Discovery and authority

- [x] Use versioned declarative files from the selected pack and applicable selected providers. Resolve every referenced file against its declared owning root, capture finite regular-file assets and package them for relocated use. Provider discovery uses declaration metadata without creating executable adapters. Reject unknown versions, unsupported fields, ambiguous identities, path escapes and symlinks before startup.
- [x] Evaluate activation from the operation's freshly resolved typed configuration. Omitted/defaulted, explicit blank and nonempty remote values preserve their existing owner-specific meanings. An endpoint alone cannot invent a local service. Disabled/unselected services, help, collection and deterministic units start nothing and require no unrelated settings or credentials.

## Service and engine scope

- [x] Version 1 supports backend prerequisites through a narrow Compose subset. Host UI remains under its existing UI-provider contract. Generic host-process prerequisites require a [separately specified extension](implementation_plan_o2.md); unsupported declarations reject before side effects.
- [x] Resolve one explicit Docker-compatible CLI/Compose command and Unix engine transport for validation, startup, inventory, readiness, logs and cleanup. Include conformance with the existing host-engine compatibility path; do not silently change engines between phases or claim support for every engine/frontend.
- [x] Use owner-qualified service identities and an explicit dependency DAG. Validate missing/cyclic dependencies, collisions and incompatible activation before startup. Close dependencies across profiles explicitly rather than assuming profile selection enables them. Capture configuration and all admitted assets before engine execution; avoid ambient dotenv/configuration inputs.
- [x] Preserve the existing ownership labels, immutable container identity checks, compatibility fingerprints, cross-process leases, rollback, retained snapshots/volumes and retryable cleanup duties. Reuse requires matching owner/configuration/assets/engine evidence. An occupied port or matching container name alone proves nothing. Startup and cleanup never reset persistent data implicitly.

## Readiness and delivery

- [x] Publish separately typed host and in-project endpoints only after bounded health/HTTP identity readiness. Host exposure is loopback; endpoints exclude credentials and unexpected redirects. Container addressing names the selected dependency service, rather than a host-only address. Endpoint outputs are operation data and do not rewrite developer configuration files.
- [x] Allow captured owner-supplied idempotent seed assets with explicit completion checks. Compatible reuse validates completion without rerunning initialization or resetting data. Failed or missing completion is actionable; no success is inferred from a process merely starting. Bound seed/readiness work through the shared timeout/progress interface and existing lifecycle.
- [x] Provide safe component status and bounded labeled logs from the same selected engine/ownership scope. Expose only actually available sources; retain identity and privacy policy. Full Run local menus, browser tabs and UI authentication remain in their existing application milestone.

## Implementation and proof

- [x] Freeze the [concrete v1 format](implementation_plan_o1b.md) and public Python interfaces alongside implementation, with finite capture/readiness budgets and source/installed equivalence. Do not add an arbitrary shell or lifecycle-plugin escape hatch.
- [x] Prove the unchanged core consumes an independently authored external synthetic pack: remote/disabled, local API with external backing, and local API with managed backing. Verify dependency order, endpoint delivery, seeding/reuse, status/logs, occupied ports, rollback, concurrent leases, cancellation and volume preservation with controlled and disposable native proofs.
- [x] Complete exact applicable unit line/branch coverage, causal mutations, installed/relocated resources, notices, public source/index/metadata, independent adversarial and joined external review before delivery. No live inference is needed.
- [ ] Human acceptance of conditional startup, endpoint/status/log visibility, reuse and Stop/Exit cleanup remains separate. Run local and product-specific consumers keep their later gates.

## Delivered requirements

- [x] Discover contributions from the selected pack and applicable providers through normal configuration/activation rules. Resolve relative assets against declared owning roots, independent of cwd. Include definitions, scripts and required assets in installed resources and compatibility fingerprints; do not require the original source checkout.
- [x] Evaluate conditions from freshly resolved typed configuration for the selected operation. Omitted, blank and defaulted values retain their declared semantics. Validate all active settings and incompatible combinations before side effects. Disabled/unselected contributions, deterministic unit execution, help and collection start no services and need no unrelated credentials.
- [x] Build an explicit dependency plan from active contributions. Reject missing/cyclic dependencies, conflicting service identities/ports and unsupported fragments. Verify composed configuration, cross-profile dependency activation, endpoint propagation and host/container addressing. Enabling one profile must not be assumed to enable another's dependencies.
- [x] Support a remote target with no managed API/database, a managed API with an external backing service, and an API with a managed backing service. Keep tracing/reporting independently selectable. Concrete database engines and service-specific connection rules remain in their owners.
- [x] Acquire only enabled dependencies in readiness order. Publish resolved endpoints and labeled status/log streams through the consumer/session APIs. Reuse compatible services through shared ownership labels, leases, inventory and stop-time validation. Integrate with current explicit engine selection and fixed-port conflict rules.
- [x] Handle readiness failures, partial startup, cancellation, repeated cleanup and competing consumers through the existing lifecycle. Release only eligible owned resources, preserve active/reused services and durable volumes, and retain retryable cleanup duties until disposal is confirmed. Startup never resets persistent data implicitly.


The original TUI publication requirement remains in the unchecked startup status/logs and local-run menu joins in [application prerequisites](implementation_plan_h1a.md). This foundation exposes the session data and shared progress interface; it does not complete that TUI wiring.

## Conformance acceptance

- [x] Use an independently authored external synthetic pack declaring an API plus a conditional backing service. Prove the unchanged core consumes both, orders startup/readiness and passes the resolved endpoint to the API. This conformance fixture requires no particular database engine.
- [x] Prove disabled, remote, local/external-backing and local/managed-backing cases; invalid combinations reject before startup. Include missing readiness, occupied ports, compatible reuse, concurrent leases, cancellation, failure rollback and volume preservation. Test dependency/profile validation through controlled commands and disposable native services where applicable.
- [x] Prove source and relocated installed-pack discovery from unrelated directories, complete packaged assets and unsupported-contract rejection. Use meaningful mutations, applicable exact unit line/branch coverage, package/license/public-boundary checks and independent review.
- [ ] Human-review conditional activation, endpoint/status/log visibility and Stop/Exit cleanup with disposable synthetic services. Confirm that help/collection and disabled services cause no startup. Later demo and external-pack services reuse this accepted extension point.
