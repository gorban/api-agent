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

The reasoning-contrast test needs separately reported reasoning-token counts;
the current Converse adapter does not expose those counts. An unqualified route
is a configuration skip, not a passed comparison. Do not enable
`LLM_REASONING_TEST_TOKEN_REPORTING` merely because reasoning is visible.

The Studio selection test also skips if its two exact model choices are absent.
For an account and allowed-model filter admitting both of these US profiles,
configure the following test-specific settings in `.env`:

```dotenv
LLM_STUDIO_TEST_DEFAULT_MODEL=us.openai.gpt-6-luna
LLM_STUDIO_TEST_SELECTED_MODEL=us.openai.gpt-6.1-terra
LLM_STUDIO_TEST_DEFAULT_EFFORT=low
LLM_STUDIO_TEST_SELECTED_EFFORT=high
```

These model IDs are exact strings, not regular expressions. The test still
validates both choices against the selected catalog and allowed-model filter.
Malformed choices and actual provider/authentication/execution errors fail.

Native ConverseStream is collected with bounded content, metadata, event count
and deadlines. Large Claude thinking requests use that SDK path even for a
buffered completion. Display progress is published after native validation,
which can be after a node completes; this does not promise per-token previews.
GPT-6 Luna/Astra native JSON Schema is qualified for nonstreaming calls; streaming
uses the declared response fallback or rejects before inference. The original
schema remains validated after a permitted fallback.

Signed and opaque reasoning is preserved in order for answer and tool
continuations. The `us.openai.gpt-6-luna` profile has reported rejecting plaintext
reasoning as assistant input. The adapter retains that original
response privately and displays its summary, but omits unsigned reasoning text
from replay requests only when the message contains no signed or encrypted
state. An entire signed/encrypted message remains unchanged, including any
surrounding unsigned content. Other profiles/models retain exact replay;
mixed-carrier adaptation and expansion to other models remain unqualified. This
does not promise identical cache prefixes for unsupported display fields.
Opaque binary state has a 64 KiB aggregate bound and is never
shown in Studio, logs, traces or optional evidence. Converse usage may omit a
separate reasoning-token count; missing usage remains unknown.

## Qualification limits

Live checks established GPT-OSS request/replay and Sonnet 4.5 low-budget replay.
A separate GPT-6 Luna low-effort request confirmed the nested effort carrier.
An operator's GPT-6 Luna high-effort two-call probe also confirmed exact replay
of returned encrypted `redactedContent`. The reported unsigned-text replay
repair's original integration test now passes in the operator's retest. The
structural probes themselves supplied text-only and encrypted-only responses,
so they do not independently exercise the unsigned-text adaptation.
These checks do not qualify every model/version/account or guarantee a nonempty
summary. Opus 4.5 hybrid effort passed low-effort replay and low/high-effort
named-tool workflows on its Runtime US profile; this does not qualify strict
tools, answer schemas or every profile. See the [operator guide](bedrock.md).

Consult the [AWS GPT controls](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-6-luna.html),
[adaptive thinking](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-adaptive-thinking.html)
and [manual budgets](https://docs.aws.amazon.com/bedrock/latest/userguide/claude-messages-extended-thinking.html)
when qualifying additional exact versions.
