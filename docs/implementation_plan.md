# api-agent checklist

## Testing during implementation

Write deterministic unit tests covering each task's requirements as it is implemented, preferably red-to-green where possible. Before committing code changes, require all applicable unit tests to pass with 100% line and branch coverage, as required by AGENTS.md. Use focused mutation checks for rejection assertions that could pass for the wrong reason.

The TUI enforces exact 100% line/branch coverage for All tests; partial selections report coverage informationally. CI must enforce the full applicable unit suites and coverage gate before PR merges. Unit tests are excluded from Allure. Reverify coverage as features land and again before final cleanup. The final E2E acceptance pass-rate gate remains a separate task, with its threshold and measurement rules to be established with the user.

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

- [ ] Share generic unattended provider session ownership and operation-wide usage accounting across tests and explicitly unattended local runs. The provider owns token refresh and any persistence. See [unattended lifecycle grouping](implementation_plan_a7.md).

## Current provider architecture

- [ ] Complete [consumer contract hardening](implementation_plan_m3.md) before freezing the version-1 surface or starting concrete adapter/consumer lanes. Existing factory/session primitives remain completed; normalized LLM data, activation, streams, operational errors and graph-wide cleanup remain prerequisites.
- [ ] Build the early [included personal GPT provider](implementation_plan_f2.md) through the same replaceable LLM interface. Public demo UI/Nx apps and demo API servers/providers are later work; separately owned external pack providers can exercise generic core during early live acceptance. Synthetic fixtures prove interchangeability without a live demo backend.
- [ ] Complete generic factory, configuration, capability, route and lifecycle composition. See [shared provider contracts](implementation_plan_m.md).
- [ ] Build the included generic GraphQL provider with a separately selected upstream extension, and prove an outside-path API replacement. See [GraphQL provider](implementation_plan_m1.md).
- [ ] Restore upstream authentication/validation through an external API extension and LLM connectivity through the included or another selected adapter. See [API extension requirements](implementation_plan_m2.md) and [LLM adapter requirements](implementation_plan_f1.md). Keep system-specific requirements in their owners; non-inference access adapters are independent of the selected GPT provider.

Follow the [pluggable API and LLM provider plan](provider_architecture.md) for provider configuration, route extensions, authentication and browser lifetimes.
