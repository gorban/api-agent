# Declarative LLM output modes and provider fallback

[Bounded inference contracts](implementation_plan_f1a.md) · [Workflow grammar](implementation_plan_b1.md) · [Shared adapter requirements](implementation_plan_f1.md)

Prefer native schema-constrained generation when an LLM must produce structured
data. A linked node output schema is a local contract; its presence alone does
not establish that the provider enforced it during generation. This follow-through
joins declarations, shared requests, provider adaptation and recorded evidence.

## Existing declaration table

- [x] Extend the existing Functions table, retaining ID, Registry, Input and
  Output and their schema links. Add an output-mode enum and an allow/reject
  fallback policy there, rather than introducing another LLM-call table.
  The extended grammar accepts exactly `ID | Registry | Input | Output |
  Response schema | Output mode | Fallback`; the existing four-column shape
  retains its behavior. This table extension is independent of document carrier
  versions. Extended LLM rows require explicit mode and fallback values; JSON
  modes require one linked response schema and text requires `-`. Deterministic
  rows require `-` in all three added cells. Partial or reordered headers reject.
  Existing declarations never change inference behavior merely because they have
  an Output schema.
- [x] Distinguish `structured` (native response-schema enforcement),
  `prompted_json` (ordinary text requested as JSON and parsed locally), `json`
  (provider JSON mode without schema enforcement), and `text` (ordinary generated
  text). Keep JSON mode distinct from prompted JSON. These are generation
  mechanisms, independent of whether the HTTP response serializes JSON inside
  one text field. Do not advertise a mode until its actual adapter path exists.
- [x] Prefer `structured` with fallback `reject` for new structured decisions.
  Reject unavailable required modes before inference. Fallback `allow` permits
  an explicitly documented weaker path, preferring available native enforcement,
  then JSON mode, then prompted JSON where applicable. Honor explicitly selected
  weaker modes; never imply they received native enforcement. No fallback may
  change the selected provider/account, use an unapproved route or replay a
  possibly completed request automatically.

Executable extension of an existing structured producer:

```markdown
| ID | Registry | Input | Output | Response schema | Output mode | Fallback |
| --- | --- | --- | --- | --- | --- | --- |
| first_draft | produce | [input](../../demo/contrived/schemas/producer.json) | [output](../../demo/contrived/schemas/draft.json) | [response](../../demo/contrived/schemas/draft.json) | structured | reject |
```

Example links above resolve from this document. Executable declarations retain
their own containing-document-relative links.

## Node contracts and generated values

- [x] Keep Input/Output as the complete node contracts, including deterministic
  nodes and nodes performing multiple provider calls. Specify an unambiguous
  generated-response binding when the LLM supplies only part of a node's result.
  `context.response_request(messages, tools=(), generation=None, strict=True)`
  explicitly builds requests using the node's one declared generated-response
  schema and policy. Repeated helper calls share that schema; a Registry ID never
  implies one call. Native code assembles whole or nested results and metadata;
  there is no automatic output placement. Legacy internal calls remain visible
  migration exceptions and are not rewritten implicitly. Response schema links
  participate in captured definition identity, relocation and viewer references.
  Generated schemas may describe object, array or scalar JSON roots; complete
  node Input/Output contracts remain objects. Provider preflight decides which
  roots its native or JSON modes support, with only explicitly permitted fallback.
- [x] Send only the generated-response schema to the LLM. Runtime-owned usage,
  selected/reported identities, status and billing remain independently assembled
  metadata, not fields the LLM is asked to invent. Validate the complete returned
  node object against its declared Output schema as well.
- [x] Preserve original schema semantics and the existing explicit `strict=False`
  compatibility contract. Do not silently add defaults, replace omission with
  null, remove constraints or label non-strict hints as strict enforcement.
  If an adapter uses a supported schema projection, make that policy explicit,
  retain the original contract and validate against it locally.

