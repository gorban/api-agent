# Amazon Bedrock provider

Reviewed workflow qualifications provide Bedrock cap 52
and GPT cap 48 under their separate fingerprints; exact safe exports are promoted.
Bedrock qualification includes the credential transaction/adapter identity repairs. The later host-admission reporting fix leaves sampling
fingerprints unchanged. The normal catalog passed all 17 core cases plus one
provider UI case, with successful Allure publication and clean exit. The complete
unit gate passed 18,674 tests with exact line/branch coverage. Final artifact/package/external review passed; human acceptance remains separate.

This optional provider uses only regional bedrock-mantle
endpoint through the ordinary provider interface. The maintained OpenAI SDK handles AWS signing, HTTP and SSE in an owned worker.
Converse, InvokeModel, Bedrock Runtime and direct OpenAI fallback remain unsupported.
Controlled tests qualify the initial matrix below; live account access remains
a separate acceptance step.

## Install and select

Install the optional Python dependencies in the environment running the app:

    python -m pip install 'api-agent[bedrock]'

The dependency manifest declares identical Boto3/pinned openai[bedrock] requirements. Source packaging requires frontend assets from npm ci.
SDK imports and credential resolution are lazy, so configuration inspection
does not require AWS access or invoke login.

From the source owner root set:

    LLM_PROVIDER=src/api_agent/llm_providers/bedrock/factory.py:factory

For an installed package, locate that factory beside this README through
api_agent.llm_providers.bedrock.__file__.

## Modes, models and defaults

BEDROCK_AUTH_MODE selects session, access_key or api_key.
BEDROCK_API_MODE independently selects completions (Chat Completions) or
responses. The default API is responses. Both APIs use the literal model-ID
regex `openai\.gpt-oss-20b` when no default model is configured. Explicit dotenv, environment, CLI and interactive
values always retain precedence; switching APIs never rewrites a saved model.

| Model ID | Qualified API formats |
| --- | --- |
| zai.glm-5 | completions |
| zai.glm-4.7 | completions |
| zai.glm-4.7-flash | completions |
| openai.gpt-oss-20b | completions, responses |
| openai.gpt-oss-120b | completions, responses |

Inference uses /v1; discovery uses /v1/models, never the SDK's /openai/v1 default. Models outside
this matrix fail explicitly, including allowlisted Claude families that need a
separately qualified Messages transport. Catalog visibility proves neither invocation permission nor free-tier/account
availability; discovery never runs paid probes.

LLM_ALLOWED_MODELS remains a shared full-model-ID regex. Bedrock contributes
OpenAI GPT, numeric GLM with optional Flash, and Claude Haiku/Sonnet/Opus
patterns; the execution matrix remains a separate restriction. Remove an old
saved GPT-only regex to accept this provider default. Provider declaration
defaults replace pack manifest/config declaration defaults under the existing
composition order; explicit pack dotenv values still win. LLM_DEFAULT_MODEL is
a separate regex: ambiguous families, incomparable versions or ties fail rather
than choosing an arbitrary model.

## Credential sources

- session uses the selected acquisition adapter. The builtin path requires a
  preauthenticated SDK profile or credential chain and rejects indexed account
  selection. BEDROCK_PROFILE defaults to lowercase default; BEDROCK_REGION
  defaults to us-east-1. Explicit blanks restore SDK resolution. A custom
  BEDROCK_SESSION_PROVIDER=file.py:factory can acquire complete temporary
  key/secret/token material, with identity binding, expiry, refresh and disposal.
- access_key with an unattended LLM User index reads only that selected item:
  access-key ID from username and secret access key from password. With no index,
  allowed private interaction prompts for the ID and then the masked secret.
  Temporary keys needing a session token require session mode and complete
  adapter-provided material; do not overload TOTP or omit the token.
- api_key with an unattended LLM User index reads only password as a Bedrock
  bearer key. With no index, allowed private interaction prompts for one masked
  opaque key. Username and TOTP are unused. Explicit-region bearer
  calls do not resolve AWS profiles or credentials. Expired/rejected keys fail
  without generation, login or another source being substituted.

Indexed modes use the configured password-manager provider and authenticated
retrieval helper; missing indexed material never falls back to prompts or another
identity. Attended key entry uses the existing private authentication screen and
preserves exact values, including whitespace and punctuation. NO_STD_IN, missing
or canceled interaction fails explicitly without hidden stdin. No new vault item
is created.

The optional native CLI adapter supports attended direct AWS/Identity Center.
Indexed direct/federated automation remains future acquisition work.
Okta is one replaceable federated provider, not the only planned session route. For session adapters, the version-one asynchronous
acquire(request, *, previous=None)/aclose contract and canonical
SessionRequest/SessionMaterial types live in acquisition.py. Indexed custom
adapters must verify effective AWS identity. The builtin profile path does not
establish indexed identity binding. See the [acquisition requirements](../../../../docs/prds/implementation_plan_f4a1.md).

Secrets stay in opaque owners/worker stdin, never arguments, credential
environment assignments, dotenv or ordinary diagnostics.
Closing owners revokes adapter credential reads and accounts for admitted helper
cleanup. Each request captures one refreshed auth snapshot. Shared setup,
refresh, cancellation and failed cleanup retain explicit retry ownership.

## Selected-profile sessions and attended recovery

