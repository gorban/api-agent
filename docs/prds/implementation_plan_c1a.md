# Extension interfaces and behavioral expectations

[Integration-guide requirements](implementation_plan_c1.md)

For every extension, give a reason to replace it, the actual public interface,
factory/version and capability requirements, configuration selection, inputs and
outputs, lifecycle ownership, failure behavior and a minimal independently
authored example. Link authoritative source definitions and conformance tests;
distinguish required methods from optional capabilities. Do not present every
extension as a Python-file provider if it uses a different registration contract.

- [ ] **LLM provider:** document catalog/model-family/version/reasoning metadata,
  completion and optional streaming/structured/tool calls, authentication/session
  setup, normalized usage, errors and optional accounting/pricing/quota metadata.
  Explain omitted/unsupported capabilities, lazy credential setup, cancellation,
  timeout clipping, retained cleanup and credential-safe evidence. Select with
  `LLM_PROVIDER`; adjust allowed/default model patterns for the new families.
  Explain provider-specific cost cohorts and non-secret `cost_affecting` route,
  billing and guardrail settings; identical model IDs do not make provider prices
  interchangeable. Link the [Bedrock adapter plan](implementation_plan_f4.md).
  Make the implementor's obligations explicit: supply actual usage and validated
  rate/charge provenance, declare stable billing/region/guardrail-policy/version
  dimensions and identify request transformations affecting charged usage. The
  shared estimator cannot detect an undeclared server-side policy change or infer
  compatibility from a display name. Keep ancillary non-token charges and unknown
  overhead distinct from model tokens, preserve any unattributed total and never
  borrow another provider's rate card or calibration factor. Link the detailed
  [provider/profile qualification](implementation_plan_f3c.md) and describe how to
  prove separation using equal model IDs and token counts with different costs.
  Describe subscription versus API-key adapters only where an authorized upstream
  interface actually supports that mode; never silently switch funding routes.
- [ ] Use Anthropic as an endpoint-translation example, without claiming a
  bundled Anthropic adapter. Verify against its [official HTTP reference](https://platform.claude.com/docs/en/api/http):
  `POST /v1/messages` for messages and streaming, `GET /v1/models` and optional
  `GET /v1/models/{model_id}` for discovery, and optional
  `POST /v1/messages/count_tokens` for preflight counting. Explain request/stream,
  tool, cache-write/read and reasoning normalization rather than substituting an
  OpenAI endpoint. Its legacy `POST /v1/complete` is deprecated, not the preferred
  new integration. Link the [subscription/API distinction](https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-is-claude-api-usage-billed-separately-from-my-paid-claude-plan);
  any supported subscription integration needs its own verified interface and
  entitlements, not an assumption that chat subscription credentials fund API use.
- [ ] **UI provider:** explain bringing an existing application's chatbot as well
  as a dedicated frontend. Describe the versioned lifecycle, single configured
  build/serve command, supplied origin/port and backend/proxy configuration,
  readiness, signals and process/browser disposal. Cover fresh host prerequisites,
  optional indexed-user browser preparation, exact ready page/context handoff,
  headless test versus headed local/DEBUG behavior and credential-masked evidence.
  Link the actual chat/catalog/stream/heartbeat/cancel contract for UI authors;
  mark unavailable continuation features honestly. Select with `UI_PROVIDER`.
- [ ] **API provider and upstream extension:** distinguish tool transport from
  application HTTP serving, upstream authentication/identity/signing and optional
  managed local API/database prerequisites. Explain remote `API_ENDPOINT` versus
  explicit blank for supported local hosting, readiness and owned disposal.
  GraphQL is one included transport/example, not a required tool protocol. Show
  BYOT through workflow/tool contracts; do not equate all tool calls with an API
  provider. Document `API_PROVIDER` and `UPSTREAM_API_PROVIDER` only in the scopes
  actually declaring/consuming them. Link the owning infrastructure requirements
  for hosting capabilities not yet delivered.
- [ ] **Access and identity:** cover separately selectable `ACCESS_PROVIDER`
  non-inference discovery/usage, optional routes and pack-declared identity and
  credential roles. Distinguish these from an LLM provider and UI login; an access
  adapter cannot advertise inference merely to perform authentication checks.
- [ ] **Password manager:** document `cfg/password_managers.toml`, stable manager
  identifiers and actual CLI/reader interfaces, independently retrieved username,
  password and fresh TOTP, optional passkey capability and explicit unsupported
  failures. Explain `UNATTENDED_AUTH`, provider-declared roles and a shared
  zero-based `UNATTENDED_USER_INDEX` for roles consumed by an operation. Show how
  indexed accounts enable reproducible real-provider integration/E2E on CI/CD,
  while respecting spending/acceptance gates, signed-in helper prerequisites,
  manual-only challenges and noninteractive ambiguity. Never put secrets in
  source examples, logs, screenshots or command arguments.
- [ ] **Workflow, resources and tools:** describe selected graph declarations,
  deterministic and LLM nodes, prompts, schemas, bindings, nested resources and
  explicit replacements; link real extension/runtime contracts and the bundled
  GraphQL example. Preserve source fingerprinting, content logging and tool/error
  semantics. Separate current behavior from pending declarative-format migration.
- [ ] **Other integration surfaces:** include pack test-suite and adjacent-provider
  test declarations, production/dependency manifests, optional managed service
  fragments, tracing/export destinations and accounting metadata. Distinguish
  selectable providers from configuration-only seams; do not invent a tracing,
  tool or bootstrap factory simply to make the list uniform.
