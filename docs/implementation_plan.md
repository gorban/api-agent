# api-agent checklist

## Testing during implementation

Write deterministic unit tests covering each task's requirements as it is implemented, preferably red-to-green where possible: demonstrate the missing behavior with a failing test, then implement it and make the test pass. Maintain a 100% unit-test pass rate for every commit. Do not pursue or require 100% code coverage at each implementation step; coverage completion is an explicit later task, before final documentation archival and cleanup.

The TUI enforces exact 100% line/branch coverage for All tests; partial selections report coverage informationally. Before PR merges, CI must require the full applicable unit suites to pass with exact 100% production coverage; see the [shared CI gate task](implementation_plan_i.md). Unit tests are excluded from Allure. During implementation, that coverage result identifies remaining work and does not block committing a step whose unit tests all pass. Use targeted tests to verify the current requirements. The final E2E acceptance pass-rate gate is also a separate root-plan task, with its threshold and measurement rules still to be established with the user.

## Checklist

- [x] Complete the reviewed Segment 1A foundation: versioned typed config/pack declarations, direct source launchers, hierarchical overrides, concise terminal help, and the stale-worktree gate. Human checkpoint A accepted on 2026-10-04; full TUI/runner/auth/Layout tasks below remain open.

- [ ] Establish the fresh runtime skeleton and resource-pack structure, organized by responsibility, node, and prompt files. See [skeleton grouping plan](implementation_plan_g.md).
- [ ] Build a Python TUI for starting the application and running unit, integration, and E2E tests. See [TUI grouping plan](implementation_plan_a.md).
- [ ] Add conditional infrastructure profiles for Langfuse, local upstream API/database prerequisites, and configurable host Nx UI startup. See [infrastructure and UI grouping plan](implementation_plan_h.md).
- [ ] Establish LLM provider connectivity, early authenticated model discovery, and token/quota tracking before the first integration or E2E runs. See [providers and usage grouping plan](implementation_plan_f.md).
- [ ] Establish LangFuse/OTEL configuration, local profiles, tracing and content scrubbing before the first Ok run. See [tracing grouping plan](implementation_plan_k.md).
- [ ] Add running-container inventory and an explicitly confirmed cleanup TUI mode for retained/crashed stacks. See [container cleanup grouping plan](implementation_plan_l.md).
- [ ] Start with a single final renderer LangGraph node instructed to say "Ok", without verifier/guard nodes for now, exposed through the existing frontend API contract, with example integration and human-accepted E2E tests. See [initial LangGraph grouping plan](implementation_plan_e.md).
- [ ] Establish declarative formats for configuration, LangGraph flow, and other executable descriptions, parsed programmatically at runtime and used to derive configuration documentation and flow diagrams. See [declarative formats grouping plan](implementation_plan_b.md).
- [ ] Start a README describing application and TUI usage, with configuration documentation derived from executable descriptions. See [README grouping plan](implementation_plan_c.md).
- [ ] Establish Allure reporting for integration and E2E tests before their first rewrite tests, excluding unit tests, with persistent results in S3 and an optional secondary results destination. See [Allure grouping plan](implementation_plan_d.md).
- [ ] Track UI API refinements, deployment-log integration, and manual prompt/instruction-edit hook enforcement without losing deferred requirements. See [engineering follow-up grouping plan](implementation_plan_i.md).
- [ ] Add conservative content scrubbing for logs, Langfuse, and traces, allowing content only with explicitly supplied `LOG_CONTENT=True`. See [content logging grouping plan](implementation_plan_j.md).
