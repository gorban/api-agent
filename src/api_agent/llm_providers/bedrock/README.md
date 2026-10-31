# Amazon Bedrock provider

This optional provider supports regional bedrock-mantle and bedrock-runtime endpoints through the ordinary provider interface. The worker owns SDK signing and transport. InvokeModel, the Messages API and direct OpenAI fallback remain unsupported.
Controlled tests qualify the initial matrix below; live account access remains
a separate acceptance step.

## Install and select

For a source checkout, follow [Getting started](../../../../README.md#get-started)
and select this factory in Review configuration or `.env`:

    LLM_PROVIDER=src/api_agent/llm_providers/bedrock/factory.py:factory

`./launch.sh` installs the selected provider's Python SDK dependencies through
uv/PyPI automatically. AWS CLI is not required for API-key or explicit access-key
authentication; CLI-assisted session recovery has [conditional prerequisites](PREREQUISITES.md).
Normal mandatory workflow coverage needs no calibration baseline; see
[workflow coverage](../../../../docs/workflow_poc.md#deterministic-workflow-coverage).

For an independently managed installed-package environment instead:

    python -m pip install 'api-agent[bedrock]'

Source packaging requires frontend assets from npm ci.
SDK imports and credential resolution are lazy, so configuration inspection
does not require AWS access or invoke login.

For an installed package, locate that factory beside this README through
api_agent.llm_providers.bedrock.__file__.

## Modes, models and defaults

BEDROCK_AUTH_MODE selects session, access_key or api_key.
BEDROCK_ENDPOINT selects mantle (the default) or runtime; discovery follows the same endpoint. BEDROCK_API_MODE independently selects completions (Chat Completions), responses, or Runtime-only converse. The default API is responses. The original Mantle modes use the literal model-ID
regex `openai\.gpt-oss-20b` when no default model is configured. Explicit dotenv, environment, CLI and interactive
values always retain precedence; switching APIs never rewrites a saved model.
Known closed-weight GPT models on Runtime Responses use system inference-profile IDs, for example
`LLM_DEFAULT_MODEL=us\.openai\.gpt-5\.6-luna`; the provider never chooses a
geographic profile on the user's behalf. Studio lists routing scopes as separate
model families (for example, US versus GLOBAL), preserving the selected scope when
choosing a version. Runtime discovery uses the AWS control-plane
model and inference-profile APIs and does not establish invocation permission.
Runtime discovery projects valid model and profile IDs without claiming that every
model supports the selected endpoint or API. Use `LLM_ALLOWED_MODELS` to filter
the list against AWS documentation and account access. Incompatible requests may
be rejected by AWS. Runtime prices are unavailable until independently qualified.
Native structured output is limited to documented strict schemas; the existing
fallback policy handles unqualified models and unsupported streaming combinations.
GPT-5.4 is documented as Mantle-only. Some Claude models emit signed reasoning
content that the current Converse codec cannot retain for continuation; such a
response fails safely.


| Mantle model ID | Qualified API formats |
| --- | --- |
| zai.glm-5 | completions |
| zai.glm-4.7 | completions |
| zai.glm-4.7-flash | completions |
| openai.gpt-oss-20b | completions, responses |
| openai.gpt-oss-120b | completions, responses |

Mantle inference uses /v1; discovery uses /v1/models, never the SDK's /openai/v1 default. Mantle models outside
this matrix fail explicitly. Runtime supports a separate OpenAI-compatible path
and native Converse, as described above. Catalog visibility proves neither invocation permission nor free-tier/account
availability; discovery never runs paid probes.

LLM_ALLOWED_MODELS remains a shared full-model-ID regex. Bedrock defaults to
`.+`; configure a narrower filter for your endpoint, API and account access.
The Mantle execution matrix remains a separate restriction. Explicit saved
filters retain precedence. Provider declaration
defaults replace pack manifest/config declaration defaults under the existing
composition order; explicit pack dotenv values still win. LLM_DEFAULT_MODEL is
a separate regex: ambiguous families, incomparable versions or ties fail rather
than choosing an arbitrary model.

## Credential sources

- session defaults to the bundled aws_login.py:factory adapter through ordinary
  configuration resolution. It reuses valid selected SDK credentials first,
  then offers attended direct AWS recovery when permitted. No extra session
  selector is needed. Explicitly empty BEDROCK_SESSION_PROVIDER selects the
  SDK-only preauthenticated path; an explicit SSO/custom selector remains
  authoritative. Bundled adapters reject indexed account selection; use
  `--unattended-user-index=` for attended session login. NO_STD_IN terminal
  execution permits session sign-in through an explicitly supplied visible
  authentication channel; a missing/disallowed owner prevents hidden recovery. BEDROCK_PROFILE
  defaults to lowercase default and BEDROCK_REGION to us-east-1; explicit blanks
  restore SDK resolution. Custom adapters own acquisition, refresh and disposal.
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

The default native adapter supports attended direct AWS recovery; explicitly
select an absolute or owner-root-relative path to aws_sso.py:factory for IAM
Identity Center.
Indexed direct/federated acquisition remains future work; Okta is replaceable. For session adapters, the version-one asynchronous
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

Session setup first verifies the selected SDK profile. Usable credentials need no extra identity settings/CLI/browser. Ordinary resolved defaults select the bundled
direct AWS adapter for permitted attended recovery when credentials are
unavailable. Explicit blank selects SDK-only acquisition; Identity Center needs
an explicit SSO adapter path. Expected account/principal assertions are optional;
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
Native reasoning text is bounded and suppressed. Legacy plaintext-only reasoning
without replay-relevant native metadata retains the existing suppression behavior
and no continuation. Responses tool turns that return encrypted native state retain
validated opaque state in opaque, report-excluded continuation; replay
preserves admitted message and function-call metadata without duplicating the
normalized assistant. State belongs to the same provider owner, exact model, API,
endpoint, resolved region and frozen authentication identity. Credential rotation,
a recreated provider or unresolved default-chain identity/region prevents replay.
Final answers without tool calls retain no continuation. See
[the continuation requirements](../../../../docs/prds/implementation_plan_f4o1.md).

GPT-OSS 20B and 120B support explicit low, medium and high reasoning effort on
both APIs. Responses is the qualified route for reasoning-token contrast tests;
Completions can omit reasoning-token details. GLM 5 supports bounded strict JSON
schema output on Completions. Other structured model/API combinations and GLM
categorical effort controls fail explicitly. See [integration qualification](QUALIFICATION.md)
for provider-contributed test selections and their limits.

New response policies admit native strict GLM5 Completions schemas within the
conservative subset. Native JSON mode, OSS schemas and response-format/tool
combinations remain unqualified; allowed fallback uses the same text route.
Original-schema validation and legacy behavior remain intact. See
[response modes and evidence](../gpt/RESPONSE_MODES.md).

Explicit cache controls, cache boundaries and continuation on Completions or
Converse remain unsupported. Native Responses replay supports only the validated
encrypted tool-turn contract; unsupported reasoning shapes fail explicitly.
Token/context caps interpret published K units conservatively in
decimal units. Usage is normalized, with bounded AWS list-price estimates described in [costs](COSTS.md).
Reported invoice charges remain unavailable; quotes never use GPT rates or report
unknown spend as zero.

## Ordinary integration profiles

[test-profiles.toml](test-profiles.toml) supplies Mantle-only exact-case defaults. Runtime retains
the operator's application and test-specific selections. The launcher
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

Workflow graph/component/LLM budgets are 600/300/120 seconds; application defaults
remain two passes. Inference/setup defaults are 900/960 seconds, clipped by parents. Profiles select operations within the selected provider and use the
intended configured credential identity. Studio browser cases retain their
ordinary frontend/browser prerequisites. Unsupported cache-retention controls
remain unsupported.

Earlier reporting-enabled 14 Responses plus one Completions workflow passes
retain their source/settings scope in [historical qualification](QUALIFICATION.md);
they do not establish a new full 18-case SDK-profile run or authorize inference.

Normal workflow coverage now executes ten fresh-UUID route/readiness cases under
`LLM_WORKFLOW_TEST_TIMEOUT_SECONDS` (600 seconds by default), without consulting
statistical baselines. Its profile sets the shared output-token cap to 1024;
application generation limits remain independent. Earlier native-adapter and
reporting qualification retain their historical source/settings identity in
[qualification](QUALIFICATION.md). They are never relabeled as observations of
new source. Committed workflow provenance checks remain required. See
[legacy calibration maintenance](../../../../docs/workflow_calibration.md) and
[integration readiness requirements](../../../../docs/prds/implementation_plan_f4l2.md).
Profile resolution does not waive test assertions or turn an unavailable
prerequisite into a passing result.

Run focused offline tests:

    PYTHONPATH=src python -m pytest -q tests/unit/test_bedrock_*.py \
      --cov=api_agent.llm_providers.bedrock --cov-branch \
      --cov-report=term-missing --cov-fail-under=100

Tests use synthetic SDK/HTTP responses, controlled subprocesses and real
flow.md.html/LangGraph/provider joins. They require no real credentials or AWS
calls. Account readiness must be confirmed before live authentication/catalog
checks; inference needs separate explicit bounded spending approval. The full applicable unit gate passed with exact line/branch coverage. See [qualification](QUALIFICATION.md) for technical gates and acceptance.

Protocol and model requirements: [Mantle contract](../../../../docs/prds/implementation_plan_f4d.md),
[AWS endpoints](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html),
[Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
and [Responses](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-responses-api.html).

Direct AWS sign-in and IAM Identity Center use separate session adapters:
`src/api_agent/llm_providers/bedrock/aws_login.py:factory` and
`src/api_agent/llm_providers/bedrock/aws_sso.py:factory`, respectively, selected
through BEDROCK_SESSION_PROVIDER. Both reuse valid selected credentials before
recovery. See [session setup and migration](SESSION_LOGIN.md).

## Historical qualification checkpoints

These measurements describe older statistical workflow coverage, not a setup
requirement for today's deterministic mandatory coverage fixture.

The historical API-key Completions calibration qualified Bedrock cap 50 from
60 graphs and 156 calls: measured minimum 46 plus allowance 4. GPT cap 57 was
unchanged at that checkpoint. The matching workflow coverage test passed 1/1,
with six user turns and 9,415 tokens; cost remained unknown. Source verification
passed 19,433 unit tests with exact line/branch coverage and independent/external
reviews. The effective default attended login worked; repeat/restart reuse, SSO
and indexed session qualification remained separate. Packaged/relocated artifact
verification and final delivery review passed. Later deterministic coverage has
separate unit, artifact, live, review and human gates.

The earlier built-in SDK-profile checkpoint qualified Bedrock cap 50 with GPT
cap 48. Fresh workflow/reasoning checks passed, retaining an earlier empty-answer
failure. Its 18,700 units passed with exact line/branch coverage and technical
reviews; launcher follow-up acceptance was conditional.

The historical native-adapter checkpoint qualified cap 52, passed 17 core plus
one provider UI case and 18,674 units. This is not a new full 18-case SDK run.
See the [qualification guide](QUALIFICATION.md) for evidence and acceptance scopes.


Structured-output fallback follows the declared workflow policy and the known
schema capability catalog. Configurable capability caching and optional probes
remain in the [capability-discovery plan](../../../../docs/prds/implementation_plan_f1a2.md).
