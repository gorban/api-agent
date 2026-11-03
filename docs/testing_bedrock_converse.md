# Bedrock Converse reasoning

[Reasoning testing guide](testing_reasoning.md) ·
[Requirements](prds/implementation_plan_f4o4a.md)

Select the bundled Bedrock provider, your existing session or access-key identity,
`BEDROCK_ENDPOINT=runtime` and `BEDROCK_API_MODE=converse`. Discover the exact
model/profile ID first, then restrict both model regular expressions to that ID.
Changing API mode does not grant model access or switch credentials.

## Controls

| Qualified model | Low / Medium / High |
| --- | --- |
| GPT-OSS | Native `reasoning_effort` |
| Cataloged GPT Luna, Terra, Sol and Astra versions | Native nested `reasoning.effort` |
| Claude Opus 4.1, Sonnet 4.5 and Haiku 4.5 | Configured thinking-token budgets |
| Claude Opus 4.5 | Native output effort plus the required thinking budget and effort opt-in |
| Cataloged adaptive Claude versions | Adaptive thinking plus native `output_config.effort` |

Unknown versions receive no inferred effort choices. The catalog admits `Off`
only for qualified versions that support disabling thinking. Provider default
omits an explicit effort; it does not mean thinking is disabled.

The Bedrock provider contributes three settings, visible for Converse:

- `LLM_REASONING_LOW_TOKENS=1024`
- `LLM_REASONING_MEDIUM_TOKENS=4096`
- `LLM_REASONING_HIGH_TOKENS=16384`

Presets must be nondecreasing integers of at least 1024. Manual budgets are capped
below the effective total output limit, reserving 1024 answer tokens. An explicit
total limit below 2048 is rejected for enabled budget thinking. An omitted limit
uses `LLM_MAX_OUTPUT_TOKENS`, capped by model metadata. Adaptive versions use native
effort instead of manual presets. Active Claude thinking rejects temperature.

## Runnable checks

After configuring authentication, model selection and test storage:

```bash
./launch.sh --tui-mode integration --no-std-in --start api-agent::tests/integration/test_provider_reasoning_replay.py::test_provider_reasoning_replay
```

This makes two calls using low effort where advertised and replays any native
continuation. Its evidence distinguishes display reasoning from native state;
a successful answer alone does not prove visible reasoning or greater effort.
For Studio inspection, use the reasoning test described in the
[main guide](testing_reasoning.md), or retain `test_studio_request` with `--debug`
and send your own prompt after selecting effort.

Native ConverseStream is collected with bounded content, metadata, event count
and deadlines. Large Claude thinking requests use that SDK path even for a
buffered completion. Display progress is published after native validation,
which can be after a node completes; this does not promise per-token previews.
GPT-6 Luna/Astra native JSON Schema is qualified for nonstreaming calls; streaming
uses the declared response fallback or rejects before inference. The original
schema remains validated after a permitted fallback.

Signed and opaque reasoning is preserved in order for answer and tool
continuations. Opaque binary state has a 64 KiB aggregate bound and is never
shown in Studio, logs, traces or optional evidence. Converse usage may omit a
separate reasoning-token count; missing usage remains unknown.

## Qualification limits

Live checks established GPT-OSS request/replay and Sonnet 4.5 low-budget replay.
A separate GPT-6 Luna low-effort request confirmed the nested effort carrier.
These checks do not qualify every model/version/account or guarantee a nonempty
summary. Opus 4.5 hybrid effort passed low-effort replay and low/high-effort
named-tool workflows on its Runtime US profile; this does not qualify strict
tools, answer schemas or every profile. See the [operator guide](bedrock.md).

Consult the [AWS GPT controls](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html),
[adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)
and [manual budgets](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)
when qualifying additional exact versions.
