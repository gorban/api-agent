# Model-specific prompt caching qualification

[Bedrock capabilities](implementation_plan_f4c.md) · [Provider requirements](implementation_plan_f4.md)

- [ ] Inventory prompt-cache support for each implemented provider/model/API route,
  including differences between model families. Distinguish automatic cache reuse,
  explicit cache boundaries and supported retention controls.
- [ ] Map supported `LLM_CACHE_POLICY` choices through provider-owned capabilities
  and request adaptation. Reject unsupported explicit choices actionably; do not
  infer support from another model or silently alter the selected route.
- [ ] Qualify actual cached input/write usage, retention and regional pricing without
  inventing missing counts or treating a stochastic cache miss as transport failure.
- [ ] Exercise eligible model families with an account that admits them, preserving
  selected identity and separately approved inference scope. Current OSS-model
  qualification does not establish support for other model families.
- [ ] Complete deterministic controls, applicable coverage, installed resources,
  independent/external review and explicit human acceptance before claiming support.

This follow-up does not change the currently implemented cache behavior.
