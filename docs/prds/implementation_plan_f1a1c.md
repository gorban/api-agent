# Ordered inference history for parsed flows

[Declarative responses](implementation_plan_f1a1.md)

## Requirements

- [x] Provide one shared, invocation-owned history implementation for both
  parsed-flow executors. Calls through runtime-provided `llm` and `complete`
  services retain separate histories per node and logical dialogue. Audit all
  included application and integration flows with retries; do not implement this
  only in a demo or provider. Document these service interfaces for native nodes.
- [x] Preserve each earlier request and accepted assistant message exactly.
  Append changed input and failure feedback in order on retry. Recognize an
  already accumulated native conversation without duplicating assistant calls
  or tool results. Preserve opaque continuation on its original message; never
  fabricate an assistant response, tool call or result for a failed request.
- [x] Keep planner, reviewer and individual tool-relevance dialogues separate.
  Isolate graphs, concurrent invocations, user turns, selected service owners,
  models, schemas, tools and generation settings. Serialize one dialogue's
  calls; cancellation must release queued ownership without dispatching a
  cancelled request. Explicit dialogue scopes support multiple roles in a node.
- [x] Keep the unchanged request prefix intact when adding retry inputs.
  Actual provider serialization, credential scrubbing and cache support remain
  authoritative; do not promise cache hits or change `LLM_CACHE_POLICY`.
  Retain the shared JSON-format instruction after a reported JSON/prompted-JSON
  call, using the same instruction builder as the provider adapter. Verify the
  actual transport message prefix rather than only the authored request.
  Preserve application pass budgets, transport retry ownership, accounting,
  request identities, deadlines and canonical GRAPH evidence.
- [x] Retain existing inference message/byte limits and a bounded invocation
  history budget. Reject overflow before further dispatch instead of silently
  truncating prior context. Histories are ephemeral and close with the graph
  invocation; they must not become another reporting payload or persistent store.

## Acceptance

- [x] Deterministic tests inspect actual second requests for both executors,
  application guard retries and native tool conversations. Verify exact prefixes,
  detailed failure feedback, preserved calls/results/continuations, no duplicated
  histories, isolation, cancellation, bounded overflow and unchanged call counts.
- [x] Exercise distinct included retrying application and test graphs. Record
  remaining provider-dependent live acceptance separately from offline proof.
- [x] Complete meaningful causal controls, exact applicable unit coverage,
  affected integrations, installed/relocated resources, boundary checks and
  independent external review before the human checkpoint.
- [x] Human-approved ordered planner and verifier retry histories, detailed error
  feedback and tool-message ordering without duplication. This approval does not
  establish cache hits; those require a supporting, configured provider.

## Verification

The shared runtime passes 22,881 unit tests with exact 100% line and branch
coverage across 336 measured files. Controlled requests cover both executors,
all included graph roots, separate planner/reviewer and tool-verifier histories,
concurrent and nested graph isolation, cancellation and bounded retention.
Actual-body prefix checks cover six included transport encoders with native and
prompted-JSON responses. Causal controls detect missing prefixes, authority
partitioning, native-history duplication, missing JSON instructions and lost
reviewer details. Both content flags retain independent credential-safe capture.

The normal integration runner passes the controlled useful-demo retry case and
real selected-provider shared-primary and independent-target tool cases.
Source, installed and relocated resources, workflow provenance without Git
metadata and independent external source review pass. These checks establish
ordered requests; actual provider cache hits remain a separate observation.
