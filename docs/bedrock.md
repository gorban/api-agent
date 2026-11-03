# Using Amazon Bedrock

[README](../README.md#guides) · [Settings controls](workbench.md) ·
[Provider implementation details](../src/api_agent/llm_providers/bedrock/README.md)

## Select the provider in the TUI

1. Run `./launch.sh`. From the main menu, open **Review configuration**.
2. Find `LLM_PROVIDER`, edit it, and save this factory selector:

   ```text
   src/api_agent/llm_providers/bedrock/factory.py:factory
   ```

3. If `LLM_PROVIDER` already has a saved value and is absent from Review,
   first open **Clear .env configuration**, select `LLM_PROVIDER` and confirm
   deletion. Close that screen and enter Review to set the replacement.
   This removes the saved assignment, not the provider or its credentials. It
   temporarily restores the configured fallback provider (bundled GPT in the base
   application) until you save the Bedrock selector.
4. Review the newly revealed `BEDROCK_*` settings. Choose the endpoint, API and
   authentication mode together using the tables below. Set both model filters.
5. Close Review, then use **Run local**, **Integration tests** or **E2E tests**.
   The launcher prepares the selected provider's SDK dependencies automatically.

Bedrock settings appear from the selected provider's declarations. Configuration
inspection does not sign in or perform inference. Clear other saved settings the
same way when you want their defaults or another value. Environment variables and
CLI overrides take precedence over `.env`; deleting a saved assignment does not
remove those overrides. See [configuration precedence](workbench.md#overrides-and-command-line-options).

The selector above is relative to the repository root. Installed-package callers
can locate `factory.py` beside `api_agent.llm_providers.bedrock.__file__`.

## Choose an endpoint, API and identity

| Setting | Choices and meaning |
| --- | --- |
| `BEDROCK_ENDPOINT` | `runtime` is the default; `mantle` is explicit. Discovery follows this selection. |
| `BEDROCK_API_MODE` | `converse`, `responses` or `completions` (Chat Completions). The declared default is `responses`; choose a compatible API for your model. |
| `BEDROCK_AUTH_MODE` | `session`, `access_key` or `api_key`. |
| `BEDROCK_REGION` | Defaults to `us-east-1`; set the intended region explicitly when needed. |
| `BEDROCK_PROFILE` | Session profile, default `default`; explicitly blank restores SDK profile resolution. |
| `LLM_ALLOWED_MODELS` | Full-ID regular expression filtering the discovered catalog. |
| `LLM_DEFAULT_MODEL` | Full-ID regular expression resolving the default from that filtered catalog. |

Converse requires Runtime. Neither endpoint/API selection nor discovery proves
invocation permission. The provider preserves the selected model, route and
identity when AWS rejects a request. It does not silently switch them.

| Authentication | How credentials are acquired |
| --- | --- |
| `session` | Reuses the selected SDK profile credentials, including externally refreshed credentials. The default session adapter offers attended AWS recovery when necessary and permitted. Leave the unattended user index empty with bundled session adapters. |
| `access_key` | Reads the selected LLM User entry's username as key ID and password as secret key; without an index, uses attended prompts when permitted. Temporary credentials require a session adapter. |
| `api_key` | Reads the selected LLM User entry's password as bearer key, or an attended masked prompt. Account and API permissions still apply. |

The provider implements all three authentication modes for Runtime and Mantle;
this does not establish that a particular key has the required permissions.
Custom session acquisition is described in the [provider details](../src/api_agent/llm_providers/bedrock/SESSION_LOGIN.md).
No credentials belong in `LLM_PROVIDER`, model filters or profile selectors.

## Model-selection examples

Use IDs actually returned by your account's discovery. These are regular
expressions, not friendly Studio labels. `[.]` matches a literal period.

| Intended route | Endpoint / API | Example allowed and default selector |
| --- | --- | --- |
| GPT-OSS 20B | `runtime` / `completions` or `converse` | `openai[.]gpt-oss-20b-1:0` |
| GPT Luna 6, US profile | `runtime` / `responses` or `converse` | `us[.]openai[.]gpt-6-luna` |
| Claude Sonnet 4.5, US profile | `runtime` / `converse` | `us[.]anthropic[.]claude-sonnet-4-5-20250929-v1:0` |
| GPT-OSS 20B | `mantle` / `responses` or `completions` | `openai[.]gpt-oss-20b` |

The bundled default-model pattern covers OSS 20B IDs on either endpoint. It does
not make Runtime OSS support Responses: AWS lists OSS Responses on Mantle only.
Set an API/model pair explicitly. Claude routes in this guide use Converse;
Messages and InvokeModel are not implemented by this adapter. Check the exact
[AWS API/model matrix](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html)
before expanding your filter.

For US GPT and Claude families, including older Claude version-first IDs:

```text
LLM_ALLOWED_MODELS=us[.](?:anthropic[.]claude-(?:(?:haiku|sonnet|opus)-.+|[0-9]+(?:-[0-9]+)*-(?:haiku|sonnet|opus)-.+)|openai[.]gpt-[0-9]+(?:[.][0-9]+)*-(?:luna|terra|sol)(?:-[0-9]{4}-[0-9]{2}-[0-9]{2})?)
```

Choose a compatible exact default separately. A broad filter can expose models
that do not support your selected API. Studio preserves exact execution IDs;
family/version display names do not grant capabilities or access.

## Support matrix

This table describes **current api-agent admission**, not every AWS model.
A tool's argument schema and the final answer's JSON Schema are separate contracts.

| Combination | Converse | Responses |
| --- | --- | --- |
| Reasoning alone | Qualified GPT and Claude versions | Qualified models/routes |
| Application tools plus reasoning, without a native answer schema | Native tool/result continuation supported, including signed/opaque state | Native function/result continuation supported on qualified routes |
| Native answer schema plus an explicit effort selection and/or application tools | Current policy requires permitted fallback or rejects before inference | Special qualification for Runtime `us.openai.gpt-5.6-luna` and `us.openai.gpt-6-luna`: omitted/low/high effort, non-strict tools |
| Strict tool arguments plus a native answer schema | Do not infer combination support from separate capabilities; current combined policy falls back/rejects | Outside that special combined qualification |
| Native answer schema plus streaming | GPT-6 Luna/Astra are blocked; declared fallback or rejection applies | Exact Luna qualification admits it; broader streaming/schema support remains unqualified |
| Early visible reasoning | Native SDK stream is collected and validated before display; usually per-call/node progress | Admitted native summary deltas can be forwarded when supplied |

AWS's current Luna card describes JSON Schema for **nonstreaming** requests across
APIs. The existing streamed Responses qualification is narrower implementation
and test evidence, not a universal upstream guarantee. Reconciling it is an open
[qualification item](prds/implementation_plan_f4o4b.md).
See the [Luna card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html)
and [structured-output contracts](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html).

Fallback must be allowed by the node. The original schema is still validated
locally. Synthetic strict-function adaptation and configurable capability caches
remain [future work](prds/implementation_plan_f1a2.md); an ordinary tool call is
not that adapter. Inspect `response` metadata and `output_mechanism` in call logs
to distinguish native schema output from prompted JSON.

## Reasoning controls and display

Qualified GPT models use native effort. Older Claude versions use configurable
`LLM_REASONING_LOW_TOKENS=1024`, `LLM_REASONING_MEDIUM_TOKENS=4096` and
`LLM_REASONING_HIGH_TOKENS=16384`. Newer adaptive Claude versions use native
output effort; Opus 4.5 combines native effort with a manual budget. These three
settings appear for Converse. See [budget limits and replay](testing_bedrock_converse.md).

Provider default omits effort; `Off` is exposed only where disabling reasoning is
qualified. Active manual Claude thinking is incompatible with temperature changes
and forced tool selection. [AWS thinking constraints](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html).

Studio shows admitted reasoning separately, then replaces the preview with the
final answer and retains display text in collapsed details. A summary can be empty;
encrypted state is never shown. Missing reasoning-token counts remain unknown.
Use the [reasoning testing guide](testing_reasoning.md) for visible explanations,
streaming expectations and native-state replay tests.

## Check your setup and remaining qualification

First [configure test reporting](testing_qualification.md), then run a small request:

```bash
./launch.sh --tui-mode integration --no-std-in --start api-agent::tests/integration/test_llm_request.py::test_llm_request
```

Use `test_provider_reasoning_replay` for two-turn low-effort replay. To retain
Studio for manual requests with the configured model/API:

```bash
./launch.sh --tui-mode integration --debug --start api-agent::tests/integration/test_studio_request.py::test_studio_request
```

Select the version and reasoning effort in Studio before submitting your prompt. A replay test can
pass without native state; inspect its state/display evidence separately.
Successful discovery, HTTP status, a visible summary and reasoning-token usage
are distinct observations. Tracing failures do not make inference fail.

Bounded Runtime Converse checks passed for Opus 4.5 hybrid low-effort two-turn
replay and its low/high-effort named-tool workflows, including actual tool results.
These do not establish strict tools, native answer schemas or all
regional profiles. An automated Sonnet 5.5 replay check was account-denied;
the separate manual check below passed on an account with access.

Manual Studio checks also passed Converse high effort for Sonnet 4.5/5.5 and
Haiku 5.5, and selected GPT reasoning calls on Responses and Converse. These
checks do not establish an exhaustive model-ID/profile/tool/schema matrix.
Sonnet 4.5/5.5 Responses requests were rejected as unsupported API/model pairs.
Haiku 4.5 high effort failed the demo planner schema; a separate tool workflow
completed both reads but failed final raw-JSON validation. Workflow acceptance
remains open; neither result establishes an API/control rejection.

Errors identify different problems:

- `LLM_DEFAULT_MODEL matches no allowed model`: reconcile both filters with the
  selected catalog and preserve the exact model/profile ID.
- AWS `AccessDeniedException`: inspect the code, status and selected account's
  permissions/access. Discovery success does not prove invocation access.
- Model missing or unsupported API: check the exact AWS model/API matrix.
- `Response output does not match its original schema`: output was received but
  local contract validation failed. Inspect the named node, response mechanism,
  completion outcome and admitted request/response evidence. This alone does not
  show an unsupported reasoning control or justify weakening the schema.

The [qualification checklist](prds/implementation_plan_f4o4b.md) separates live
proofs from controlled tests and remaining combinations: GPT tool/reasoning/schema
matrices, Claude hybrid/adaptive effort with tools, and streamed schema behavior.
There is no claim that every cataloged version or regional profile has been live-tested.
