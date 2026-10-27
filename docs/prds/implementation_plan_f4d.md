# Mantle-only inference and authentication modes

[Bedrock requirements](implementation_plan_f4.md) · [Authentication](implementation_plan_f4a.md) · [Transport/costs](implementation_plan_f4c.md)

The Bedrock provider targets only `bedrock-mantle.{region}.api.aws`. This replaces the earlier Converse/ConverseStream prototype direction. The controlled implementation now replaces the earlier inference surface while preserving acquisition/lifecycle ownership. Offline qualification establishes the initial matrix below, not live model/account availability.

## Agreed modes

- [x] BEDROCK_AUTH_MODE distinguishes `session`, `access_key` and `api_key`, independently of API shape. Session uses SDK/profile or adapter-acquired temporary AWS credentials. Access-key logins store key ID in username and secret access key in password. API-key logins store the Bedrock bearer key in password; username is unused. No credential or endpoint fallback between modes.
- [x] BEDROCK_API_MODE distinguishes `completions` and `responses`, analogous to the included GPT provider's API selector. Runtime selectors are implemented for the controlled matrix; broader capability and live acceptance remain open. Preserve profile default `default` and region default `us-east-1`, explicit overrides and explicit-blank SDK resolution.
- [x] Contribute both selectors with descriptions and enumerated editors through ordinary provider configuration. Keep acquisition independent of billing. Use an explicit documented default for the API selector and preserve existing explicit settings. Unknown costs remain unavailable rather than zero.

The default API is Responses. Both Responses and Chat Completions use the
literal default-model regex `openai\.gpt-oss-20b` when no model is explicitly
selected. Saved API and model values remain authoritative. The initial controlled matrix qualifies
`zai.glm-5`, `zai.glm-4.7` and `zai.glm-4.7-flash` for completions, and
`openai.gpt-oss-20b`/`openai.gpt-oss-120b` for both APIs, using `/v1`. Expand
the execution matrix only with independent capability evidence. Broader allowlist
patterns do not admit unqualified versions or API formats automatically.

## Endpoint, catalog and protocol

- [x] Construct an HTTPS Mantle regional endpoint and qualify the model's documented prefix, `/v1` or `/openai/v1`. Most models use `/v1`; do not infer a universal prefix from API compatibility. Keep endpoint and signing region consistent. Never route to bedrock-runtime, the direct OpenAI endpoint, Converse or InvokeModel as a fallback.
- [x] Use the Mantle OpenAI-compatible `GET /models` catalog and endpoint-specific model IDs. The former six-model Converse allowlist is not Mantle admission evidence. A catalog entry alone does not prove support for both API modes, tools, streaming or account inference permissions; establish a documented initial capability/route matrix.
- [x] Implement Chat Completions and Responses requests, results and streams under the existing LLM provider contract, including text and qualified tool-call/result turns through actual flow.md.html/LangGraph execution. Keep model identity, schema validation, usage and cancellation semantics. Reject unsupported capabilities before requests where determinable.
- [x] Reuse provider-neutral protocol helpers where suitable after checking the GPT implementation. Do not inherit GPT-only model-name filters, subscription OAuth/headers, quota accounting, price sources or backend discovery probes. OpenAI-compatible request shapes do not make a Bedrock API key an OpenAI key.
- [x] Preserve validated terminal stream reconstruction and buffer actionable tool calls until request/result validation completes. Require API-specific token usage and classify bounded HTTP/SSE errors without exposing credentials or upstream diagnostics. Define request storage explicitly for Responses; do not silently adopt server-side conversation retention.

## Provider model filters

