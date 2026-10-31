# Bedrock Runtime endpoints and model families

[Bedrock provider](implementation_plan_f4.md)

Support independently selected endpoint surfaces and inference formats. Preserve
existing Mantle defaults and authentication ownership. Messages API is out of scope.

- [x] Add `BEDROCK_ENDPOINT=mantle|runtime`; retain `BEDROCK_API_MODE=responses|completions`
  and add `converse` for Runtime only. Validate combinations without authentication.
- [x] Derive discovery from the endpoint: Mantle `/v1/models`; Runtime paginated
  `ListFoundationModels` and `ListInferenceProfiles` through the AWS control plane.
  Preserve exact model/profile identifiers; discovery never proves inference access.
- [x] Project every valid Runtime model and profile identifier returned by discovery, including future IDs, without a model-based endpoint or API availability blacklist. The operator's model filter selects which IDs to use; AWS remains the authority on endpoint and feature availability. Keep exact identity and never retry on another route. Generic metadata must not invent limits or native structured support.
- [x] Apply bundled Mantle test-profile defaults only when the selected endpoint is Mantle. A Runtime selection retains the user’s exact API, model and test-specific settings; an inactive profile must still pass declaration validation.
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
