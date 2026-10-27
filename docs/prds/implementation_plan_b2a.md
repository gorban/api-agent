# B2a — Layout, runtime and inference contracts

## Requirements

- [x] B2.1 Parse Layout v1 Markdown structurally: nested ordered Flow lists,
  sequential 1..99 numbering, four-column markers/four-space indentation and
  fixed component/function/binding/edge/prompt/timing tables. Unknown or
  inconsistent declarations fail with source locations before inference.
- [x] B2.2 Compile LangGraph graphs/subgraphs with registered deterministic/LLM
  functions, typed input/output contracts, deterministic conditional routing,
  declaration-relative links and selected-pack prompt substitution.
- [x] B2.3 Own independent component pass budgets and structured guard feedback.
  Carry immutable attempt ancestry; retries preserve enclosing deadlines and
  stable prompt prefixes. Format violations remain fatal contract failures.
- [x] B2.4 Expose validation, diagram/source-index generation, optional pack
  workflow declarations and reusable execution without a graphical UI.
- [x] B2.5 Carry richer inference requests/results through owned GPT and shared
  integration boundaries. Preserve text compatibility, privacy, usage, selected
  authentication, cancellation and disposal. Unsupported capabilities fail explicitly.

Follow-up declaration stability requirements and repair acceptance are in
[B2a1](implementation_plan_b2a1.md).

## Acceptance

- [x] Applicable unit tests pass with 100% line and branch coverage.
- [x] Focused mutations establish that intended rejection checks matter.
- [x] Independent adversarial review reports no unresolved defect.
- [x] Installed artifacts preserve declaration links and executable provenance.

## Requirement-to-test mapping

| Requirement | Deterministic evidence |
| --- | --- |
| B2.1 | `test_workflow_layout.py`: grammar, indentation, schema and link rejection |
| B2.2/B2.3 | `test_workflow_execution.py`, `test_workflow_runtime.py`: compiled routing, nested passes, feedback, deadlines and cancellation |
| B2.4 | Layout CLI tests and `test_pack_workflow.py`: selected-pack joins, CWD independence and installed paths |
| B2.5 | `test_rich_inference.py`, `test_rich_setup_transport.py`, `test_gpt_inference.py`: native structured/tools, encrypted continuation, real synthetic IPC/HTTP and owned cancellation |

Rich Responses tool continuation retains bounded encrypted provider state across
IPC and excludes it from content reports. Native schemas use a conservative
closed subset with external retrieval disabled. Cached OpenRouter Responses
continuations ending in a tool-output item fail explicitly as unsupported;
ordinary text and supported Chat Completions calls retain their behavior.

- [ ] Complete the separate future
  [B2a2 — Nested runtime interoperability](implementation_plan_b2a2.md) contract.
  Earlier delivered requirements and acceptance above retain their recorded status.
