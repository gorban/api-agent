# Proposed function registry contracts

These are illustrative contracts for future Python registrations, not implemented node functions. A real Layout must resolve them to callable implementations and validate their input/output types. The only existing implementation linked by this example is the common timeout/progress handler.

## Planning

`example.plan(request, history)` returns a proposed structured `plan`. It uses the selected LLM provider and the linked planning prompt. Each provider invocation receives the declared operation wrapper inside the node's scope.

## Verification

`example.verify_plan(request, plan)` returns acceptance and `verified_plan` when accepted. `example.verify_queries(plan, evidence)` returns acceptance and `verified_evidence` when accepted. Rejected results contain typed feedback for their owning group; schemas/feedback bindings must be defined with the real implementation.

Verifier decisions are distinct from exception/timeout recovery. Verification must not treat missing evidence or an incomplete call as success.

## Query execution

`example.execute_queries(plan)` calls the selected API tool and returns structured `tool_evidence`. Model-assisted query construction or repair uses `llm_call_clock`; each actual API request uses `graphql_call_clock`. A multi-call batch remains a separate scope even if the same node orchestrates both operations.

The real callable owns the operation/request limits and retry semantics. This structural example does not silently add API repair attempts or change mutation behavior.

## Query GraphQL tool

`selected_api.query_graphql(request, variables)` resolves to the API provider's advertised, registered MCP-style tool. Provider-owned transport/authentication/protocol details stay in that provider. The source viewer should link to the selected implementation once it exists.

Keep returned data/errors as typed tool evidence, separate from user input and LLM-authored commentary. Calls and any repairs use the same instrumentation boundary and preserve their actual attempt/call identity.

## Rendering

`example.render(request, evidence)` uses the selected LLM provider and linked rendering prompt to return a response grounded in verified evidence. This example adds no new renderer/verifier behavior to any existing integration.

## Error output

`example.report_error(failure)` returns the exhausted group's typed terminal outcome. It does not silently retry, change authorization, or fabricate results.

## Conditions

| Registry ID | Contract |
| --- | --- |
| `example.plan_accepted` | True when the current plan-verifier output accepts the plan |
| `example.plan_retry` | True when the current plan-verifier output rejects the plan; group budget decides retry versus exhaustion |
| `example.query_accepted` | True when the current query-verifier output accepts the evidence |
| `example.query_retry` | True when the current query-verifier output rejects the evidence; group budget decides retry versus exhaustion |

Conditions resolve to named Python functions. Exactly one accepted/rejected disposition must exist at each verifier boundary; malformed dispositions fail validation/execution with typed diagnostics rather than guessing a route. Conditional prompts can use the same registry mechanism when real behavior requires them.
