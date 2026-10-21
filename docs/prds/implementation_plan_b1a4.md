# Grouped declarations, truthful control flow and compact inspection

[Workflow presentation](implementation_plan_b1a1.md) · [Declaration contracts](implementation_plan_b1b.md) · [Previous scope repair](implementation_plan_b1a3.md)

This revision follows renewed presentation review. Preserve earlier accepted execution/provider contracts and original verification evidence. Combined declaration/diagram acceptance remains open.

## Editor and declaration structure

- [x] Put the root-only editor instructions immediately after its mode hint, before metadata or script declarations. Preserve the exact inert carrier, early CSP, source maps, fallback behavior and legacy envelopes. Child documents retain their minimal envelope.
- [x] Accept unversioned `Component: ID` and `Node: ID` headings while retaining existing versioned declarations. Root-selected execution semantics remain explicit; changing a displayed heading cannot switch engines.
- [x] Support component-local leaf declarations using `## Node: ID`, existing node sections at H3, and local Flow links such as `[node llm_call](#node-llm_call)`. Keep optional split declarations and stable-ID replacement compatibility. A nested one-node component can own a separately organized node.
- [x] Resolve inline schemas, native sources and prompts relative to their containing document. Preserve immutable physical-file snapshots, exact line maps, stable node identities, per-node reference ownership and scoped prompt provenance. Reject missing/duplicate/foreign fragments, unreferenced definitions, invalid nesting and fenced fake headings. Capture each physical document once; node navigation targets its owning component's fragment.
- [x] Group bundled application, qualification and integration-fixture nodes and prompts into their components. Preserve prompt bytes, schemas and native ownership except explicitly reviewed behavior changes. Retire only the replaced declaration pages and their known generated artifacts; retain unrelated files and recovery evidence.

## Deadlines and exclusive routing

- [x] Continue the existing shared timeout/progress scope for every graph, component and node, even with no local timeout. Parent deadlines clip descendants; a component's single budget covers all internal passes. Local timeout declarations remain optional. Give every bundled graph and component an explicit positive configurable timeout; omit individual-node declarations in these examples. Preserve cancellation, cleanup, accounting and hierarchical elapsed/effective-deadline reporting.
- [x] Use `otherwise` for a conditional edge set's fallback. Reject mixed conditional edges and `always`, duplicate/mixed fallbacks and ambiguous matches. A sole unconditional `always` remains compatible. Prompt composition keeps its independent unconditional-prompt semantics.
- [x] Admit bounded, declaration-visible predicates using Python comparison syntax, including `first_draft["route"] == "skip"`. Parse and interpret a strict AST allowlist over admitted output fields and scalar literals; prohibit calls, imports, arbitrary attributes, assignment and unbounded expressions. Keep registered native condition identifiers compatible. The saved and compiled branch labels must describe the same executed predicate, with canonical escaping and no native-code inspection during generation.
- [x] In the qualification example, expected content-format rejection is a validation result consumed by its guard and feeds bounded retry. Malformed provider/protocol/schema output remains an error. Preserve original input and routing choice during repair. Show actual acceptance, retry while below the pass limit, and terminal exhaustion from the same admitted component contract. Do not draw a validation retry that execution does not implement.
- [x] Preserve `NodeContext.attempt` and `max_passes`, explicit counter bindings and ancestor restoration. Demonstrate controlled final-attempt behavior in the deterministic fixture. Keep journal completeness, failed/partial evidence, timeout events and visit/attempt accounting consistent with actual control decisions.

## Compact diagrams and bounded references

- [x] Separate input and output on explicit lines. Use compact linked model/schema labels rather than long field lists, with accurate node-fragment targets. Show available attempt context separately from declared input fields. Preserve opaque component counts and scoped START/END.
- [x] Display relevant declared settings, including inherited timeout keys and retry-budget references, as individually clickable links. Multiple references are allowed; never infer settings by executing or scanning native implementation code.
- [x] Make only selected, captured schema/configuration references inspectable through bounded deterministic companion pages. Schema pages show the captured contract; configuration pages show only the referenced declaration, never runtime/environment values or unrelated declarations. Tie routes and bytes to admitted provenance, validate generated HTML/links, preserve static relative navigation and finite loopback inventories, and reject stale, unadmitted or escaping resources. General native-source browsing remains separate work.

## Verification and acceptance

- [x] Prove mixed inline/split ownership, both execution engines' compatibility, replacements, prompt tampering, predicate safety/exclusivity, timeout inheritance and retry/exhaustion with controlled graph executions and meaningful mutations. Verify real renderer output, managed HTTP navigation, unavailable-script guidance, orphan-safe generation, staged isolation, relocated packages, notices and exact applicable measured coverage.
- [x] Preserve the original qualified observations and cap22 record as historical. Do not silently rebind materially changed validation/retry behavior to it. Determine the final effective sampling difference after source review; require explicit authorization and independent review before fresh live qualification or promotion. Styling-only changes alone do not authorize new inference.
- [x] Finish independent lane and joined external review, reconcile technical status recursively, and retain failures and evidence privately. No publication is implied.
- [ ] Human acceptance of editor-first guidance, grouped component/node content, linked compact diagrams, truthful exclusive branches/retries and deadline visibility. Earlier preparation acceptance does not establish this checkpoint.

## Admission and grouped-reference consistency

- [x] Attribute each Functions row’s input/output schema references to its declared node ID, including rows written at root or component scope; preserve the containing file and line as physical provenance.
- [x] Apply one shared guarded-component pass limit of 99 at both declaration admission and journal validation. Reject 100 before execution; prove a successful recorded run at 99 remains valid through retained-evidence validation.

Technical source, qualification and delivery checks are complete. Direct-file, repository-platform and combined declaration/diagram acceptance remain unchecked above. The separately planned [native Allure scope projection repair](implementation_plan_b1c1a1.md) retains its own open presentation gate; correct journals/accounting do not establish a duplicate-free native step view.

## Diagram readability follow-through

The subsequent [readability and route-context requirements](implementation_plan_b1a5.md) supersede this slice's SVG-only attempt/budget annotations. Earlier source closure remains valid for its delivered scope. The new layout/notation technical follow-through is reviewed; its final human presentation gate remains open.
