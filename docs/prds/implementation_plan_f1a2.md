# Provider capability discovery and fallback

[Declarative output modes](implementation_plan_f1a1.md) · [Bounded inference](implementation_plan_f1a.md)

This future work extends the existing pre-dispatch response-policy selection. It does not change the current `structured | reject` guarantee or imply that an API-compatible endpoint supports every model or schema.

## Requirements

- [ ] Discover available output mechanisms from trustworthy provider-specific capability metadata when available. Scope evidence to the effective endpoint, API, model, route and relevant schema/features; account for account access and routed upstream differences. Distinguish verified, unsupported, unknown and expired evidence. Unknown is not native support.
- [ ] Cache discovered capabilities with a configurable lifetime. Define appropriate cache keys, storage, invalidation and concurrency, including changed model, provider routing, credentials or relevant request features. Avoid storing credentials or confidential schema/prompt content. Specify the default and behavior for disabled, stale, conflicting or unavailable metadata before implementation.
- [ ] Support optional bounded preflight inference probes where metadata is insufficient, with explicit configuration, cost and usage accounting, deadlines, privacy and normal selected-identity ownership. Do not probe automatically when disabled or treat a probe as a guarantee about future routed requests. Require appropriate authorization for paid inference; preserve failed probe evidence and make its scope and limitations visible.
- [ ] When a provider can positively classify an upstream rejection as occurring before inference, permit a weaker supported response mechanism only when the declaration explicitly allows fallback. Define provider-specific evidence and safe error classification; ordinary authentication, permission, bad-request or ambiguous failures must not be treated as proof. Never change provider, account or requested schema, and never automatically downgrade after a request that may have executed. Preserve the original schema for local response validation.
- [ ] Record requested and actual modes, capability source and freshness, fallback reason, physical attempts, timing and known or unknown usage/cost without leaking credentials. Respect inherited deadlines and retry limits; fallback must not consume application-defined graph retry counters or silently omit a potentially billable attempt. Decide how recovery is represented in reporting consistently with existing retry semantics.

## Acceptance

- [ ] Test metadata success, missing/unknown/stale results, configurable expiry and invalidation, independent identities and routes, concurrent lookups, optional probes and their disabled/error paths, and selected-account preservation.
- [ ] Test confirmed pre-inference rejection and permitted fallback, `reject` policy, ambiguous and possibly executed errors, authentication failures, deadlines, cancellation, local schema validation and per-attempt evidence. Use causal controls to prove an unsafe downgrade or lost accounting fails.
- [ ] Complete applicable unit and integration coverage, source and installed/relocated checks, independent and external review, and separately record human acceptance. Real provider probes require the appropriate setup and inference authorization.
