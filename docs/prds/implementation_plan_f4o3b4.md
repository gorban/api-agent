# OpenRouter family reasoning and cache qualification

[Effort controls and output budgets](implementation_plan_f4o3b2.md) ·
[Testing guide](../testing_openrouter_reasoning.md)

Define provider-owned version defaults and qualify exact OpenRouter combinations
independently of official endpoints, without
silently ignoring configured efforts or cache controls. Preserve final-answer,
usage and credential-protection contracts.

- [x] Use provider-owned minimum supported versions for Luna, Terra and Sol (5.6),
  and Astra (6). Carry support forward to later numeric versions, with explicit
  later-version changes such as Sol 6.1 excluding Off. Map Off to native `none`;
  malformed or zero-padded numeric aliases, older generations and other backends remain unqualified.
  Narrow choices when the OpenRouter catalog explicitly reports fewer efforts,
  mandatory reasoning or no common controls; malformed metadata stays unknown.
  Keep observed restrictions provider-owned and enforce them before inference,
  including ordinary requests without an explicit output ceiling.
- [x] Use the documented OpenRouter GPT-5.6+ cache eligibility rule in the existing
  renderer, then qualify Terra 6.1 live,
  preserving the shared output clamp and configured effort. Do not add another
  cache capability table or treat inherited eligibility as measured live support.
- [x] Run fresh-prefix/repeated-prefix cache checks on Responses and Completions.
  Prove cache hits from reported counters; use a model-specific calibrated fixture
  only if its actual framing differs. Verify Off avoids explicit cache reads and
  writes where supplied. Preserve unreported raw write counters as unreported,
  and independently verify the outgoing Off mode and absence of breakpoints.
  Medium and High currently request identical 30-minute semantics; do not
  claim distinct retention or prove lifetime from a short repeated pair.
- [x] Verify the three GPT-5.6 common controls on both APIs, retaining exact model,
  effort, request acceptance and reported usage. Visible reasoning, correctness,
  tools, structured output and opaque replay require separate qualification.
- [x] Verify Studio admission with medium caching and an explicit output ceiling.
- [x] Close causal regression, exact applicable unit line/branch coverage, package,
  public-boundary and independent/external review before publication.
- [ ] Human acceptance of the selected model controls and cache behavior.

Contracts: [OpenRouter model catalog](https://openrouter.ai/api/v1/models),
[reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens),
[caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).

Version defaults establish eligibility, not exhaustive measured model behavior.
Catalog metadata may narrow inherited controls. Structured output, tool and native
reasoning display/replay qualifications remain independently scoped.
