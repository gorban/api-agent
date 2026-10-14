# LangGraph Layout v1 — proposed example

## Definition

| Property | Value |
| --- | --- |
| Graph ID | `example_agent` |
| Configuration | [Typed timing fields](cfg/config.toml) |
| Registry contracts | [Functions and conditions](functions.md) |
| Reporting convention | [Component timing v1](reporting.md) |

## Components

| ID | Definition |
| --- | --- |
| timeout | [Shared timeout/progress wrapper](components/timeout.md) |

## Nodes

| ID | Function | Contract | Timeout instance |
| --- | --- | --- | --- |
| planner | `example.plan` | [Planning](functions.md#planning) | `planner_clock` |
| plan_verifier | `example.verify_plan` | [Plan verification](functions.md#verification) | `plan_verifier_clock` |
| graphql_executor | `example.execute_queries` | [Query execution](functions.md#query-execution) | `executor_clock` |
| graphql_verifier | `example.verify_queries` | [Query verification](functions.md#verification) | `query_verifier_clock` |
| renderer | `example.render` | [Rendering](functions.md#rendering) | `renderer_clock` |
| error | `example.report_error` | [Error output](functions.md#error-output) | `error_clock` |

## Groups

| ID | Members | Entry | Maximum attempts | Retry condition | Success target | Exhausted target | Timeout instance |
| --- | --- | --- | --- | --- | --- | --- | --- |
| planning | `planner, plan_verifier` | `planner` | `2` | `example.plan_retry` | `graphql_executor` | `error` | `planning_clock` |
| query | `graphql_executor, graphql_verifier` | `graphql_executor` | `2` | `example.query_retry` | `renderer` | `error` | `query_clock` |

## Edges

| From | To | When |
| --- | --- | --- |
| START | planner | always |
| planner | plan_verifier | always |
| plan_verifier | graphql_executor | `example.plan_accepted` |
| graphql_executor | graphql_verifier | always |
| graphql_verifier | renderer | `example.query_accepted` |
| renderer | END | always |
| error | END | always |

## Bindings

| Consumer | Input | Source | Category |
| --- | --- | --- | --- |
| planner | `request` | `runner.user_request` | user content |
| planner | `history` | `runner.conversation_history` | user-facing history |
| plan_verifier | `request` | `runner.user_request` | user content |
| plan_verifier | `plan` | `planner.plan` | proposed plan |
| graphql_executor | `plan` | `plan_verifier.verified_plan` | verified plan |
| graphql_verifier | `plan` | `plan_verifier.verified_plan` | verified plan |
| graphql_verifier | `evidence` | `graphql_executor.tool_evidence` | tool results |
| renderer | `request` | `runner.user_request` | user content |
| renderer | `evidence` | `graphql_verifier.verified_evidence` | verified tool results |
| error | `failure` | `runner.group_failure` | typed exhausted-group outcome |

## Prompts

| Node | File | When |
| --- | --- | --- |
| planner | [Planning instructions](prompts/planning.md) | always |
| plan_verifier | [Verification instructions](prompts/verification.md) | always |
| graphql_verifier | [Verification instructions](prompts/verification.md) | always |
| renderer | [Response instructions](prompts/rendering.md) | always |

## Tools

| Owner | Tool ID | Contract | Operation timeout instance |
| --- | --- | --- | --- |
| graphql_executor | `selected_api.query_graphql` | [Selected MCP-style API tool](functions.md#query-graphql-tool) | `graphql_call_clock` |

## Timeout instances

| ID | Scope | Component | Name | Config source | Field | Lifetime |
| --- | --- | --- | --- | --- | --- | --- |
| graph_clock | graph `example_agent` | timeout | Agent workflow | [Timing fields](cfg/config.toml) | `GRAPH_TIMEOUT_SECONDS` | one whole graph execution |
| planning_clock | group `planning` | timeout | Planning and verification | [Timing fields](cfg/config.toml) | `PLANNING_GROUP_TIMEOUT_SECONDS` | all planning attempts |
| planner_clock | node `planner` | timeout | Planner | [Timing fields](cfg/config.toml) | `NODE_TIMEOUT_SECONDS` | one node attempt |
| plan_verifier_clock | node `plan_verifier` | timeout | Plan verifier | [Timing fields](cfg/config.toml) | `NODE_TIMEOUT_SECONDS` | one node attempt |
| query_clock | group `query` | timeout | GraphQL execution and verification | [Timing fields](cfg/config.toml) | `GRAPHQL_GROUP_TIMEOUT_SECONDS` | all query-group attempts |
| executor_clock | node `graphql_executor` | timeout | GraphQL executor | - | - | one node attempt; inherit only |
| query_verifier_clock | node `graphql_verifier` | timeout | Query verifier | [Timing fields](cfg/config.toml) | `NODE_TIMEOUT_SECONDS` | one node attempt |
| renderer_clock | node `renderer` | timeout | Renderer | [Timing fields](cfg/config.toml) | `NODE_TIMEOUT_SECONDS` | one node attempt |
| error_clock | node `error` | timeout | Error output | - | - | one terminal node; inherit only |
| llm_call_clock | selected LLM calls within nodes | timeout | LLM call | [Timing fields](cfg/config.toml) | `LLM_CALL_TIMEOUT_SECONDS` | each actual provider call |
| graphql_call_clock | tool `selected_api.query_graphql` | timeout | GraphQL API call | [Timing fields](cfg/config.toml) | `GRAPHQL_CALL_TIMEOUT_SECONDS` | each actual tool call |

## Notes

This section is nonexecuting. Read [the example guide](README.md) for the proposed grammar and implementation status.

Group ownership supplies retry/exhaustion transitions: a rejected verifier result retries its group's entry only while its attempt budget permits; otherwise it targets `error`. Acceptance exits the group along the declared edge. Registered conditions are specified in [the catalog](functions.md#conditions); ambiguous declarations fail validation, and malformed runtime dispositions fail explicitly.

Group attempts include the initial attempt. The two groups have independent counters, and their clocks do not reset for verification retries. This example specifies no automatic exception retries. An actual migration must separately preserve its existing exception/timeout recovery and mutation-approval behavior.

Prompt rows apply in declaration order. A retry iteration binds the verifier to that iteration's most recent upstream output. An implementation must declare/validate output types and any retry feedback required by its real functions; those schemas are omitted from this structural example.

The executor can contain multiple model and tool calls. Its untimed node wrapper does not replace their individual allowances with a single LLM allowance. An integration with an existing derived node/batch deadline must bind and preserve that deadline explicitly; optional example group/graph fields must not erase it.