- [x] Keep LLM_ALLOWED_MODELS as a shared full-model-ID regular expression, with provider-contributed defaults. Preserve the included GPT provider's existing default policy. Bedrock's default includes version variants of OpenAI GPT, numeric Z.AI GLM with optional Flash, and only Anthropic Claude Haiku, Sonnet and Opus. Explicit dotenv, environment, CLI and interactive patterns retain precedence; do not rewrite saved configuration. Provider declaration defaults retain the existing precedence over resource-pack manifest/config declaration defaults. Moving this default out of shared configuration intentionally changes which fallback wins for this field; pack policies that must persist need an explicit value rather than a declaration default.
- [x] Qualify GLM first for the initial Mantle Chat Completions path: the documented IDs are `zai.glm-5`, `zai.glm-4.7` and `zai.glm-4.7-flash`, with `/v1` inference paths. These models do not document Responses support. Do not infer support from the endpoint's overall API list, and do not silently change API mode after a failed call.
- [x] Retrieve model IDs from Mantle `/v1/models`, then intersect the user filter with independently qualified model/API capabilities. Catalog visibility and allowlist membership do not establish account permission, free-tier eligibility or successful inference. Do not make paid capability probes during discovery.
- [x] Include the requested Claude families in the default filter, but keep them unavailable for execution through unqualified API formats. Their current Mantle cards document Anthropic Messages, rather than Responses or Chat Completions. A future Messages transport requires separate requirements and qualification; adding a filter branch does not introduce it. LLM_DEFAULT_MODEL remains an explicit selection concern separate from admission.

Model basis: [GLM 5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5.html), [GLM 4.7](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7.html), [GLM 4.7 Flash](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-4-7-flash.html), [Claude Haiku](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html) and [Claude Sonnet](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html).

## Credentials and owned work

- [x] For session/access_key, authenticate Mantle HTTP requests using supported AWS credentials with verified signing-region/service rules; include session tokens for temporary credentials. Resolve blocking SDK profiles/process helpers inside the bounded owner. Sign the exact URL/method/body bytes sent over HTTP and prevent redirects or unrelated ambient credentials from changing the chosen source.
- [x] For api_key, use the selected password-only Bedrock key as a scoped bearer authorization header. Do not require username, AWS profile or SDK credential discovery for an explicit-region bearer request. Keep the key opaque; controlled rejected/expired-key HTTP failures do not trigger generation or fallback. Live short/long-term key expiry remains an authentication acceptance gate.
- [x] Keep shared setup, credential-reader revocation, coalesced refresh, session expiry and cancellation/cleanup retry intact. A cancelled setup must dispose an unpublished acquired session. Closing ownership revokes new credential reads and accounts for in-flight helper cleanup, including timeout and repeated-cancellation races.

## Controlled verification

The preceding runtime snapshot passed 675 focused tests with exact100% line/branch
coverage across its nine executable provider modules (1220 statements,388 branches).
Subsequent native acquisition and effective-configuration qualification is tracked
in the [native sign-in milestone](implementation_plan_f4a2.md).
Three Sol low lanes and distinct Sol high reviews closed nullable SDK fields,
early response identity/errors, unsupported tool execution metadata, SDK option
escapes and incomplete-terminal forwarding. External review additionally found
repeated process-group disposal and missing-credential classification; focused
repairs add coalesced cleanup receipts and explicit authentication errors.
Independent integration probes cover
six auth/API pairs through the real parser/LangGraph path, tool turns, EOF/schema
publication gates, unknown-cost accounting and retained acquisition ownership.
The source tests use synthetic SDK/HTTP data and controlled subprocesses only.
Package/boundary/external review and human acceptance below remain separate gates.

## Qualification and remaining phases

- [ ] Cover all three auth modes across both API modes offline, with relevant key/token, region/prefix, schema/stream, malformed response, failure and ownership regressions. Require focused exact 100% line/branch coverage for new provider code, meaningful mutations, source/installed/relocated resources, public-boundary checks and independent/external review before human acceptance.
- [ ] Implement and qualify concrete direct AWS/Identity Center/federated acquisition adapters separately, preserving the requested optional helper and TOTP behavior. Mantle API-key support does not close session login automation.
- [ ] Wait for account readiness before real authentication/catalog checks and separate explicit bounded inference approval. Keep full main-unit/coverage and later consumer integration gates unchanged.

Protocol basis: [AWS endpoints](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html), [Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html), [Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html), [Bedrock API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html) and [API-key permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-permissions.html). Pin and verify supported model routes and signing contracts during implementation.
