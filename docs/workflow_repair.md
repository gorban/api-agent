# Repairing generated candidates

An existing guarded component can repair a deterministic candidate rejection
before its semantic guard runs. Native validation raises an explicit
`CandidateCorrection` from `api_agent.workflows.execution` with JSON feedback:

```python
if len(candidate['answer']) > 8192:
    raise CandidateCorrection({
        'reason': 'Answer exceeds its character limit',
        'rule': 'maxLength', 'path': '$.answer',
        'limit': 8192, 'actual': len(candidate['answer']),
        'instruction': 'Shorten the answer while preserving the request.'
    }, rejected=candidate)
```

Declare representable constraints in the response/output schema as well. A
definitive generated-response schema rejection is candidate data to repair;
unavailable validation is an operational failure. Do not convert arbitrary
exceptions into correction requests.

Feedback is untrusted content. Supply concise validation facts, not exception
dumps, schema fragments or secret-bearing expected values. Automatic schema
feedback projects rule names and numeric limits/counts instead of copying
`const`, `enum`, patterns or other schema payloads. The runtime borrows the
invocation's credential authority even when tracing/content reporting is off.
An explicit empty authority is suitable only when the caller has no credentials
to enroll; it cannot discover credentials hidden inside arbitrary native data.

The runtime retries the producing node, preserving completed prefix work. It
appends credential-safe feedback to that node's actual inference history after
the rejected assistant reply, including its opaque continuation. Native code
can inspect `context.correction` and `context.rejected`. Feedback does not require
an authored `FEEDBACK` binding. An ambiguous node with multiple independent
dialogues cannot guess which dialogue produced a rejected native value.

Shared provider validation raises `CandidateOutputError` only for a definitive
rejection of a bounded, completed response against its original response schema.
The private worker error contract carries the actual response, including opaque
continuation; the caller validates it against the originating request before
repair. This result is transient correction state, never an accepted output or
ordinary exception-log payload. Provider authors should use the shared result
validator rather than classify arbitrary provider errors as candidate defects.

Deterministic repairs and semantic guard rejections consume the same component
pass limit and inherited deadline. `context.attempt` counts consumed passes;
`context.guard_attempt` distinguishes semantic component passes, and
`context.correction_attempt` counts local node repairs. `PREVIOUS` contains only
accepted prior-pass outputs, never an invalid candidate.

Tool dispatch is a replay boundary. Runtime-owned tools and legacy `ToolGate`
owners record dispatch on the active native node before awaiting execution. An
`operations@1` call is recorded only when its operation is not declared
[idempotent](tools.md#implementation-declared-operations); correction and replay
stay available after a step whose every call was to an idempotent operation.
Native code must call `context.mark_effect()` before any
other side effect that cannot safely repeat. A component cannot restart after
such dispatch; a later independent producer may still repair its own output
without repeating the earlier operation. The native tool-conversation helper
uses its existing round limit and actual call/result ledger for remaining-work
corrections.

Argument batches are prepared before dispatch. A proposal containing invalid
tool arguments or an undeclared tool cannot be replayed as an admitted assistant
message under the inference contract. It receives rejection feedback without
invented tool results; valid earlier messages and completed results remain
unchanged. Schema-valid proposals rejected by preflight or relevance can retain
their exact assistant message with explicit not-executed failure receipts.

Configuration and immutable input errors, unavailable validators, authentication,
cancellation, deadlines and uncertain execution remain terminal. One-pass flows
and stateful cycles acquire no implicit retry budget. Cross-node input failures
do not guess an upstream producer. Put candidate constraints at their producing
response/output boundary or use an authored semantic guard with explicit input
bindings. See [requirements and acceptance](prds/implementation_plan_f1a1d.md).

Run the controlled length-repair proof from the repository root:

```sh
./launch.sh --tui-mode integration --start --no-std-in api-agent::tests/integration/test_useful_demo_graph.py::test_selected_graph_repairs_deterministic_length_before_review_or_tool_execution
```

It rejects an 8,666-character planner answer, checks that the next request
contains the 8,192 limit and actual length after the original assistant reply,
and verifies that review and tool execution occur only after correction.
