# Shared output-token ceilings and provider limit negotiation

[Shared adapters](implementation_plan_f1.md) · [Transport recovery](implementation_plan_f1b.md)

## Requirements

- [x] Add `LLM_MAX_OUTPUT_TOKENS`, default 200,000, range 1..1,048,576. Resolve through normal application, pack and test precedence. This is one per-generation output ceiling, including reasoning where counted by the provider, not a whole-flow cumulative budget. Existing byte/frame, continuation and deadline limits remain independent resource bounds. Preserve explicit lower call budgets. Do not impose unsupported controls on transports without output-limit support.
- [x] Before dispatch, reduce the effective limit to the smallest of the configured ceiling, explicit call budget, advertised selected-model output maximum and unexpired learned maximum. Preserve exact model, endpoint, account, API and generation options. Record the effective request rather than silently claiming the requested ceiling was sent. This requirement supersedes the earlier no-clipping rule in the bounded-inference contract for these declared ceilings.
- [x] Keep catalog lookup optional for implicit configured ceilings: a provider-confirmed catalog-only authorization denial must not block a permitted selected-model inference. Use the actual provider output-limit capability to supply the configured/learned cap without inventing model metadata. Authentication failures remain failures. Preserve existing explicit-budget and cache-boundary admission; the new authorization bypass does not apply to either. Optional hints still remain unapplied when catalog metadata is unsupported or unavailable under the existing contract.
- [x] A provider may normalize a confirmed pre-inference output-limit rejection into a typed smaller maximum. Only explicit, bounded output-parameter/range evidence from a validation rejection qualifies. Arbitrary error prose, context-window exhaustion, authentication failures, 5xx, response truncation and uncertain execution do not authorize correction.
- [x] Permit one immediate correction per logical call, within its original deadline, independently of transport attempt and workflow retry budgets. Preserve distinct physical-attempt evidence and recovered attention. Repeated rejection remains terminal. Never replay tools, authentication or completed/truncated generation.
- [x] Cache learned limits within the selected provider/account/endpoint/API owner and exact model, with `LLM_OUTPUT_LIMIT_CACHE_TTL_SECONDS`, default 3,600; zero disables reuse. Bound cache growth; expire and dispose it with normal ownership. Do not share across identities/routes or increase a cap on rejection.

## Acceptance

- [x] Prove configuration precedence, Studio/request propagation, catalog reduction and unsupported-route behavior with synthetic providers.
- [x] Prove exact typed-rejection admission, one correction, unchanged transport/application budgets, accounting, expiry, isolation, cancellation and deadline behavior. Use meaningful causal rejection checks.
- [x] Complete exact applicable unit line/branch coverage, affected integration, packaged/relocated resources, public-boundary/license checks, independent and external review.
- [ ] Human review: repeat a reasoning-heavy Run local prompt at low/high effort without the previous 1,024-token default; inspect effective limits and usage. Provider access and response completion remain required, not assumed from a larger cap.

The delivered path passes full applicable unit line/branch coverage, controlled
HTTP/SDK/worker tests, installed and relocated resources, and independent and
external source review. Live provider acceptance of the larger implicit ceiling
remains the human checkpoint; no new paid inference is claimed here.
