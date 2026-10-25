# Tool ownership, admission and production delivery

[Tool requirements](implementation_plan_o4.md) · [Selection/configuration](implementation_plan_o4d.md) · [Migration gates](implementation_plan_o4b.md)

## Declaration and executable admission

- [x] Version the Tools table, instance descriptor, implementation descriptor and test-association catalog. Validate required/unknown keys, bounded stable IDs, duplicate ports/instances, root containment, symlinks, capture races and total resource limits. Preserve an explicit alias scope/inheritance policy; do not silently rebind a parent's port.
- [x] Capture selected instance and implementation descriptions, typed declarations, factory/native source closure, dependency/package identity, assets and required licenses through the established resource/provenance owners. No source-checkout fallback or uncaptured later reread may change admitted execution. Prove source, wheel and relocated external-pack resolution independently of cwd.
- [x] Resolve a workflow's declared port to its selected qualified instance and provide a borrowed invocation view through graph services. Calling nodes must not rely on a magic global API singleton or an example-specific graph ID. Missing/ambiguous port bindings reject before graph execution.
- [x] Check a versioned callable/capability contract before factory acquisition and validate the runtime ToolSpec before argument generation. A matching interface string alone is insufficient. Preserve protocol-specific schemas where needed; do not silently reinterpret GraphQL documents as REST/OData requests.
- [x] Admit every selected test's graph and tool binding plan in the trusted runner before dispatch. Workers must consume those admissions rather than expand roots, select another implementation or substitute the application's graph. Preserve definition/operation identity across retries and resumes.

## Runtime and prerequisites

- [x] Own named tools inside the existing operation Runtime transaction, retaining the separate API route provider contract. Distinct tool instances may use the same factory; they must not overwrite a singleton provider slot. Share an owner only through an explicit compatible declaration with one disposer.
- [x] Register ownership before awaited startup, preserve the primary failure alongside cleanup-pending evidence, drain invocation views before parents, and retain all pending tool/prerequisite owners through cancellation and retryable cleanup. Respect existing event-loop/process ownership, timeout/progress scopes, emissions/redaction and teardown dependency order.
- [x] Let selected instance/implementation owners contribute prerequisite roots and configuration. Capture each contribution with its own declared owner/root; do not assign all tools to one hardcoded infrastructure owner or load every installed tool's infrastructure.
- [x] Reuse the current dependency planner for recursive service order, typed activation, host/container outputs, readiness, seed completion, compatible reuse and lease/volume policy. Shared backing services acquire once only under the existing identity/compatibility checks. A disabled required dependency rejects; conflicts never silently enable or merge services.
- [x] Accept a valid symbolic local output as provisionally satisfying its declared input during preflight. Create factories only after all their outputs are ready, using per-instance effective values. A missing remote endpoint fails before acquisition. Demonstrate actual lifecycle acquisition and endpoint handoff, including failures; a pure dependency plan alone is insufficient evidence.
- [x] Move bundled demo endpoint policy and factory/slot bindings into the owning declarations. Preserve omitted, blank, owner-default and explicit nonempty behavior without imposing a synthetic fallback on external packs. Keep real database/migration delivery with its service owners.
- [x] Supply the GraphQL owner with local endpoint/header slots; keep its existing deterministic verification, MCP package/integrity, schema/error handling and credential exclusions. State the supported legacy API-setting bridge and reject ambiguous explicit old/new configuration until migration removes that bridge.

## Managed and externally owned sidecars

- [ ] Deliver [stable blank defaults, deployment explicit-address validation and isolated runtime admission](implementation_plan_o4f.md) for selected sidecar-backed tools. Development provisioning and deployment service ownership remain separate authorities.

## Required verification and decisions

- [x] Add deterministic config/TUI tests for real Review membership and edits across app, primary-only tests, dual-target tests and alternate implementation; include persistent values, conditional selector defaults, CLI duplicates and unknown/inactive inputs.
- [x] Test exact selected-case associations, multiple graphs, replacements, finite generated variants and stale/forged worker admissions. Prove zero tool discovery effects separately from existing pytest collection behavior.
- [ ] Run the actual selected application/test consumer with two named GraphQL instances and a compatible alternate implementation through its normal operation owner. Verify independent headers/results, cross-instance grant rejection, no replay, cancellation, partial-start rollback and cleanup retry.
- [ ] Prove local/remote dependency branches and shared backing through the shared lifecycle, then source/wheel/relocated pack behavior with no development checkout. Retain applicable exact unit coverage, causal rejection mutations, independent/external review, public-boundary and human acceptance gates.

The authoring, association, alias, capability, inactive-CLI and legacy-transition decisions are recorded in the discovery requirements. Remaining transport, operation-service and platform acceptance stays separate from the delivered core.

## Bounded factory preflight

- [x] Before importing any selected factory, validate all captured entries as direct, undecorated synchronous function definitions with exactly two positional parameters: `create_tool(local_config, services)`. Reject generators, dynamically assigned aliases and decorated entries in this version. Validate returned owner capabilities separately after creation.
- [x] Bind dependency admission to the executable origins actually used by cold imports and already loaded modules, including enabled dependency extras and their transitive requirements. An installed distribution receipt must not authorize a different module earlier on the import path.

## Scope inventory and final-use cleanup

- [x] Deliver [declaration-bound inventory and final-use cleanup](implementation_plan_o4e1.md). Recognize declared neighbors without acquiring them; preserve locked leases and transfer eligible cleanup responsibility to the last active consumer.

## Delivered verification subsets

- [x] Exercise named GraphQL consumers, independent bindings/results, cross-instance grant rejection, cancellation, partial-start rollback and cleanup retry through existing operation ownership.
- [x] Verify local/remote dependency branches, shared backing and source/wheel/relocated generic-pack behavior with applicable exact coverage, causal checks, independent/external review and public-boundary gates.
The pending conjunctive verification requirement above retains compatible alternate-transport execution.
- [ ] Record explicit human acceptance of the delivered lifecycle and configuration behavior.

## Independent alternate consumer

- [ ] Run exact selected integration cases with independent primary and secondary targets, then select a compatible synthetic REST implementation for secondary through its hidden implementation selector. Preserve primary API_ENDPOINT/API_HEADERS and secondary API_2_ENDPOINT/API_2_HEADERS; add only the REST implementation's API_2_PATH setting. The same schema-driven conversation must use each selected ToolSpec without translating GraphQL arguments. Assert distinct values, headers and owners, content-bound grants and cleanup. Managed targets use the existing prerequisite session; explicit remote endpoints acquire no fixture services. This fixture does not deliver a general REST/OData provider.

## Installed dependency receipts

- [ ] Accept ordinary installer-generated bytecode cache entries without treating them as executable import roots. Ignore only an inferred cache root backed exclusively by unhashed generated bytecode and absent from explicit top-level declarations. Retain strict rejection of unowned, shadowed, changed, ambiguous or missing executable origins, including actual cache-named modules and explicit roots. Verify both dependency closure and declared-manifest discovery in installed artifacts.

## Managed synthetic fixture shutdown

- [ ] The included API/backing fixtures must handle SIGTERM and close their HTTP server successfully within the existing bounded prerequisite cleanup budget. Verify actual subprocess readiness, timely successful signal shutdown and preserved seeded data; a causal handler-removal check must fail. Do not expand the global cleanup deadline or change ownership policy to hide a fixture that ignores normal termination.
