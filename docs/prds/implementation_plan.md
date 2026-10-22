# api-agent checklist

## Testing during implementation

Write deterministic requirement tests, preferably red-to-green. Before code commits, require applicable units at exact 100% line/branch coverage under AGENTS.md. Use focused mutations where rejection could pass for the wrong reason.

The TUI enforces exact coverage for All tests; partial coverage is informational. CI enforces complete applicable unit/coverage gates before merges. Units never enter Allure. Renew coverage as features land and before cleanup. Establish the separate final E2E pass-rate threshold and measurement rules with the user.

## Checklist

- [x] Complete the reviewed Segment 1A foundation: versioned typed config/pack declarations, direct source launchers, hierarchical overrides, concise terminal help, and the stale-worktree gate. Human checkpoint A accepted on 2026-10-04; full TUI/runner/auth/Layout tasks below remain open.

- [ ] Establish the fresh runtime skeleton and resource-pack structure, organized by responsibility, node, and prompt files. See [skeleton grouping plan](implementation_plan_g.md).
- [ ] Build a Python TUI for starting the application and running unit, integration, and E2E tests. See [TUI grouping plan](implementation_plan_a.md).
- [ ] Add conditional infrastructure profiles for Langfuse, local upstream API/database prerequisites, and configurable host Nx UI startup. See [infrastructure and UI grouping plan](implementation_plan_h.md).
- [ ] Establish durable shared reporting-service ownership and one persistent accounting cache per computer, preserving fixed application ports and existing suite exclusion. See [shared resource coordination](implementation_plan_h2.md).
- [ ] Establish LLM provider connectivity, early authenticated model discovery, and token/quota tracking before the first integration or E2E runs. See [providers and usage grouping plan](implementation_plan_f.md).
- [ ] Establish LangFuse/OTEL configuration, local profiles, tracing and content scrubbing before the first Ok run. See [tracing grouping plan](implementation_plan_k.md).
- [ ] Add running-container inventory and an explicitly confirmed cleanup TUI mode for retained/crashed stacks. See [container cleanup grouping plan](implementation_plan_l.md).
- [ ] Start with a single final renderer LangGraph node instructed to say "Ok", without verifier/guard nodes for now, exposed through the existing frontend API contract, with example integration and human-accepted E2E tests. See [initial LangGraph grouping plan](implementation_plan_e.md).
- [ ] Establish declarative formats for configuration, LangGraph flow, and other executable descriptions, parsed programmatically at runtime and used to derive configuration documentation and flow diagrams. See [declarative formats](implementation_plan_b.md) and [ordered format/fixture conversion](implementation_plan_b1b.md).
- [ ] Start a README describing application and TUI usage, with configuration documentation derived from executable descriptions. See [README grouping plan](implementation_plan_c.md).
- [ ] Establish Allure reporting for integration and E2E tests before their first rewrite tests, excluding unit tests, with persistent results in S3 and an optional secondary results destination. See [Allure grouping plan](implementation_plan_d.md).
- [ ] Track UI API refinements, deployment-log integration, and manual prompt/instruction-edit hook enforcement without losing deferred requirements. See [engineering follow-up grouping plan](implementation_plan_i.md).
- [ ] Add conservative content scrubbing for logs, Langfuse, and traces, allowing content only with explicitly supplied `LOG_CONTENT=True`. See [content logging grouping plan](implementation_plan_j.md).

- [ ] Share generic unattended provider session ownership and operation-wide usage accounting across tests and explicitly unattended local runs. The provider owns token refresh and any persistence. See [unattended lifecycle grouping](implementation_plan_a7.md).

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
