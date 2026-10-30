# Caller-aware component boundary navigation

[Workflow resources and views](implementation_plan_b1a.md) · [Shared SVG interaction](implementation_plan_b1a7.md)

Later presentation work, after the existing viewer/application priorities and before final credential-sanitization profiling. Use the admitted selected graph and shared diagram interaction; no graph execution, inference or authentication is required. Repository-hosted Markdown/SVG rendering is outside this enhancement's scope.

## Navigation semantics

- [ ] Keep reusable component declarations and saved SVGs independent of any fixed caller. Opening a component directly must not invent a parent from browser history, HTTP referrer or a single usage elsewhere. With no valid caller context, boundary controls explain the missing context and may offer an explicit choice of admitted callers.
- [ ] When following a component link from a containing diagram, carry a bounded caller stack identifying the selected graph/definition, containing scope and exact component occurrence. START returns to the parent diagram at the component occurrence through which the viewer entered; highlight that occurrence. It does not claim which predecessor ran. Support nested composition and repeated use of the same document without confusing callers.
- [ ] END follows the component's declared outward continuation only when all possible exits resolve to one destination without evaluating runtime output. Explicitly account for implicit edges, enclosing END boundaries and component retry/exhaustion decisions. Do not mistake the last node's schema or a conditional label for a known value or a completed invocation.
- [ ] If an exit depends on output, a router, retry state or another unresolved choice, return to the nearest ancestor diagram that owns the decision and highlight its component/routes. This can require unwinding several enclosing boundaries. Show available alternatives and the reason for ambiguity; do not choose a branch or imply observed execution. Bound ancestor traversal and stop safely for loops or incomplete context.
- [ ] Keep the workflow root's own START/END terminal; do not invent an external caller. Provide understandable labels/tooltips for return-to-caller, unique continuation, ambiguous continuation and missing-context behavior. Preserve keyboard access, focus, Back/Forward and modified-click/new-tab behavior.

## Environments and boundaries

- [ ] Guarantee this interaction in managed `--view-flow` using its captured graph and finite resource inventory. Reuse the existing shared scripts for ordinary `file://` viewing where the browser permits the necessary context and diagram access. Prefer parity, but allow a clearly explained managed-only fallback without insecure flags, arbitrary local reads or a new installation prerequisite.
- [ ] Keep caller context separate from immutable declarations/diagrams and future execution observations. Validate it against the current selected graph and admitted scope/edge IDs; reject stale, forged, cross-graph or escaping destinations. Context is a navigation hint, never filesystem or execution authority. Freeze context transport, size/lifetime limits, refresh and cross-tab behavior before implementation. Do not depend solely on document referrer or storage behavior that differs for file origins.

## Verification and acceptance

- [ ] Exercise unique and ambiguous continuations, multiple-level unwinding, retries/loops, repeated component use, direct entry, missing/stale context, new tabs and Back/Forward. Prove different caller contexts lead to their respective call sites while saved component bytes remain identical. Verify actual managed browser behavior and any claimed ordinary-file support; preserve plain navigation when enhancements are unavailable.
- [ ] Complete independent/adversarial review, causal regression controls, applicable exact coverage, packaged/relocated assets and public-boundary checks before delivery. This later task does not reopen accepted static interaction behavior.
- [ ] Human acceptance of caller-aware START/END navigation, including conditional and nested examples. No implementation or acceptance is recorded yet.
