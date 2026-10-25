# Workflow-selected tool instances

[Design decisions](implementation_plan_o.md) · [Proposed contract](implementation_plan_o4a.md) · [Migration and acceptance](implementation_plan_o4b.md)

Workflow tool usage determines configuration and prerequisites for the application or selected tests. Core capture, configuration, named ownership and GraphQL execution are delivered. The acceptance matrix and linked transport, service-capability and platform requirements retain their remaining gates. Existing tool and Compose contracts remain valid.

## Required outcomes

- [x] Parse explicitly declared application and integration/E2E graph entries, including nested selected declarations and replacements, to capture tool usages without executing native code. Discover test graphs independently of the Run local graph; do not infer graph selection from directory contents.
- [x] Distinguish a reusable versioned tool interface, a named configured instance, and its selected implementation. Deduplicate repeated uses of the same qualified instance; preserve separate instances even when they use the same Python factory.
- [x] Give each instance a default implementation target with a typed TOML-declared override selector. Hide that selector from ordinary Review configuration by default while preserving supported explicit configuration. Packs may choose to expose their additional tool settings. Active endpoint fields remain visible and editable.
- [ ] Bind each implementation's local configuration slots to instance settings. Support two GraphQL instances using `API_ENDPOINT` and `API_2_ENDPOINT`, and a compatible custom REST/OData implementation for either instance. Do not create a universal requirement for either endpoint name.
- [x] Derive required fields and recursive conditional service prerequisites from the selected operation's tool closure. Reuse the existing Compose planner, readiness, seed, lease and cleanup contracts; concrete services remain owner declarations.
- [x] Show application requirements in application Review configuration. Test discovery may describe other graphs; only selected tests contribute execution requirements. Recompute when graph, test selection, implementation or controlling settings change. An unrelated tool must not make its settings required.
- [x] Preserve independently verified tool execution, configuration precedence/provenance, privacy, immutable definitions, installed-pack behavior and all existing acceptance gates.
## Next delivery

- [x] Agree the [bounded contract](implementation_plan_o4a.md) and establish the [ordered migration and acceptance matrix](implementation_plan_o4b.md).
- [ ] Complete the matrix's remaining REST alternate, operation-service/four-shape, sidecar/platform, durable failure-evidence, evidence-destination, trace-attribution and human acceptance gates before expanding endpoint-dependent consumers.

The [selection/configuration requirements](implementation_plan_o4d.md) and [ownership/admission requirements](implementation_plan_o4e.md) specify production joins and remaining decisions. Apply [managed development/external sidecar policy](implementation_plan_o4f.md). Support [all configuration/prerequisite combinations](implementation_plan_o4g.md) and [shared-target dual-tool live acceptance](implementation_plan_o4h.md). Recording requirements claims no implementation or human acceptance.
