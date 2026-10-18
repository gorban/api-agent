# Interactive cache-policy acceptance

[Inference](implementation_plan_f2.md) · [Retained Studio interaction](implementation_plan_i2c2.md)

- [ ] Defer cache-policy human testing until working Run local or retained interactive Studio permits manual requests on the user's own schedule. A passed test with a retained inspection page alone does not satisfy this capability.
- [ ] Keep the interactive backend, selected provider/account and browser usable over pauses of at least 31 minutes and longer when required by the effective cache policy. Idle time must not consume a completed fixture/startup deadline; each later request gets a fresh bounded deadline and normal lazy credential renewal.
- [ ] Record an initial unique-prefix request, a matching warm repeat and a later matching request after an appropriate wait. Preserve the meaningful response assertion, model/version, reasoning, routing/affinity and other request settings. Inspect reported input/cache usage and available charge evidence rather than inferring a cache hit from response text or latency.
- [ ] Determine wait thresholds from the selected provider's actual supported policy. Omission/automatic caching may have no guaranteed retention; do not turn 31 minutes into a universal expiry assertion. Record observations and policy limitations when the backend cannot promise a hard lifetime or when eviction/routing may change reuse.
- [ ] Keep all later manual activity separate from finalized test usage/cost/Allure artifacts and avoid contaminating another running suite's quota accounting. Preserve ordinary inspection cleanup and cancellation after a long pause.
- [x] Disposition short cold/warm cache-reuse acceptance using recorded successful evidence. Preserve individual failed runs as failures; their existence does not block this acceptance. Long-idle policy review remains separate. No new live inference or paid run is required.

## Diagnostic procedure for future misses

- Freeze the two owned requests before judging reuse. Compare the selected model and version, reasoning effort, effective cache policy, message content and ordering, and routing affinity. On subscription routes, verify that the JSON `prompt_cache_key` and HTTP `session-id` remain the same across both calls and any newly allocated clients; on API-key routes, verify that subscription affinity is absent. Compare the serialized request bodies without recording credentials or prompt text in reports.
- Preserve the raw reported input, cached-input and cache-write token counts for both responses, including an upstream zero. Verify the first request was cold and both responses processed the question. Keep the required repeated-call threshold as a failing acceptance check when the backend reports zero or insufficient reuse; do not synthesize cached tokens from identical text, response latency, or a successful answer.
- If the selected endpoint supports prompt-cache diagnostics, compare the second response with the first response ID and record the classified reason. Check the endpoint contract before adding diagnostics to a live request. An eligible prefix and stable affinity improve reuse but do not guarantee that a matching machine-local cache entry is available. Record unresolved upstream misses as failures with the request settings and usage needed to reproduce them.

## Accepted short cache reuse

The user accepts recorded cold/warm reuse of 0 then 3,840 cached tokens out of 4,045 input tokens as sufficient. This accepts demonstrated reuse without requiring every live run to hit. Per-run processing, cold-prefix and reuse assertions remain unchanged, including genuine failures. Backend-specific long-idle/expiry review above remains deferred.
