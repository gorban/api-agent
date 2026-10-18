# Interactive cache-policy acceptance

[Inference](implementation_plan_f2.md) · [Retained Studio interaction](implementation_plan_i2c2.md)

- [ ] Defer cache-policy human testing until working Run local or retained interactive Studio permits manual requests on the user's own schedule. A passed test with a retained inspection page alone does not satisfy this capability.
- [ ] Keep the interactive backend, selected provider/account and browser usable over pauses of at least 31 minutes and longer when required by the effective cache policy. Idle time must not consume a completed fixture/startup deadline; each later request gets a fresh bounded deadline and normal lazy credential renewal.
- [ ] Record an initial unique-prefix request, a matching warm repeat and a later matching request after an appropriate wait. Preserve the meaningful response assertion, model/version, reasoning, routing/affinity and other request settings. Inspect reported input/cache usage and available charge evidence rather than inferring a cache hit from response text or latency.
- [ ] Determine wait thresholds from the selected provider's actual supported policy. Omission/automatic caching may have no guaranteed retention; do not turn 31 minutes into a universal expiry assertion. Record observations and policy limitations when the backend cannot promise a hard lifetime or when eviction/routing may change reuse.
- [ ] Keep all later manual activity separate from finalized test usage/cost/Allure artifacts and avoid contaminating another running suite's quota accounting. Preserve ordinary inspection cleanup and cancellation after a long pause.
- [ ] Retain the separately recorded provider-cache failure until repaired/dispositioned; neither this deferral nor approved inference contracts claims that failure passes. No new live inference or paid run is authorized by this requirements update.
