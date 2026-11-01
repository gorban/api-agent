# Qualified Runtime response-policy combinations

[Response declarations](implementation_plan_f1a1.md) · [Future capability discovery](implementation_plan_f1a2.md)

## Requirements

- [x] Admit native JSON-schema response policies on Runtime Responses for exact `us.openai.gpt-5.6-luna` and `us.openai.gpt-6-luna` selections with omitted, low or high reasoning, both streamed and nonstreamed. Preserve selected identity, routing, generation controls, strict original schema and local result validation. Do not broaden other models, routing prefixes, endpoints, APIs or reasoning choices from display-family parsing.
- [x] Qualify the combined native-schema/non-strict-tool path and tool-result continuation separately. Retain conservative admission of unqualified combinations, including strict tools combined with a response schema. Exercise existing strict-function argument validation independently; ordinary tool calls are not a synthetic structured-result adapter.
- [x] Remove the corresponding redundant streaming preflight restriction only for the qualified combinations. Explicit legacy structured requests and declared response policies must follow the same streaming admission boundary. Preserve failure/cancellation evidence and usage without hidden inference retries.
- [x] Prove the included application's real reviewer schema uses native output and its planner's open nested object uses explicitly allowed prompted JSON. Never close the planner object, change generated bindings or weaken original-schema validation to obtain native admission. Invalid generated values fail before rendering or tool execution.
- [x] Expose existing admitted response metadata in local call diagnostics: requested/actual mode, fallback policy/reason, and a clear output-mechanism label. Keep content flags independent and credentials/payloads excluded. Reuse canonical accounting and capture; do not add duplicate payload storage or a second call receipt. If credential redaction changes optional response metadata, retain the call with an explicit unavailable notice and keep its journal valid. Never restore redacted labels.

## Acceptance

- [x] Controlled provider/factory and streaming tests cover both exact selections, reasoning choices, native payloads, original-schema rejection, adjacent unqualified routes, tool arguments/continuation and effective request capture.
- [x] Run the real selected application with controlled provider responses; verify planner fallback, native reviewer and textual renderer at low/high effort. Inspect metadata through the actual terminal projection with content disabled/enabled.
- [x] Complete meaningful causal controls, applicable exact unit line/branch coverage, installed/relocated resources, workflow provenance, public-boundary checks and independent/external review.
- [ ] Human acceptance: repeat a complete Run local turn with each selected version and inspect native reviewer versus planner fallback in call diagnostics.

These rules are provider-owned qualification, not dynamic discovery, a learned capability cache or permission to infer support for a newer version. Native strict function-result adaptation, optional probes, expiring observations and inherited candidate ordering remain in the linked capability task. Public contract basis: [Luna 5.6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html), [Luna 6](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html), and [Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html). Provider documentation and bounded qualification do not guarantee access or every schema on every account.
