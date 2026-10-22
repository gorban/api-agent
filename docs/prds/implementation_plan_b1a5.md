# Diagram readability and route context

[Grouped declarations](implementation_plan_b1a4.md)

This follow-through supersedes earlier diagram-only attempt-context and pass-budget annotations. Runtime controls and Markdown declarations remain authoritative. The user accepted the direction and shorthand; preview appearance alone does not establish parser support or verification closure.

- [x] Choose top-down layout for root and recursively generated component diagrams.
- [x] Apply that direction consistently to saved, selected and packaged diagrams. Omit repeated attempt-context boxes and separate pass-budget summaries from SVGs; retain the pass limit and node context documentation in Markdown.
- [x] Show the concrete retry/exhaustion boundary in guard route labels. Prefer Boolean negation over comparison with `False`. Keep condition labels inert under SVG admission, with captured configuration links and the component policy table available for progressive disclosure.
- [x] Choose leading-dot source-relative syntax: `.accepted` and `not .accepted` read the direct routing source node's declared output. Prefer explicit dotted map paths such as `first_draft.route`; retain string indexing for compatibility and non-identifier keys. Bare `attempt` / `max_passes` read nearest component context; `.attempt` remains an output field. Components without their own declared output cannot infer a child result.
- [x] Admit those expressions through bounded, schema-checked data access. Boolean negation requires declared Boolean fields; keep explicit cross-node reads, short-circuit behavior and source-scoped predicate identity. Attribute-looking syntax accesses map keys only, never Python object attributes or calls.
- [x] Preserve the runtime meaning of attempt and max passes: one-based component-local pass count, reset per component invocation, bounded retry on rejection, immediate completion on acceptance and terminal exhaustion when no pass remains. These are controls, not extra executable nodes.
- [x] Keep generated controller route labels faithful to admitted source/context semantics. Preserve immutable provenance, conditional exclusivity, inherited timeouts, scoped component counts, linked input/output and static/hosted navigation.
- [x] Verify applicable coverage, meaningful predicate/controller mutations, real diagrams, installed assets and independent/external review before commit. Do not change runtime behavior or refresh sampling observations merely to implement a layout preview.
- [ ] Human acceptance of the final root/component presentation and chosen authoring syntax.

## Configurable component pass limits

- [x] Choose a linked configuration declaration in the Max passes cell, such as `[WORKFLOW_MAX_PASSES](../../cfg/config.toml)`. Conditions may reference that declared uppercase name directly; no separate Settings table is required.
- [x] Capture and validate the linked declaration through normal resource ownership. Require an integer default and effective value from 1 through 99; reject missing, ambiguous or invalid declarations before execution. Preserve literal pass limits for compatibility.
- [x] Resolve effective configuration once per workflow. Use the same limit for execution, attempt context, predicates, journal validation and qualification fingerprints. Preserve immutable source provenance and detect changes to the effective limit.
- [x] Show the symbolic limit and a configuration link in diagrams; identify static declared defaults separately from effective runtime overrides. Verify a controlled override changes both execution and retained evidence consistently.

## Equivalent contracts and operand positions

- [x] Treat a singleton JSON Schema type array as its equivalent scalar type for Boolean shortcuts and nested object-path admission, including immutable captured schema forms. Keep nullable or mixed unions outside exact Boolean/object shorthand contracts.
- [x] Admit source-relative field reads in either operand of a supported comparison, preserving declared-field checks, string/number literals, bounded syntax and rejection of internal aliases.
- [x] Prove both cases through declaration loading and real controlled execution, with causal negative controls; renew source bindings and verification before delivery.

## Direct source navigation

- [x] Point input/output schema, timeout and pass-limit diagram links directly to their selected, captured `.json` or `.toml` source. Use relative source paths in saved and hosted views and the recorded repository identity with `_____switch_branch_____` in GitHub links. Generate no schema or setting HTML wrappers.
- [x] Serve only admitted JSON and TOML bytes in the hosted route inventory. Bind route content, source provenance and SVG links to the immutable capture, and reject altered or unadmitted bytes. Keep source limits and owned-orphan cleanup for superseded helper pages.
- [ ] Human review of direct source navigation in the final static and hosted diagrams.