Session setup first verifies the selected SDK profile. Usable credentials need no extra identity settings/CLI/browser. Select the optional bundled native
adapter only to permit attended direct AWS or Identity Center recovery when
credentials are unavailable. Expected account/principal assertions are optional;
SSO routing is requested through ordinary configuration review only when needed.
See [session login and recovery](SESSION_LOGIN.md) for selectors, routing,
identity assertions, selected-profile/cache persistence, exported credential
snapshots, refresh behavior and owned cleanup. Native persistence has offline qualification; human live-persistence acceptance
remains pending.

## Prerequisites

See [credential, platform, CLI and clipboard prerequisites](PREREQUISITES.md).

## Controlled capabilities and limits

Text, client-side tools, output-token limits and model-qualified temperature are
supported by the controlled protocol tests. The reported Chat Completions
serialization and observed model/tier metadata are covered by focused offline
regressions. Earlier live results retain their recorded scope; new pricing is
qualified without additional inference. Requests default to a finite 1024 output-token cap when no limit is
supplied. Responses always sends store=False. Actionable streamed tool calls wait
for EOF, terminal reconstruction and validation against the original tool schemas.
Missing/invalid input, output or total usage prevents successful completion.
Native reasoning output is bounded and suppressed; it is not displayed, persisted
as continuation or promised to preserve thinking across tool turns.

GPT-OSS 20B and 120B support explicit low, medium and high reasoning effort on
both APIs. Responses is the qualified route for reasoning-token contrast tests;
Completions can omit reasoning-token details. GLM 5 supports bounded strict JSON
schema output on Completions. Other structured model/API combinations and GLM
categorical effort controls fail explicitly. See [integration qualification](QUALIFICATION.md)
for provider-contributed test selections and their limits.

Explicit cache controls, cache boundaries, native continuations, preserved
reasoning continuity and additional model routes remain unsupported.
Token/context caps interpret published K units conservatively in
decimal units. Usage is normalized, with bounded AWS list-price estimates described in [costs](COSTS.md).
Reported invoice charges remain unavailable; quotes never use GPT rates or report
unknown spend as zero.

## Ordinary integration profiles

[test-profiles.toml](test-profiles.toml) supplies exact-case defaults. The launcher
resolves API/model/controls/deadlines before backend creation; select ordinary
cases together without manual API switching.
Application configuration, saved dotenv values and authentication identity stay
unchanged. Explicit `LLM_*_TEST` selections retain authority, including an
explicit empty value; invalid or unsupported selections fail visibly.

| Case profile | Operation defaults |
| --- | --- |
| Ordinary discovery, request and Studio request cases | Responses, OSS20B, low effort |
| Tool workflow cases with strict structured relevance replies | Completions, GLM5, no reasoning effort |
| Reasoning contrast | Responses, OSS120B, low versus medium, 16000 output tokens, 960-second request deadline |
| Studio selection | Responses, OSS20B low to OSS120B medium |
| Workflow | Completions, GLM 5, strict structured output with 1024 output tokens, four component passes |

The workflow profile uses graph/component/LLM budgets of 600/300/120 seconds.
The ordinary application default remains two component passes. Provider inference
and setup defaults remain 900 and 960 seconds; shorter parent deadlines still
apply. Profiles select operations within the selected provider and use the
intended configured credential identity. Studio browser cases retain their
ordinary frontend/browser prerequisites. Unsupported cache-retention controls
remain unsupported.

Earlier reporting-enabled 14 Responses plus one Completions workflow passes
retain their source/settings scope in [historical qualification](QUALIFICATION.md);
they neither qualify the current 18-case catalog nor authorize inference.

The fresh independently reviewed calibration matches its frozen sampling
identity after the credential transaction repair. The reporting-only host
admission correction preserves that sampling identity. Any different source, provider, route,
model, dependency versions or effective settings still requires a matching
reviewed qualification. Stale reports cannot substitute; committed workflow provenance checks remain required. See [calibration maintenance](../../../../docs/workflow_calibration.md)
and [integration readiness requirements](../../../../docs/prds/implementation_plan_f4l2.md).
Profile resolution does not waive test assertions or turn an unavailable
prerequisite into a passing result.

Run focused offline tests:

    PYTHONPATH=src python -m pytest -q tests/unit/test_bedrock_*.py \
      --cov=api_agent.llm_providers.bedrock --cov-branch \
      --cov-report=term-missing --cov-fail-under=100

Tests use synthetic SDK/HTTP responses, controlled subprocesses and real
flow.md.html/LangGraph/provider joins. They require no real credentials or AWS
calls. Account readiness must be confirmed before live authentication/catalog
checks; inference needs separate explicit bounded spending approval. The full applicable unit gate passed with exact line/branch coverage. Final
archive/packaging/boundary reconciliation remains a main-integration gate.

Protocol and model requirements: [Mantle contract](../../../../docs/prds/implementation_plan_f4d.md),
[AWS endpoints](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html),
[Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
and [Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html).

Direct AWS sign-in and IAM Identity Center use separate session adapters:
`src/api_agent/llm_providers/bedrock/aws_login.py:factory` and
`src/api_agent/llm_providers/bedrock/aws_sso.py:factory`, respectively, selected
through BEDROCK_SESSION_PROVIDER. Both reuse valid selected credentials before
recovery. See [session setup and migration](SESSION_LOGIN.md).

See [list-price estimates and limitations](COSTS.md) for qualified models, regions, tiers and historical predictions.
