# OpenRouter reasoning qualification follow-through

[Node summaries and replay](implementation_plan_f4o3b.md) ·
[Observed route matrix](../testing_reasoning.md#openrouter-overlap)

Reusing a model through another backend requires independent qualification.
The November 2, 2026 OpenRouter checks found a Responses continuation rejection
for `openai/gpt-6-luna` after inference. Successful discovery is insufficient.

[Effort controls and output budgets](implementation_plan_f4o3b2.md) tracks the
separate Terra 6.1 selector and Sol 6.1 reasoning-budget repair.

- [ ] Diagnose the rejected native item shape using credential-safe structural
  evidence. Admit documented OpenRouter Responses metadata without discarding,
  reordering or exposing encrypted state, or weakening unknown-field rejection.
- [ ] Prove exact returned native items survive a real two-call replay when
  supplied. Keep ordinary replay, encrypted replay and cache-hit evidence distinct.
  Cover owned tool continuations, summary/final separation and malformed state.
- [ ] Audit Completions `reasoning`, `reasoning_content` and `reasoning_details`
  separately. Preserve supported native replay or explicitly reject unsupported
  continuation; a generated explanation is not proof of native-carrier support.
- [ ] Requalify Luna Responses application-flow and optional-summary browser
  checks, retaining exact-answer and preview-before-final assertions. Preserve
  detailed failures and any usage incurred before decoder rejection.
- [ ] Diagnose the OSS20B Completions exact-answer mismatch without relaxing the
  assertion or attributing its cause from the browser symptom alone.
- [ ] Close applicable exact unit coverage, causal rejection, package,
  public-boundary and external review before claiming this route works.
- [ ] Human acceptance on a qualified OpenRouter route.

The OSS20B Completions browser probe separately returned `323` instead of the
requested `Ok` in its optional-summary fixture. This is an observed exact-answer
failure; the probe does not isolate upstream output, normalization or graph
behavior as its cause. Retain the assertion and do not label that run a pass.
Non-GPT catalog support
is outside this repair: the bundled GPT adapter currently excludes GLM5 even
though OpenRouter lists it.
