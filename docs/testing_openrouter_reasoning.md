# Testing reasoning through OpenRouter

[Reasoning tests](testing_reasoning.md) · [Qualification requirements](prds/implementation_plan_f4o3b3.md)

## OpenRouter overlap

The same model can be offered by several backends with different IDs and wire
behavior. OpenRouter lists `openai/gpt-6-luna`, `openai/gpt-oss-20b` and
`z-ai/glm-5` in its [model catalog](https://openrouter.ai/api/v1/models).
The bundled GPT adapter currently admits the first two and filters out GLM5;
listing GLM5 upstream does not make it usable through that adapter.

Live checks on November 2, 2026 used the GPT provider, OpenRouter base URL,
API-key authentication and exact model filters. Browser checks ran once with
E2E reruns disabled. These earlier observations predate the GPT-OSS native decoder repair below; they are not a reliability guarantee:

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
cleared an optional saved setting without saving changes in those earlier checks.
The GPT-OSS qualification below now supports explicit categorical effort controls. Luna's replay test
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

## GPT-OSS controls and native Responses

The exact OpenRouter IDs `openai/gpt-oss-20b` and `openai/gpt-oss-120b`
use mandatory reasoning with Low, Medium and High controls. Off is unsupported.
Use the GPT provider, `GPT_AUTH_MODE=api_key`,
`GPT_BASE_URL=https://openrouter.ai/api/v1`, and your matching account selection.
For example:

```dotenv
LLM_ALLOWED_MODELS=openai/gpt-oss-(?:20b|120b)
LLM_DEFAULT_MODEL=openai/gpt-oss-20b
LLM_REASONING_EFFORT=low
GPT_API_MODE=responses
```

Responses extracts native plaintext reasoning separately from the final answer
and retains the original native items for ordered replay. Completions effort
controls work independently; its native display/replay remains unqualified.
Neither effort acceptance nor reasoning-token usage proves visible text delivery.
Empty effort uses upstream defaults; it does not disable reasoning. Exact IDs
are qualified independently of official OpenAI routes or suffixed aliases.

Run the ordinary retained Studio integration with the settings above:

```bash
./launch.sh --tui-mode integration --no-std-in --debug api-agent::tests/integration/test_studio_request.py::test_studio_request
```

Expanded response details should contain native reasoning separately from `Ok`.
API-key inference currently delivers this text after each call completes; this
is not token streaming. Use the optional node-summary E2E from the parent guide
to check delivery before the overall multi-node response finishes.
