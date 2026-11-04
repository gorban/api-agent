# Bedrock Runtime endpoints and model families

[Bedrock provider](implementation_plan_f4.md)

Support independently selected endpoint surfaces and inference formats. Default
to Runtime; preserve explicit Mantle selection and authentication ownership.
Messages API is out of scope.

The Runtime default applies consistently to provider declarations, direct provider
construction, configuration validation and regional-price applicability. Preserve explicit
endpoint, API, model, account and region settings; do not rewrite dotenv files.
The bundled full-ID default-model regex selects the OSS 20B Mantle ID or its
Runtime `-1:0` ID when present in the selected catalog. It must not admit other
OSS sizes, profile aliases or extra suffixes. Explicit model overrides remain authoritative.
Expose the shipped model regexes and optional US/global selectors in configuration
help. Preserve explicit routing scope and warn that geographic profiles can cost
more than corresponding supported global profiles; link current AWS model/source-Region
pricing in the [Bedrock guide](../bedrock.md). Never infer global routing from a bare ID.
Historical Mantle test profiles apply only when Mantle is selected. Runtime tests
retain the operator's effective application and test-specific settings.

Reject intrinsically invalid combinations before authentication or dispatch,
including Mantle with Converse. Session, access-key and API-key authentication
are supported with either endpoint; attended versus indexed credential selection
does not alter endpoint support. Do not probe permissions, balances or account
access to admit a configuration. AWS remains authoritative: propagate bounded,
credential-scrubbed upstream messages, error codes, HTTP status and request IDs
when available, preserving execution and usage without changing identity or route.

- [x] Add `BEDROCK_ENDPOINT=mantle|runtime`; retain `BEDROCK_API_MODE=responses|completions`
  and add `converse` for Runtime only. Validate combinations without authentication.
- [x] Derive discovery from the endpoint: Mantle `/v1/models`; Runtime paginated
  `ListFoundationModels` and `ListInferenceProfiles` through the AWS control plane.
  Preserve exact model/profile identifiers; discovery never proves inference access.
- [x] Project every valid Runtime model and profile identifier returned by discovery, including future IDs, without a model-based endpoint or API availability blacklist. The operator's model filter selects which IDs to use; AWS remains the authority on endpoint and feature availability. Keep exact identity and never retry on another route. Generic metadata must not invent limits or native structured support.
- [x] Apply bundled Mantle test-profile defaults only when the selected endpoint is Mantle. A Runtime selection retains the user’s exact API, model and test-specific settings; an inactive profile must still pass declaration validation.
- [x] Match the bundled OSS 20B default in either endpoint catalog, including Runtime Completions, using one full-ID regex; preserve explicit overrides and fail when neither exact ID is available.
- [x] Preserve selected credential/session-token identity for discovery and
  inference, lazy optional dependencies, bounded worker ownership, cancellation,
  parent deadlines and shared transport retries. Disable SDK inference retries.
- [x] Normalize text, client tools/results, stop reasons, usage and errors through
  existing contracts. Use existing content flags, credential authority, GRAPH/Allure
  capture and telemetry. Report unknown token subsets and prices as unknown.
  Native and OpenAI-compatible SDK errors retain bounded named upstream diagnostics
  only after credential-authority admission; parent tracebacks include those safe
  messages independently of content flags. Withhold unsafe or oversized diagnostics
  explicitly without changing the original error classification or retry semantics.
  Native Converse validation reports a bounded rule and schema path from a fixed
  vocabulary for malformed responses. The worker and parent preserve this diagnosis
  alongside valid usage and execution state, and display it in the safe error message
  regardless of content logging. Neither rejected values nor unknown field names enter
  diagnostics or tracebacks; unavailable detail remains unspecified.
- [x] Validate the implemented API request fields, caching and output-schema combinations
  before dispatch; preserve the shared declared output-mode fallback policy. Generic
  catalog controls describe the encoder, not verified model-side acceptance. Unsupported
  model controls remain AWS failures with credential-scrubbed diagnostics.
- [x] Test route/profile admission, pagination, identity isolation, codecs, tools,
  usage, errors and disposal, including Mantle regressions. Qualify source and
  installed/relocated artifacts, exact coverage, public boundary and reviews.
- [ ] Verify selected real Runtime OpenAI and Claude models with authorized account
  access; retain failed attempts and exact model/profile/region/API provenance.
- [ ] Human acceptance: select Runtime and a supported inference profile, populate
  Studio models and complete a user turn; inspect tools and content-enabled reporting.
- [x] Parse recognizable discovered OpenAI GPT identifiers into display family and
  version, including bare IDs such as `openai.gpt-5.6-luna` → `gpt-luna` / `5.6`.
  Preserve routing scopes and exact execution IDs. Display parsing must not grant
  native schema support, limits or other model capabilities.
- [ ] Human acceptance: Studio shows a bare GPT model's family and numeric version,
  with routing scopes separate and default selection choosing the newest matching version.

Converse may initially publish a validated buffered completion through the stream
interface; incremental ConverseStream delivery is separate from correctness of
the completed result. No unverified native schema capability is advertised. OpenAI-format
reasoning controls describe supported request encoding; AWS model support may vary.

Converse accepts well-formed plaintext `reasoningContent.reasoningText` alongside
a completed non-tool final answer. Reasoning is not appended to that answer.
Signed reasoning on tool or unfinished replies still requires scoped native
continuation support and must fail explicitly rather than discard replay state.
Opaque/redacted reasoning remains unsupported. Preserve usage and uncertain
execution on these failures; never treat them as pre-inference rejections.
Content-enabled request evidence retains the actual typed `inferenceConfig.maxTokens`
value, subject to the same credential authority as other provider evidence.

Public contracts: [AWS endpoint comparison](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html),
[discovery](https://docs.aws.amazon.com/bedrock/latest/userguide/models-get-info.html),
[Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html),
[Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).


Runtime model visibility does not guarantee that the selected endpoint, API, region or feature is supported. Operators should filter models according to AWS documentation. The [Responses native continuation follow-up](implementation_plan_f4o1.md) owns encrypted assistant-state replay for client tool results, including owner/authentication binding and report exclusion. Unqualified continuation fails safely rather than being silently discarded.


[Capability discovery and fallback](implementation_plan_f1a2.md) owns configurable
capability caching, optional accounted preflight probes, and fallback after a
confirmed pre-inference rejection. This endpoint work does not add hidden probes
or automatic inference route changes.

Offline qualification passed 22,464 unit tests with exact 100% line and branch
coverage. Source distributions, installed and relocated packages, workflow
resources and independent/external source reviews passed. Real Runtime access
and the Studio acceptance above remain separate checks.

- [x] Complete [Claude/GPT display parsing and Studio target labels](implementation_plan_f4o2.md), preserving full IDs and explicit inference routing.

[Runtime Completions reasoning carriers](implementation_plan_f4o3.md) tracks qualification of in-band reasoning and final-answer extraction. A successful request does not by itself close that application-output gate.

- [ ] Deliver [Claude reasoning controls and budget-backed effort](implementation_plan_f4o4.md), using provider-owned version/API qualification and meaningful Studio choices.
- [ ] Repair [SDK worker-group cleanup after early rejection](implementation_plan_f4o5.md), retaining typed errors and retryable helper ownership.
