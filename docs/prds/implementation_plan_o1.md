# Conditional pack-declared infrastructure prerequisites

[Follow-through](implementation_plan_o.md) · [Existing infrastructure requirements](implementation_plan_h1a.md) · [Lifecycle foundation](implementation_plan_h1.md)

Resource packs can require additional local services without adding service-specific branches to core. Extend the existing Compose/process planner and lifecycle manager; keep concrete service definitions and conditions in the selected pack/provider.

## Contract decisions before implementation

- [x] Review the existing implementation and agree the [bounded v1 contract](implementation_plan_o1a.md) before dependent implementation, including discovery, activation, dependencies, endpoints, readiness/seeding, ownership and the supported engine boundary. The [concrete declaration format](implementation_plan_o1b.md) remains subject to implementation proof and independent review.
- [x] Agree the host-process boundary: v1 is Compose-only; generic host processes require a separately specified extension. Existing UI process ownership remains separate. Reject unsupported contributions before startup.

## Requirements

- [ ] Discover contributions from the selected pack and applicable providers through normal configuration/activation rules. Resolve relative assets against declared owning roots, independent of cwd. Include definitions, scripts and required assets in installed resources and compatibility fingerprints; do not require the original source checkout.
- [ ] Evaluate conditions from freshly resolved typed configuration for the selected operation. Omitted, blank and defaulted values retain their declared semantics. Validate all active settings and incompatible combinations before side effects. Disabled/unselected contributions, deterministic unit execution, help and collection start no services and need no unrelated credentials.
- [ ] Build an explicit dependency plan from active contributions. Reject missing/cyclic dependencies, conflicting service identities/ports and unsupported fragments. Verify composed configuration, cross-profile dependency activation, endpoint propagation and host/container addressing. Enabling one profile must not be assumed to enable another's dependencies.
- [ ] Support a remote target with no managed API/database, a managed API with an external backing service, and an API with a managed backing service. Keep tracing/reporting independently selectable. Concrete database engines and service-specific connection rules remain in their owners.
- [ ] Acquire only enabled dependencies in readiness order. Publish resolved endpoints and labeled status/log streams to the consumer/TUI. Reuse compatible services through shared ownership labels, leases, inventory and stop-time validation. Integrate with current explicit engine selection and fixed-port conflict rules.
- [ ] Handle readiness failures, partial startup, cancellation, repeated cleanup and competing consumers through the existing lifecycle. Release only eligible owned resources, preserve active/reused services and durable volumes, and retain retryable cleanup duties until disposal is confirmed. Startup never resets persistent data implicitly.

## Acceptance

- [ ] Use an independently authored external synthetic pack declaring an API plus a conditional backing service. Prove the unchanged core consumes both, orders startup/readiness and passes the resolved endpoint to the API. This conformance fixture requires no particular database engine.
- [ ] Prove disabled, remote, local/external-backing and local/managed-backing cases; invalid combinations reject before startup. Include missing readiness, occupied ports, compatible reuse, concurrent leases, cancellation, failure rollback and volume preservation. Test dependency/profile validation through controlled commands and disposable native services where applicable.
- [ ] Prove source and relocated installed-pack discovery from unrelated directories, complete packaged assets and unsupported-contract rejection. Use meaningful mutations, applicable exact unit line/branch coverage, package/license/public-boundary checks and independent review.
- [ ] Human-review conditional activation, endpoint/status/log visibility and Stop/Exit cleanup with disposable synthetic services. Confirm that help/collection and disabled services cause no startup. Later demo and external-pack services reuse this accepted extension point.

## Approved bounded v1

- [x] Agree the [Compose-only contribution boundary](implementation_plan_o1a.md) before dependent implementation. The user approved selected-owner declarations, one Docker-compatible Unix engine, dependency closure, typed endpoints/readiness, idempotent seeding and existing lease/cleanup ownership. Generic host-process prerequisites remain a separate extension. Implementation and acceptance above stay open.
