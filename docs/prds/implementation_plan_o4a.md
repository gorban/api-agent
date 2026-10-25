# Tool discovery and binding requirements

[Requirements](implementation_plan_o4.md) · [Migration](implementation_plan_o4b.md)

These requirements define the agreed bounded contract. Production implementation and acceptance remain separate gates.

## Identity and authoring

Add an explicit, versioned `Tools` declaration to the executable workflow grammar. A usage names a graph-local tool alias and links to an inert owner-scoped TOML instance descriptor. The same loader admits these links with the existing root, replacement, bounded-read and immutable-capture rules. Do not extract dependencies by analyzing Python bodies. Shared implementation descriptors/configuration are captured once; distinct configured instances remain distinct.

An instance descriptor joins:

- A stable owner-qualified instance ID and versioned interface/capability requirement.
- A default implementation descriptor/factory and an optional typed configuration selector overriding it.
- Bindings from implementation-local slots, such as `endpoint` and `headers`, to owner-declared configuration fields.
- Owner-declared endpoint policy and prerequisite roots/outputs, including any service declarations contributed by the selected implementation.

The interface describes the callable contract; its identity is independent of GraphQL, REST or OData transport. A factory must declare compatibility before binding. A workflow with GraphQL-shaped inputs cannot silently acquire a REST implementation. The dynamic tool-schema consumer can use compatible alternatives without adding a general query translation language.

Illustrative relationships, not final TOML syntax:

| Instance | Default implementation | Local slot | Configuration field |
| --- | --- | --- | --- |
| primary | GraphQL | endpoint | API_ENDPOINT |
| secondary | GraphQL or compatible custom tool | endpoint | API_2_ENDPOINT |

Only explicitly declared sharing can merge configuration or service identities. Conflicting declarations reject before effects. Two instances using one factory must have separate configuration and ownership unless explicit compatible sharing is supported.

## Resolution phases

1. Resolve only owner/root, graph/test catalog and typed bootstrap selectors needed for discovery. Load explicit static graph entries and selected replacements without importing native catalogs, tests or tool factories.
2. Capture a catalog of graph-to-instance usages, including all explicitly declared integration graphs. Keep this discovery catalog separate from the current operation. Tests declare their graph associations statically; controlled generated variants require trusted caller admission before worker execution. An unknown association rejects rather than borrowing the application graph.
3. Select the application's closure or the union of selected tests' graph closures. Include structurally declared conditional branches; do not predict runtime LLM decisions. Only this closure selects implementation descriptors and contributes execution configuration requirements.
4. Resolve implementation selectors, compose their inert typed declarations, bind slots and resolve normal values with existing precedence and provenance. Keep selector dependencies acyclic and bounded. Missing execution values do not prevent static discovery. Inactive known fields do not prompt or block execution; explicit values are retained for later selection.
5. Build an immutable tool selection plan containing captured graph/descriptor identities, qualified instances, factories, bindings and prerequisite roots. Validate all selected config, compatibility and dependency closure before startup. Selection changes invalidate and rebuild the plan.
6. Acquire prerequisites through the existing lifecycle. Supply acquired endpoint outputs through per-instance operation configuration; never overwrite persisted user intent. Then import and instantiate selected factories, run verified tool calls and dispose through the same operation owner.

The plan joins existing owners; it is not a second general resource scheduler. Static captured tool declarations belong to definition identity; selected implementations and request-affecting configuration belong to a separate operation binding identity. Test admission joins both. Prepared calls bind to the tool instance and operation identity so a verified call cannot be replayed against another instance, even with an identical schema. Ordinary diagnostics expose identities and safe sources; credential values and secret-derived fingerprints stay excluded.

## Configuration and prerequisites

Use existing presentation metadata where it meets the requirement: `acknowledge_default = false` hides implementation selectors from ordinary Review; endpoint fields retain visible acknowledgement. Do not add a new visibility mechanism unless current behavior cannot satisfy the agreed semantics. Hidden selection remains configurable through documented mechanisms and must never bypass validation.

