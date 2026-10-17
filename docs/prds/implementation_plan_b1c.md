# Workflow inspector overlays

[Resource viewer](implementation_plan_b1a.md) · [Conversion](implementation_plan_b1b.md) · [Accounting](implementation_plan_f3.md) · [Browser ownership](implementation_plan_k2.md)

Extend the same managed viewer late in application delivery, before final profiling. The compiled declaration remains immutable; observations and comparison overlays never modify graph behavior. Static `--view-flow` remains usable without history, authentication, inference or application startup.

## Architecture and order

1. [ ] Reserve stable graph/node/edge and execution identity seams during graph/application implementation. Add origin-side transitions and invocation/call attribution before depending on new replay evidence; older incomplete records stay visibly partial. This early seam is not an early commitment to build the inspector UI.
2. [ ] Implement the [execution evidence and delivery contract](implementation_plan_b1c1.md): one versioned, scrubbed observation model/reducer consumed from finalized E2E artifacts and bounded local-run memory. Reuse accounting identities; do not put input/output payloads into accounting records.
   Freeze [document hashes, Git provenance and historical source lookup](implementation_plan_b1c3.md) alongside capture, including explicit uncommitted-source handling.
3. [ ] Implement [historical comparison, replay and diagnostic navigation](implementation_plan_b1c2.md) through the existing server/browser. Definition, Recorded execution and Live run modes share the same selected graph and detail projections.
4. [ ] Review deterministic replay, actual managed-browser lifecycle, privacy/bounds and evidence integrity independently, then obtain human acceptance of history comparisons, turn/visit navigation and live following. These are new gates, independent of initial static viewer acceptance.

## Design choices

Use bounded JSON reads plus one-way server-sent events for live updates; the browser and command-line diagnostics share projections. Completed records use the graph definition captured with that execution, not today's graph. Historical means disclose compatible cohort, sample count, missingness and window; scenario controls cannot invent the behavior of an unseen model or reasoning level. No separate trace backend or live persistent database is required for this inspector.

Exact schema versions, public read routes, capture allowlists and numeric retention/download/render budgets must be frozen and adversarially reviewed before implementation. Their selection is still work, not an implemented public API. Existing content/privacy, historical compatibility, resource isolation, timing and owned cleanup contracts apply.
