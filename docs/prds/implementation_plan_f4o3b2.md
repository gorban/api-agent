# OpenRouter effort controls and output budgets

[OpenRouter follow-through](implementation_plan_f4o3b1.md)

Reasoning controls and output budgets are independent capabilities. An advertised
effort must not leave a known reasoning-capable route limited to the text
adapter's small fallback budget.

- [x] Qualify the common `off`, `low`, `medium` and `high` controls for
  `openai/gpt-6.1-terra` on OpenRouter. Map `off` to the documented `none` value.
  Keep official-route and unfamiliar-model knowledge independent.
- [x] Advertise output-limit support for admitted OpenRouter selections when
  their catalog declares a supported token-limit parameter and a finite maximum.
  Preserve existing Luna compatibility and conservative missing/malformed metadata
  behavior. Do not infer reasoning effort support from output-limit support.
- [x] Reuse the shared configured output-limit clamp: the application default
  remains 200,000, reduced by an explicit request, catalog maximum or learned
  provider ceiling. Do not silently raise explicit caller limits or add a special
  retry after reasoning exhausted a paid output budget.
- [x] Prove the Sol 6.1 medium-effort puzzle failure at the old 128-token cap,
  then verify bounded puzzle completion through the Studio HTTP/browser path and
  its retained DEBUG provider. Preserve final-answer assertions and usage.
- [x] Verify exact unit line/branch coverage, causal controls, package/public
  boundary and external review before human acceptance.
- [ ] Human acceptance of Terra controls and the Sol medium-effort puzzle.

The broader native-continuation and exact-answer findings remain in the parent;
this repair does not qualify those unrelated failing paths.

## Verification

The canonical 23,778-test unit gate passes at exact 100% line/branch coverage.
Three causal controls, source/archive/relocated-package and external boundary
review pass. The unchanged strict-Ok Studio test passes before DEBUG retention;
subsequent manual Sol/Terra medium puzzle turns complete after suite publication,
with enabled controls observed in the original browser and clean owned shutdown.
These prove completion, not puzzle correctness. Manual turns remain separate
from finalized Allure accounting. Human acceptance remains unchecked.


## Cache-policy catalog admission follow-through

- [x] Derive the catalog cache-policy capability from the existing provider-owned
  cache renderer, including known compatible backends. Do not duplicate model
  allowlists or admit unknown model policies. Preserve explicit output ceilings.
- [x] Verify the Studio request with configured medium caching and an explicit
  catalog-clamped output limit through the selected compatible backend. The
  normal Studio integration test and Allure publication pass.
- [x] Renew applicable coverage and independent/external source review: 24,081
  unit tests pass at exact 100% line and branch coverage. Both positive and
  unknown-route mutation controls detect incorrect capability admission; wheel,
  source-archive rebuild without Git and relocated installed checks pass.

A missing cache-policy feature rejected otherwise supported Studio requests
before transport when an explicit output ceiling enabled full catalog validation.
Standalone requests without an explicit ceiling did not exercise that gate.


[Additional family qualification](implementation_plan_f4o3b4.md) covers the
GPT-5.6 reasoning catalog gaps and Terra 6.1 cache presets separately.