## Provider-owned adaptation

- [x] Resolve support for the actual provider, selected family/version, API,
  endpoint/profile, schema features and reasoning/tool combination. Reasoning
  support and structured-output support are independent. Provider-wide feature
  advertisement or API-compatible request spelling is insufficient evidence of
  native enforcement on every serving route. Unknown capability is not support.
- [x] Let each adapter map native schema requests to its documented API fields.
  For prompted JSON, let the adapter dynamically add schema/format instructions
  through the supported message arrangement. Preserve authored prompt content and
  relative ordering; retain source-role provenance when mapping supported API
  roles, and distinguish adapter-added instructions from authored content.
  Do not require provider-specific formatting prompts in workflow documents.
- [x] For JSON-producing modes, validate generated JSON locally against the
  original schema before accepting success, regardless of enforcement mechanism.
  Text mode retains its corresponding value contract. Handle refusals, truncated
  responses and malformed output explicitly. Any repair/retry consumes existing
  declared attempt/deadline budgets, retains failed evidence and accounts for
  every actual call; do not introduce hidden unbounded repairs.
- [x] Carry requested mode, fallback permission, actual mechanism, relevant schema
  identity/projection and fallback reason through shared requests/IPC and existing
  GRAPH evidence. Capture the effective request, including adapter-added prompts,
  through the existing canonical payload capture with independent LOG_CONTENT
  and TRACELOOP_TRACE_CONTENT controls. Keep credentials redacted in every mode;
  metadata must not leak confidential schema or prompt content. Evidence `strict`
  records the requested schema strictness, independently of `actual_mode`. A true
  flag with JSON mode or prompted JSON describes original local validation; it
  does not claim native enforcement. Non-strict native hints are never presented
  as strict enforcement.

## Verification and acceptance

- [x] Audit included structured planner/reviewer calls and migrate eligible
  prompt-only JSON calls to declared native requests. Preserve intentionally
  textual responses and explain remaining explicit compatibility exceptions.
- [x] Cover supported native generation, JSON mode, prompted JSON, rejected and
  allowed fallback, reasoning on/off, API differences, incompatible schemas and
  endpoint-specific support. Exercise actual parser-to-provider request mapping,
  local validation, generated-field binding, IPC, accounting and privacy with
  controlled adapters.
- [x] Use causal controls for dropped schemas, unauthorized downgrade, missing
  adapter instructions, unsupported-route admission, skipped validation and lost
  actual-mode evidence. Complete applicable exact unit coverage and independent
  then external source review. Current source verification passed 19,433 unit
  tests with exact 100% line and branch coverage. Live qualification, if needed
  for changed request semantics, requires its separately recorded authorization.
- [x] Complete current packaged/relocated artifact verification and final delivery
  review; source review alone does not close these gates.
- [ ] Human-review declaration readability and native versus permitted fallback
  evidence. Human acceptance remains unrecorded until explicitly reviewed.

## Included-call migration audit

- Contrived draft producers use declared `structured`/`reject` response contracts.
- The useful demo planner uses its original open `arguments` object contract with
  `structured`/`allow`; unsupported strict schemas require an explicitly recorded
  weaker mechanism and original-schema validation, never a silently closed schema.
- The useful demo reviewer declares its four generated Boolean verdicts separately
  from the complete `accepted`/`feedback` node Output. It uses `structured`/`reject`.
- The local chat application and useful demo renderer remain intentionally textual.
  Their original runtime metadata is assembled separately from generated text.
- Tool conversations retain explicit native requests: conversational rounds and
  relevance checks require different contracts within one native owner. They are
  not automatically bound to a single node response schema. Existing explicit
  non-strict tool schemas remain unchanged.

## Public documentation basis

- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Claude structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
- [OpenRouter endpoint-specific structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs)

Recheck applicable provider documentation during implementation; these links do
not establish access or feature support for an unverified deployment.
