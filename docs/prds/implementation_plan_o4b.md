# Tool-instance migration and acceptance

[Requirements](implementation_plan_o4.md) · [Proposed contract](implementation_plan_o4a.md)

## Ordered delivery

- [x] Freeze the required authoring/compatibility decisions with implementer-facing examples before dependent runtime changes.
- [x] Extend pure D5 resource capture and parser output with explicit tool usages and inert descriptors. Add a static test-to-graph catalog, including integration fixtures independent of the selected application graph. Preserve existing v2/v3 semantics and selected replacement authority; reject unsupported versions.
- [x] Introduce the immutable operation tool plan and phased configuration composition. Bind implementation-local slots to named instance fields; recompute on controlling selections without stale requirements. Use existing presentation metadata for hidden selectors and visible endpoints.
- [x] Add a named operation-owned tool registry alongside the existing API route provider slot. Remove the assumption that every tool is `api_provider`; do not add repeated singleton API entries that overwrite ownership. Repeated factory paths are valid across distinct tool instances. Preserve start/cancel/rollback/retryable cleanup, borrowed invocation views and partial-start disposal.
- [x] Drive prerequisite roots and acquired endpoint handoff from selected tool instances through the delivered Compose planner. Replace demo-specific `requires_api`, graph-ID and endpoint wiring with declarations while preserving the application's existing behavioral contract.
- [x] Adapt the bundled GraphQL implementation to local configuration slots, retaining verification/schema/error/credential bounds. Migrate the useful demo and its controlled integration graph. Preserve API route consumers through an explicit legacy bridge until their supported migration is complete; ambiguous legacy/new combinations must reject.
- [x] Document how an implementer adds an instance, supplies defaults, overrides a Python implementation and binds configuration/prerequisites. Prove source, wheel and relocated external-pack use without relying on the development checkout.

## Production consumer joins

- [x] Deliver the [operation selection, staged resolution and actual Review requirements](implementation_plan_o4d.md); a field-list projection does not close editor/default/persistence acceptance.
- [ ] Deliver [executable admission, Runtime ownership, graph-port binding and acquired prerequisite handoff](implementation_plan_o4e.md) through normal application/test consumers and installed packs.

## Acceptance matrix

Every row requires deterministic assertions. Tool/catalog discovery must assert zero tool/native/test-module imports, subprocesses, network calls or service acquisition. Existing pytest collection may still import test modules and run its existing collection subprocess; this refactor adds no tool-discovery effects to that path.

| Case | Required result |
| --- | --- |
| Same instance used by several nodes/graphs | One operation-owned instance; stable identity and usage provenance |
| Two instances using the same factory | Independent endpoint/header bindings, invocation identity and complete cleanup |
| Application uses primary; indexed test uses secondary | Application Review requires only primary; selecting that test adds secondary requirements |
| Unselected or unused installed tool | No execution requirement or prerequisite acquisition |
| Selected test subset and multiple graph associations | Union only that selection; reject missing/ambiguous admissions |
| Conditional graph branch | Its declared tools are included without executing the branch condition |
| Implementation override | Selected descriptor contributes its own typed settings; incompatible interface or cycles reject before effects |
| Compatible REST alternate | Generic schema-driven consumer runs unchanged; protocol-shaped incompatible inputs reject |
| Prepared call used against another instance | Reject even with identical schemas; retain content-bound verification order |
| Pending local service output | Preflight accepts declared binding; factory receives it only after readiness; retain original and effective provenance |
| Selected sidecar: blank in development versus deployment | Managed ready output locally; explicit nonempty environment value required in deployment, with no default change or orchestration |
| Owner-required isolated runtime | Bounded admission and trusted verification; unavailable/unsupported runtime rejects without fallback |
| Remote endpoint | No local API/backing stack starts |
| Owner-selected local API and backing dependency | Existing dependency/readiness/seed order; output reaches correct instance; owned cleanup |
| Required prerequisite disabled by its own typed condition | Reject with the dependency chain before effects; do not auto-enable it |
| Two APIs explicitly share one compatible backing-service identity | Acquire/seed once; conflicting same-ID configuration rejects before effects |
| Omitted, blank, owner-default, explicit endpoint | Distinct intent/provenance retained; demo policy stays demo-scoped |
| Hidden implementation selector | Default absent from ordinary Review; explicit override validated; endpoint remains editable |
| Selection/configuration edit | No stale required fields, factory choices, endpoints or plan identity |
| Partial start, timeout, cancellation or failed release | No lost owner; cleanup remains retryable under existing lifecycle |
| Changed descriptor or installed relocation | Immutable admission rejects stale bytes; legitimate relocation preserves identity |

Use independently authored synthetic service fixtures for generic prerequisite proofs. This refactor does not itself deliver a real database, migration runner or new bundled OData provider; those owners will declare their own requirements when implemented.

## Delivery gates

Checked technical gates cover the delivered core. Unfinished matrix rows and linked transport, service-capability and platform requirements remain open.

- [x] Applicable units pass exact 100% measured line/branch coverage; focused rejection mutations prove intended guards.
- [x] Controlled integration and package/relocation checks preserve existing tool, workflow and lifecycle behavior.
- [x] Independent architecture/implementation review and required external review close reported issues.
- [x] Public index, changed artifacts, metadata, dependency/license and provenance checks pass before accepting public changes.
- [ ] Human review the concrete format, two-instance configuration behavior and application/test Review contexts. Do not infer this acceptance from controlled evidence.

## Existing workflow qualification

A source change invalidates the existing workflow coverage qualification until an explicit reviewed refresh. Preserve original observations and fingerprints; never replace source identities merely to retain a cap. The [fresh tool-runtime qualification](implementation_plan_b2b1a7.md) records the replacement measurements and pending human acceptance of the renewed normal run. Shared-target execution retains its separate requirements.

## Independent alternate consumer

- [x] Run exact selected integration cases with independent primary and secondary targets, then select a compatible synthetic REST implementation for secondary through its hidden implementation selector. Preserve primary API_ENDPOINT/API_HEADERS and secondary API_2_ENDPOINT/API_2_HEADERS; add only the REST implementation's API_2_PATH setting. The same schema-driven conversation must use each selected ToolSpec without translating GraphQL arguments. Assert distinct values, headers and owners, content-bound grants and cleanup. Managed targets use the existing prerequisite session; explicit remote endpoints acquire no fixture services. This fixture does not deliver a general REST/OData provider.
