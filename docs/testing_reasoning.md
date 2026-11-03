# Testing reasoning, explanations and replay

[Testing from the launcher](../README.md#testing) ·
[Node summary requirements](prds/implementation_plan_f4o3b.md)

The application default graph and the optional-summary demonstration are separate
tests. Both use the selected real UI and provider; the latter selects its own
[declared graph](../tests/e2e/fixtures/node_summary/flow.md.html).
E2E describes the exercised browser-to-provider path, rather than requiring the
application's default graph. Keep `test_studio_graph` as its separate acceptance
check.

```bash
./launch.sh --tui-mode e2e --e2e-reruns 0 --no-std-in --start=E2E api-agent::tests/e2e/test_studio_reasoning.py::test_studio_reasoning
```

The test submits a request, observes nonempty reasoning while the final node is
waiting, releases that node, verifies the exact final answer `Ok`, and expands
response details. The earlier preview must be present there. The normal turn
screenshot captures the expanded details. This proves delivery and retention;
the controlled rendezvous does not measure provider streaming latency.
The demonstration explicitly selects `explanation`, so it can require visible
text even when a native-summary-capable model legitimately returns none. Native
text takes precedence when supplied. `auto` is tested independently; it does not
make another inference call just because one native summary was empty.

## Choosing a behavior to exercise

First [configure test storage](testing_qualification.md). Keep account selection and
credentials local; the examples do not change authentication or provision access.
Filter both model settings to the exact selection you intend to test. For
example, `LLM_ALLOWED_MODELS=gpt-6-luna` and `LLM_DEFAULT_MODEL=gpt-6-luna`
select Luna on the GPT provider. Bedrock IDs use periods and may have regional
prefixes: escape periods in regular expressions.

For a Bedrock Runtime/Completions example, first select the bundled Bedrock
provider and your existing session or access-key authentication. Then run:

```bash
./launch.sh --tui-mode e2e --e2e-reruns 0 --no-std-in --start=E2E --bedrock-endpoint runtime --bedrock-api-mode completions --llm-allowed-models 'openai[.]gpt-oss-20b(?:-1:0)?' --llm-default-model 'openai[.]gpt-oss-20b(?:-1:0)?' api-agent::tests/e2e/test_studio_reasoning.py::test_studio_reasoning
```

For GPT/Luna, select the bundled GPT provider, the official base URL and Responses
mode, then use `--llm-allowed-models gpt-6-luna --llm-default-model gpt-6-luna`
with the first E2E command. These overrides do not save configuration.

| Provider and model | Route | Behavior to inspect |
| --- | --- | --- |
| GPT, `gpt-6-luna` | Official OpenAI, Responses; subscription or API key | Opted-in native reasoning summary when supported; encrypted continuation stays private. A particular response can have an empty summary. |
| GPT, `gpt-6-luna` | Official OpenAI, Completions; API key | No qualified native display carrier; an opted-in node requests a generated explanation in its JSON response. |
| Bedrock, `openai.gpt-oss-20b-1:0` | Runtime, Completions; session or access key | Qualified tagged reasoning separated from the final answer; explicit `literal` preserves tags as answer text instead. |
| Bedrock, `zai.glm-5` | Mantle, Completions; supported authentication | The live short probe returned neither native reasoning alias. Use generated explanation; the decoder also admits plaintext aliases when supplied. Native availability is not advertised from effort support or a model name alone. |
| Bedrock, an available GPT/Luna inference profile | Runtime, Responses; session or access key | Encrypted replay state can accompany an answer without display text. Without a qualified native display capability, the optional node requests an explanation. |

Catalog access does not prove that an account can invoke a model or that every
feature works on every endpoint. Use the exact discovered execution ID. Do not
switch endpoint or authentication automatically to make a test pass. Runtime
API-key authentication remains unsupported by this adapter.
Native Bedrock replay requires an identity-bound frozen credential snapshot.
The bundled session adapter supplies one; legacy custom profile-only adapters
must adopt that contract before they can replay native reasoning state.

Model/API availability changes. Consult the
[OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning),
[AWS model cards](https://docs.aws.amazon.com/bedrock/latest/userguide/model-cards.html)
and [GLM 5 card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-zai-glm-5.html)
before adding another qualified example. A native display capability is distinct
from support for categorical reasoning effort.

## OpenRouter overlap

The same model can be offered by several backends with different IDs and wire
behavior. OpenRouter lists `openai/gpt-6-luna`, `openai/gpt-oss-20b` and
`z-ai/glm-5` in its [model catalog](https://openrouter.ai/api/v1/models).
The bundled GPT adapter currently admits the first two and filters out GLM5;
listing GLM5 upstream does not make it usable through that adapter.

Live checks on November 2, 2026 used the GPT provider, OpenRouter base URL,
API-key authentication and exact model filters. Browser checks ran once with
E2E reruns disabled. These are observed results, not a reliability guarantee:

| OpenRouter model | API mode | Application-flow E2E | Summary fixture E2E | Ordinary two-call replay |
| --- | --- | --- | --- | --- |
| `openai/gpt-6-luna` | Responses | Failed | Failed | Failed |
| `openai/gpt-6-luna` | Completions | Passed | Passed | Passed |
| `openai/gpt-oss-20b` | Responses | Passed | Passed | Passed |
| `openai/gpt-oss-20b` | Completions | Passed | Failed: `323` instead of `Ok` | Passed |
| `z-ai/glm-5` | Either | Not admitted | Not admitted | Not admitted |

Luna Responses returned encrypted continuation that the adapter rejected with
`GPT inference returned invalid continuation state`; this is an adapter
compatibility failure after inference, not an authentication failure. The OSS
Completions summary fixture delivered and retained progress, then failed its
unchanged final-answer assertion. Keep that mismatch visible; the browser probe
alone does not isolate its cause.
[OpenRouter follow-through requirements](prds/implementation_plan_f4o3b1.md)
track the unresolved compatibility work.

For a passing Luna example, select the GPT provider and your OpenRouter account:

```bash
./launch.sh --tui-mode e2e --no-std-in --start=E2E --e2e-reruns 0 --gpt-auth-mode api_key --gpt-base-url https://openrouter.ai/api/v1 --gpt-api-mode completions --llm-reasoning-effort= --llm-allowed-models openai/gpt-6-luna --llm-default-model openai/gpt-6-luna api-agent::tests/e2e/test_studio_reasoning.py::test_studio_reasoning
```

For OSS20B, replace both model filters with `openai/gpt-oss-20b`; use Responses
for the fully passing observed browser pair. The empty reasoning-effort override
clears an optional saved setting without saving changes: this adapter does not
qualify categorical effort controls for the OSS route. Luna's replay test
explicitly requests its advertised low effort. Account indexes are installation
specific; select your matching API-key entry rather than copying another user's.

Passing summary tests qualify the fixture's explanation/preview contract, not
native token streaming. The three passing replay runs exposed no native
continuation or display reasoning, so they prove ordinary conversation only.
They do not prove encrypted-state replay or cache hits. OpenRouter documents
[native reasoning preservation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning)
separately; a generated explanation does not establish that adapter capability.

Sol 6.1 and Terra 6.1 were separately checked with the puzzle through Studio,
OpenRouter Completions and medium effort, using a bounded 16,384-token cap. Both
completed with reasoning and final-answer tokens. Terra's route-specific controls
are now qualified. Catalog-advertised output limits let the shared configured
cap apply instead of the former 128-token text fallback; Sol previously exhausted
all 128 tokens on reasoning. These probes establish neither puzzle correctness nor native display/replay,
graph E2E or retained DEBUG human acceptance. The earlier matrix predates this
budget repair. [Requirements](prds/implementation_plan_f4o3b2.md).

## Node policy and expected UI

The optional eighth Functions column is `Display summary`, after `Fallback`.
Existing four- and seven-column declarations keep their behavior.

| Value | Behavior |
| --- | --- |
| `-` | Existing behavior; no schema transformation. |
| `native` | Request native display summary where the provider supports the hint; never generate an explanation. Empty display text is valid. |
| `auto` | Prefer the selected catalog's `display_reasoning` capability. Otherwise wrap the declared JSON response with a bounded explanation. Disabled reasoning effort uses the explanation path. |
| `explanation` | Always request the explanation envelope; prefer an actual native summary if one also arrives. |

Generated policies require a declared JSON response schema. They use the node's
existing output mode and fallback permission. The inference envelope has
`progress_summary` and `result`; the node receives only `result`. Generated text
is prefixed `Explanation:`. It is not decrypted reasoning and does not prove
reasoning-token use. No second call is made merely because native display text
was empty. Resolve the policy before dispatch and keep it stable across retries.

The Studio preview appears with Thinking, then the final answer replaces it.
Completed display text remains in collapsed response details. Native fields can
stream where supported; buffered and structured calls can publish after a node
completes. Display text is not automatically a friendly translation.
Responses plaintext `content` is held until completion so a native `summary`
can take precedence without changing an already published prefix.

## Replay and cache checks

Run focused offline tests without spending inference:

```bash
.venv/bin/python -m pytest tests/unit/test_reasoning_replay.py tests/unit/test_node_summary.py tests/unit/test_bedrock_reasoning_carrier.py tests/unit/test_bedrock_progress_reasoning.py
```

For a selected-provider two-call replay check, run:

```bash
./launch.sh --tui-mode integration --no-std-in --start=True api-agent::tests/integration/test_provider_reasoning_replay.py::test_provider_reasoning_replay
```

Its Workflow execution evidence attachment records whether the first reply
contained native continuation and display text. A pass with no native state
proves ordinary conversation replay only. A short successful replay does not
establish a prompt-cache hit.

These cover ordered completed-answer replay, tool continuations, generated
envelopes, native precedence, malformed carriers, fragmented streams and privacy.
Append-only history belongs to one workflow invocation and node dialogue; it is
not a persistent conversation store across independent browser turns.

Preserve original native items and their order, including encrypted state, rather
than replaying the display projection. Cache eligibility is different from a
guaranteed cache hit: inspect reported cached tokens with sufficiently long,
stable prefixes. Unknown token splits remain unknown. Converse signed reasoning
with tool continuations and redacted blocks remains explicitly unsupported.
Completed non-tool Converse plaintext blocks retain their signatures for replay;
the display projection never contains the signature.
