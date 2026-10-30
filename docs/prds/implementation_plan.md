# api-agent checklist

## Testing during implementation

Write deterministic requirement tests, preferably red-to-green. Before code commits, require applicable units at exact 100% line/branch coverage under AGENTS.md. Use focused mutations for ambiguous rejection causes.

The TUI enforces exact All tests coverage; partial coverage is informational. CI requires applicable units/coverage before merges. Units never enter Allure. Renew coverage as features land and before cleanup. Agree final E2E pass-rate thresholds/measurement separately.

## Checklist

- [ ] Next: deliver [mise startup and conditional prerequisite preparation](implementation_plan_p.md).

- [x] Complete reviewed Segment 1A: typed/versioned config/pack declarations, source launchers, hierarchical overrides, terminal help and the stale-worktree gate. Human checkpoint A accepted 2026-10-04; remaining TUI/runner/auth/Layout work stays open.

- [ ] Establish the runtime/resource-pack skeleton by responsibility, node and prompts. See [skeleton grouping plan](implementation_plan_g.md).
- [ ] Build a Python TUI for application startup and unit/integration/E2E execution. See [TUI grouping plan](implementation_plan_a.md).
- [ ] Add conditional infrastructure profiles for Langfuse, local upstream API/database prerequisites, and configurable host Nx UI startup. See [infrastructure and UI grouping plan](implementation_plan_h.md).
- [ ] Establish durable shared reporting-service ownership and one persistent accounting cache per computer, preserving fixed application ports and existing suite exclusion. See [shared resource coordination](implementation_plan_h2.md).
- [ ] Establish LLM connectivity, early authenticated discovery and token/quota tracking before first integration/E2E execution. See [providers and usage grouping plan](implementation_plan_f.md).
- [ ] Establish LangFuse/OTEL configuration, local profiles, tracing and content scrubbing before the first Ok run. See [tracing grouping plan](implementation_plan_k.md).
- [ ] Add running-container inventory and confirmed TUI cleanup for retained/crashed stacks. See [container cleanup grouping plan](implementation_plan_l.md).
- [ ] Start with one final LangGraph renderer saying "Ok", no verifier/guard nodes, through the existing frontend API; include example integration and human-accepted E2E tests. See [initial LangGraph grouping plan](implementation_plan_e.md).
- [ ] Establish runtime-parsed declarative configuration, LangGraph flow and other executable descriptions, deriving configuration docs and flow diagrams. See [declarative formats](implementation_plan_b.md) and [ordered format/fixture conversion](implementation_plan_b1b.md).
- [ ] Start a README describing application and TUI usage, with configuration documentation derived from executable descriptions. See [README grouping plan](implementation_plan_c.md).
- [ ] Establish integration/E2E Allure before their first rewrite tests; exclude units, persist in S3 and allow a secondary destination. See [Allure grouping plan](implementation_plan_d.md).
- [ ] Track UI API refinements, deployment-log integration, and manual prompt/instruction-edit hook enforcement without losing deferred requirements. See [engineering follow-up grouping plan](implementation_plan_i.md).
- [ ] Keep conservative [log/evidence scrubbing](implementation_plan_j.md) under explicit LOG_CONTENT opt-in and deliver the separate [trace-content control](implementation_plan_k1b.md) shared by OTEL/LangFuse. Credentials remain excluded under both.

- [ ] Share unattended provider sessions and operation-wide accounting across tests and explicitly unattended local runs; providers own refresh/persistence. See [unattended lifecycle grouping](implementation_plan_a7.md).

## Follow-through and current architecture

See [delivery evidence, current provider architecture and accounting follow-through](implementation_plan_n.md). Their original completion marks and remaining work are preserved in that child.

## Frontend prerequisite

- [ ] Complete [reasoning/message identity](implementation_plan_f2f.md) before [frontend integration](implementation_plan_i2.md).

## Late CI follow-up

- [ ] Complete interactive hooks, staged checks, graph diagrams and no-Git CI parity after graph contracts settle. See [hook and CI requirements](implementation_plan_i1.md).

- [ ] Extend the static workflow viewer with [historical, recorded and live inspection](implementation_plan_b1c.md) after application/E2E evidence and before final profiling.

## Final follow-up

- [ ] Last, profile native-key scanning; optimize only if material, otherwise record a no-change conclusion. [Requirements](implementation_plan_f2e1.md).

## Design decisions and pack prerequisites

- [ ] Resolve blocking implementation decisions with the user before dependent behavior, and prove conditional pack-contributed infrastructure through the shared lifecycle manager. See [follow-through requirements](implementation_plan_o.md).

## Selection estimate visibility

- [ ] Keep pre-start integration/E2E `Est. $` visible with long names and roll estimates into every selection grouping. See [visibility and rollup requirements](implementation_plan_a11.md).
