# Complete-operation trace checkpoint

[Execution trace requirements](implementation_plan_k1a1.md)
[Remote ownership](implementation_plan_k1a1c.md)

## Supported mapping contract

This checkpoint uses GenAI conventions at
[revision 95aff9cdf605bbc7d7e8f7c4745a693bd0c4eea8](https://github.com/open-telemetry/semantic-conventions-genai/tree/95aff9cdf605bbc7d7e8f7c4745a693bd0c4eea8),
including its input/output message JSON schemas. Error policy follows
[semantic conventions v1.45.0](https://github.com/open-telemetry/semantic-conventions/blob/v1.45.0/docs/general/recording-errors.md).
The native observation mapper has contract version 1. Existing OTLP routes use
[Langfuse's documented observation attributes](https://langfuse.com/integrations/native/opentelemetry)
(reviewed November 3, 2026). Native JSON panels and normalized GenAI message JSON
are separate projections. SDK package versions do not pin semantic conventions.

| Placement | Fields | Admission and representation |
| --- | --- | --- |
| Resource | `service.name` | Existing fixed application identity |
| Request | `api_agent.outcome.version/state/cause_id`, `error.type` | Explicit typed final outcome; bounded cause vocabulary; UUID cause identity |
| Graph/component/node | Existing invocation/turn/scope/visit/attempt identities | Original journal timestamps and explicit same-runtime request parent |
| LLM | `gen_ai.operation.name`, request/response model, input/output token usage | Existing catalog identity authority; integer tokens; standard usage aggregated once on logical generation; physical counts use custom transport fields |
| Tool | `gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `api_agent.tool.instance` | Existing admitted named-tool lifecycle and credential authority |
| Native observation | `langfuse.observation.type/input/output` | Bounded projected JSON strings; I/O requires trace opt-in |
| LLM content | `gen_ai.input.messages`, `gen_ai.output.messages` | Normalized role/parts/text objects serialized as permitted JSON strings; unsupported native forms omitted |
| Retry child | Existing transport ID/index/outcome/status | Original physical attempt boundaries; failed attempt remains ERROR while recovered logical call succeeds |

Cancellation and client disconnect remain explicit `cancelled` outcomes with
unset SDK status. Timeout and genuine failure use ERROR. An ordinary HTTP 200
or returned dictionary does not establish application success: unclassified
non-chat requests remain `unknown`. The built-in demo promotes its typed tool
result after the selected tool contract completes; rendering cannot erase a
partial or rejected tool operation. A stricter later terminal failure supersedes
a partial result. Intermediate retry failures never report a final request error.
Ended request owners reject inherited late reports. Every production final-outcome
report requires an explicit selected tracing runtime; absent authority cannot
adopt an ambient request owned by another runtime. This applies to both executors,
application validation and the Studio completion/error/stream paths.

The adapter retains only bounded, destination-specific, already projected LLM
payloads until terminal accounting supplies the logical call. Physical attempt
identities establish correlation when capture and accounting call identities
differ. Credential exclusion runs again before SDK publication. Named tools use
one lifecycle span; a deterministic accounting receipt cannot create a duplicate.
Explicit runtime lifecycle admission precedes trace projection and buffers. Disabled,
not-yet-started and disposed exporters retain no new trace scopes, tools or native
payloads and issue no inactive-capacity diagnostics; disabled mode imports no SDK
and invokes no exporter factory. Scope, tool and deferred-payload saturation
counts each declined active observation.
A shared loss owner warns once with fixed safe text and exposes its count without
content permission. The open request records a conservative loss summary before
ending; exporter losses discovered later never mutate an ended request. An owned
1024-record admission budget wraps the SDK's public batch processor hook; records
rejected above that limit are counted before the SDK queue, without blocking
inference or relying on private queue internals. Failed loss-diagnostic logging
retains failure rather than being swallowed by optional tracing wrappers.

## Verification and remaining gates

- [ ] Independently review serialized request → parsed graph → named-tool ancestry,
  original timing and typed returned success/partial/failure for both destinations
  and all four content flag combinations.
- [ ] Independently review actual selected-provider physical retry, native LLM
  panels, normalized GenAI messages and exactly-once physical-attempt accounting.
- [ ] Independently review both parsed executors, stream failure/cancellation/
  disconnect and cleanup, late task isolation, mandatory credential exclusions,
  mapper rejection and nonfatal SDK failure diagnostics; run causal controls.
- [ ] Finish application/backend viewer qualification and complete joined gates.
- [ ] Deliver remote distributed parents, retained DEBUG exporter ownership,
  complete provider/response/finish/options/cache/reasoning fields, resource/log
  correlation and requested metrics/system signals. Unknown source fields must
  remain unavailable rather than being inferred from names or answer text.
- [ ] Deliver complete [resource-pack backend admission](implementation_plan_k1a2.md).
  A versioned, immutable native attribute mapper is a shared seam, not a claim
  that arbitrary backend discovery, dashboards or infrastructure are implemented.