The implementation declares local slot types; the instance owner chooses public names and defaults. Preserve absent, explicitly blank, owner-default and explicit nonempty values. Blank/local provisioning is an owner policy, never a global endpoint rule. The bundled synthetic target retains its existing policy; external packs do not inherit it.

A required endpoint may be a validated symbolic service output during preflight; it becomes a usable effective value only after successful acquisition. Retain user intent/source separately from acquired-output provenance. Missing remote endpoints still reject before startup. Prerequisite outputs bind to the correct instance. Reuse existing owner-qualified dependency DAGs, host/container addresses, readiness/seed gates and compatible service sharing. A tool can request an API whose owner declares a recursive backing-service dependency. A required dependency disabled by its own typed condition rejects rather than being auto-enabled. Explicitly shared compatible backing-service identities acquire once; conflicting same-ID configurations reject. Concrete database and migration implementations remain separate work.

## Optional inputs and service capabilities

- [ ] Apply [empty-schema/empty-prerequisite and storage-capability requirements](implementation_plan_o4g.md). Settings, injected in-process services and managed containers are independent; none is mandatory for every tool.

## Decisions before implementation

- [x] Agree the version 1 Tools table, instance/implementation declarations and static exact-case graph catalog, including finite declared variants.
- [x] Agree versioned create_tool(local_config, services) compatibility, retaining protocol-specific operation schemas and validating callable capabilities before acquisition.
- [x] Agree named selector/local-slot binding, explicit sharing and collision rejection, owner-contained installed resolution, and legacy API translation only at its compatibility boundary until those consumers migrate.
- [x] Confirm application versus exact selected-test Review context and rebuild after controlling edits; execution uses the same selected requirements.

Apply the detailed [operation/configuration requirements](implementation_plan_o4d.md) and [ownership/admission requirements](implementation_plan_o4e.md). Defaults above are recommendations; unresolved choices remain visible until agreed.

## Agreed version 1 authoring contract

Tools declarations have exactly Port and Instance columns; each row links an instance TOML from its selected workflow scope. Nearest lexical scope supplies a port; conflicting inherited aliases reject. Preserve scope kind and every declaration location when instance usages deduplicate.

Instance and implementation descriptors use version=1, owner-qualified IDs, versioned interfaces, typed selectors and local-slot bindings. Factories expose synchronous create_tool(local_config, services), return a version 1 owner, and provide validated asynchronous describe, validate, invoke and aclose capabilities. Runtime ToolSpec validation and independent verification remain mandatory.

A version 1 pack tooling catalog maps exact repository-qualified pytest case IDs to explicit graph entries and replacement sets; finite variants are declared rather than worker-invented. Test selection never implicitly adds the application graph. Unknown explicit CLI options and known inactive tool options reject with distinct contextual diagnostics; dormant saved/environment values remain retained without activating tools. The legacy API_* translation remains only at the existing API-provider entry point until its remaining consumers migrate; mixed local/legacy inputs reject.

These decisions were accepted on October 25, 2026. They close contract decisions, not implementation, platform qualification or human production acceptance.

## Resource and operation details

- Distribution-owned implementation links use package:DISTRIBUTION:PACKAGE/PATH with bounded inert receipt/content verification and no installed-to-checkout fallback. Relative resources retain owner containment.
- Explicit local.activation metadata declares typed operation-only controller values for a pending local output. Conflicting explicit values reject; this does not enable disabled transitive dependencies or persist acquired addresses.
- Exact catalog associations and finite variants remain authoritative for workflow tests. Freshly collected graphless identities retain their independent requirements; arbitrary unknown case strings reject. Captured per-case slices cannot expand the selected closure.
- Shared-primary profiles remain fixture-local; a selection mixing incompatible shared and independent semantics for one qualified instance rejects before acquisition.

[Implementer guide](../tools.md) documents authoring and lifetime rules; delivery gates remain in the migration requirements.
