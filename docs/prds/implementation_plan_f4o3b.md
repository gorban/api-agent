# Optional node summaries and replayable reasoning

[Progressive display reasoning](implementation_plan_f4o3a.md)

Displayable reasoning, generated explanations and opaque continuation state are
separate contracts. A generated explanation cannot reveal encrypted reasoning or
prove that the provider used reasoning tokens.

- [x] Preserve supported native reasoning continuation items, including ordinary
  completed Responses answers, through ordered workflow histories. Validate
  provider ownership and retain original item ordering. Keep opaque state out of
  logs, traces, UI and generated explanations. Reject unsupported replay shapes
  explicitly; do not silently discard state needed for tool continuation.
  Include admitted Responses plaintext reasoning carriers and completed non-tool
  Converse signed blocks. Keep redacted Converse blocks and signed tool
  continuations explicitly unsupported until separately qualified.
- [x] Add an optional per-node display-summary declaration. Existing nodes keep
  their behavior. Resolve native availability before dispatch, keep the resolved
  request schema stable across retries, and never retry merely because a
  particular response contains no reasoning. Prefer qualified native display
  reasoning; permit a bounded generated progress explanation otherwise.
- [x] Validate any generated summary envelope using the existing response-mode
  and fallback machinery. Preserve the original node result schema and exact
  provider reply in replay history. Explanations remain distinct from native
  reasoning and never replace continuation state.
- [x] Add an independently authored demonstration graph and deterministic
  integration checks for native, generated and unavailable summaries, tools,
  retries, malformed output, streaming and buffered delivery.
- [x] Add a dedicated browser E2E for the demonstration graph: observe nonempty
  progress before the final response, wait for terminal answer replacement, then
  expand response details and verify that the earlier text remains present.
  Keep default-application E2E coverage separate. Record turn screenshots through
  normal UI reporting.
- [x] Link testing guidance from README: reproducible GPT/Luna and Bedrock/open
  model examples, qualified endpoint/API combinations, expected native versus
  generated behavior, cache/replay checks, and honest unsupported/unknown cases.
  Do not require every catalog model to produce reasoning.
- [x] Close applicable exact unit coverage, causal rejection, installed/package,
  public-boundary and external review gates before human review.
- [ ] Close the newly observed [OpenRouter Responses qualification
  failure](implementation_plan_f4o3b1.md). Earlier qualified routes retain their
  evidence; they do not establish parity across compatible backends.
- [x] Human acceptance: available reasoning preview, final-answer replacement
  and retained response details approved on November 2, 2026.
- [ ] Human acceptance of the provider/model testing guide; model/API
  qualification remains separately scoped.

## Verification and limits

The final unit gate passes 23,757 tests at exact 100% line and branch coverage.
Browser qualification covers the dedicated graph with GPT/Luna Responses and
Bedrock OSS20B/GLM5 Completions; the existing application-flow E2E remains
separate. Eight causal controls, package/source-archive rebuild, relocated
installation, public-boundary and external PR review pass.

The observed GPT and Converse two-call conversations supplied no native state;
exact encrypted and signed replay has controlled coverage rather than positive
live qualification. One Runtime Responses Luna attempt was account-access
denied. No claim of universal model support or prompt-cache hits follows from
these checks. See the [testing guide](../testing_reasoning.md) for reproducible
checks, declared policies and unsupported Converse cases.
