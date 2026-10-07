# Declarative executable descriptions

## Checklist

- [ ] Define declarative formats for configuration and LangGraph flow, and identify other behavior that should use executable descriptions during planning.
  - [ ] Require a type for each configuration value and describe whether it is optional and whether it has a default. Use these declarations for the TUI/application type checks, missing-value handling, environment/CLI mapping, and generated configuration documentation.
- [ ] Parse these descriptions programmatically at runtime to configure the application and construct the executable LangGraph flow.
- [ ] Derive configuration documentation and flow diagrams from the same descriptions, avoiding separately maintained representations that can drift out of sync.

- [ ] Define a human-readable Layout file as the executable source of truth for graph structure, data bindings, retry groups, and prompt composition. Review the concrete syntax with the user.
  - [ ] Resolve named Python node and condition functions through a function registry and construct/compile LangGraph nodes and unconditional or conditional edges at runtime. Report unknown function references and invalid layouts before execution.
  - [ ] Declare input/output reference IDs. Bind an input to a matching output from an earlier executed node; where no previous output supplies it, require the runner to supply it (for example conversation history, user prompt, or UI context). Report missing required inputs. Preserve the distinction between tool results and user-provided content.
  - [ ] Declare sub-groupings with their own bounded loop behavior, condition function references, and attempt budgets. Support a planner/plan_verifier group with an initial attempt and one retry, and a later graphql_executor/graphql_verifier group with its own independent attempts. Define success and exhausted-attempt exits; group retries must not consume another group's budget. These verification loops are distinct from exception retry policies.
  - [ ] Include an ordered prompt section containing file links. Each entry applies unconditionally or has an optional named condition function deciding whether it applies. Apply included prompts in declaration order.
  - [ ] Support the special path variable `<resource_pack>` in prompt paths, resolving it to the selected resource pack directory. Use whatever is selected by `--resource-pack-dir`, or an included demo resource pack by default for standalone api-agent. Report unresolved variables and missing applicable prompt files.
- [ ] Generate the human-readable flow documentation/diagram from the same Layout loaded by the runner. The Layout describes orchestration; named function implementations remain Python code.
- [ ] Add targeted integration coverage for conditional routing, output-to-input and runner-provided bindings, independent group retries/exhaustion, ordered conditional prompts, and switching between demo and selected resource packs. Establish [Allure reporting](implementation_plan_d.md) before writing these tests.

Implementation reference: [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) provides programmatic nodes, conditional edges, graph state, and loops. The Layout format and loader are project features to build on that API.
