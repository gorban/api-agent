# Bedrock operator guide and remaining qualification

[Converse requirements](implementation_plan_f4o4a.md) ·
[Operator guide](../bedrock.md) · [Response admission](implementation_plan_f1a4.md)

## Requirements

- [x] Link a dedicated operator guide from README. Start with Review configuration,
  the exact Bedrock factory selector, and clearing a saved LLM_PROVIDER assignment
  through Clear .env configuration before replacing it. Explain explicit override
  precedence, conditional settings and authentication without collecting secrets.
- [x] Separate AWS model/API support, current adapter admission, controlled tests
  and live evidence. Distinguish tool argument schemas from answer schemas,
  omitted effort from Off, buffered progress from native deltas, and an admitted
  request from permission or successful inference. Reconcile stale replay guidance.
- [x] Preserve optional client-tool type labels through native stream collection,
  decoding and signed continuation. Reject server-side and unknown tool types
  without silently converting their execution semantics. Prove valid replay and
  causal rejection before publishing the repair.
- [x] Verify changed links, applicable exact unit coverage, package inclusion,
  exact public boundary and external
  source review. Keep human setup/Studio acceptance separate from technical checks.

## Existing bounded live proofs

GPT-OSS 20B request and low-effort two-turn replay passed on Runtime Converse.
Sonnet 4.5 low-budget two-turn replay and Studio native-stream request passed.
Opus 4.5 hybrid low-effort two-turn and low/high-effort named-tool workflows
also passed.
A separate GPT-6 Luna low-effort SDK request was accepted. Those observations do
not establish all efforts, schemas, tools, profiles or visible reasoning carriers.
Controlled tests cover binary/signature replay, tool ordering, schema admission,
error handling and credential protection; they do not prove upstream acceptance.

Manual Studio checks passed high-effort Converse Sonnet 4.5/5.5 and Haiku 5.5,
and selected GPT reasoning calls on Responses/Converse. Exact execution IDs and
all tool/schema combinations were not exhaustively recorded. Sonnet Responses
rejections establish the observed unsupported pair, not a credential failure.

## Remaining qualification

- [x] Opus 4.5 hybrid native effort plus manual thinking budget and beta opt-in:
  ordinary answer/replay first, then tools/results and low/high choices. Retain
  request control metadata and distinguish absent continuation from replay proof.
  Runtime US-profile session checks passed ordinary low-effort replay and
  low/high-effort named-tool workflows after preserving the observed optional
  client-tool marker. Strict tools, answer schemas and other profiles remain
  separate qualification.
- [ ] Adaptive Claude versions: actual effort admission, low/high answers and
  tool-result continuation with signed/redacted blocks. Test specific supported
  IDs without deriving another version's capability from a family label. An
  automated Sonnet 5.5 replay check was account-denied; manual high-effort Studio
  acceptance passed elsewhere. The broader adaptive/tool matrix remains open.
- [ ] GPT Runtime Converse: Luna/Terra/Sol version matrix with low/high, ordinary
  and strict tools, actual tool-result continuation, and private opaque state.
- [ ] Combined native answer schema with explicit effort and/or tools on Converse:
  probe supported exact routes independently before changing conservative
  fallback/reject admission. Keep strict tool arguments and answer schema distinct.
- [ ] Reconcile streamed Responses JSON Schema qualification for exact US Luna
  5.6/6 with current AWS model cards describing nonstreaming JSON Schema. Retain
  actual stream/terminal/schema evidence; do not broaden from a text-only pass.
- [ ] Broader Responses matrix: medium effort, strict tools plus answer schema,
  other GPT versions and regional profiles. Preserve existing conservative policy
  until qualified; never substitute another model, endpoint, API or account.
- [ ] Haiku 4.5 high-effort demo planner: reproduce its original-schema output
  rejection, retain sanitized field/path diagnostics and completion/fallback
  evidence, and distinguish malformed/truncated JSON from API/control rejection.
  A focused high-effort named-tool test completed both tool reads and reached
  final verification, then failed raw-JSON parsing. Preserve this partial proof
  without calling the suite a pass. Preserve the original schema; any correction
  retry must follow declared flow
  policy and append-only feedback/history contracts.
- [ ] Fresh provider defaults: qualify an explicit compatible endpoint/API/model
  tuple. The current Runtime/Responses defaults and OSS model pattern are separate
  declarations; Runtime OSS Responses is not an AWS-supported tuple. Resolve
  default policy explicitly rather than treating discovery as compatibility.
- [ ] Improve early native Converse reasoning delivery separately from the
  existing bounded collector; preserve validation, credential protection,
  cancellation and continuation consistency. Current display is buffered.

Use normal selected-provider integration/E2E paths for live checks when possible.
Record an unavailable account/model as access-limited, not as an adapter pass or
permission to change identity. Preserve normal reporting, cost accounting and
independent review. Runtime discovery alone cannot qualify inference.

Synthetic strict-function output adaptation, optional preflight probes,
configurable capability caches and newer-version candidate ordering remain in
[capability discovery](implementation_plan_f1a2.md).

- [ ] Human acceptance: follow the guide to switch providers in the TUI, select a
  compatible model/API, inspect reasoning and tool evidence, and verify that the
  support matrix accurately explains the observed fallback or rejection.
