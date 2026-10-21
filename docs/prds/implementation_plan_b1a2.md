# Generic graph names, title and component counts

[Versioned workflows and views](implementation_plan_b1a.md)

The bundled chat example uses independently authored generic declarations and native call contracts. Labels describe behavior; changing labels does not establish source ownership.

- [x] Display the exact heading `LangGraph flow` in the browser and repository Markdown. Accept only literal `# LangGraph flow` as an additional v2 root heading, preserving legacy `# Workflow v2`, v3 version selection, plain-text heading checks and the root-only carrier/fallback contract. Reject arbitrary alternative titles or injected markup.
- [x] Model the bundled chat as component `agent` containing executable node `llm_call`, with matching registry, schemas, relocated packaged assets and tests. Preserve request history, selection, request identity, cache/reasoning options, timeouts, results and internal accounting. A `Components` row with `Guard` equal to `-` permits exactly `Max passes` equal to `1`: execute the child graph once, retain component timeout/ownership and attempt attribution, and accept only after successful child completion. Reject a larger budget before execution; emit no synthetic guard result or retry edge. Restore outer previous/feedback/attempt context on exit. Preserve guarded retry and v3 shared-state component semantics, journal validation and accounting.
- [x] Label component SVG packages with `(<N> nodes)` only for at least two recursive executable descendants. Exclude component containers and START/END. Keep single-node labels unchanged. Derive counts from the complete admitted graph for both root and nested declaration views, independently of visible-edge filtering.
- [x] Verify both independently authored three-node example components, nested containment, single-node behavior, real pinned SVG output and managed loopback navigation. No provider call is needed. Keep direct-file human acceptance separate from managed HTTP evidence.

Implementation and independent/external source review are complete. The 12,708-unit gate passes at exact measured line/branch coverage, with causal journal controls and relocated installed-package checks. Public provider/application metadata contracts are unchanged by this naming and presentation work.

- [x] Admit an edge following a child only when its recorded source scope ended with outcome `passed`. A successful enclosing one-pass component or graph cannot make a failed child a complete causal route, including retained journals with a recomputed digest. Keep failed runs valid as partial observations; preserve guarded retry after a passed guard node returns rejection and the existing terminal-root observation policy. Verify both journal finalization and retained-journal validation.

- [ ] Human acceptance of the displayed root title, agent/component navigation and declaration/diagram presentation, alongside the combined viewer checkpoint. Managed HTTP evidence does not substitute for direct-file or repository-platform observation.


The [scope/discovery repair](implementation_plan_b1a3.md) supersedes package/group rendering and the earlier count-label syntax. Retain one-pass runtime behavior; diagrams show one opaque component at its parent and its children on the component page.
