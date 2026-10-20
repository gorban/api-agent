# Optional schemas and message cache boundaries

The included GPT adapter preserves caller-selected schema strictness through Responses and Chat Completions. Strict mode remains the default. Explicit non-strict mode preserves optional fields, local definitions/references and supported composition without converting them into required fields. Validate completed structured output and tool arguments locally against the original schema; reject invalid requests before transport and preserve reported usage when completed output fails validation. Remote schema resolution is forbidden.

Actual API and subscription paths await cancellable replay and response validation under the [bounded generic contract](implementation_plan_f1a.md). Synchronous factory preflight admits bounded structure and schemas; async execution validates original replayed arguments before allocating transport. Validation cancellation kills and reaps its owned worker before completion.

## Acceptance

- [ ] Prove explicit strict=false mapping and unchanged strict defaults with synthetic controlled transports for both API formats.
- [ ] Prove optional fields, local references and composition validation, including safe failures and preserved usage.
- [ ] Accept bounded optional per-message cache boundaries with 300/3600-second requested lifetimes. Routes without verified support ignore hints, preserve prompt bytes and actual usage, and report no applied boundaries. Automatic upstream caching may still occur. Existing request cache policies retain their separate semantics.
- [ ] Respect advertised output limits without silent clipping, funding changes or credential/API-mode fallback. Subscription output limits remain unsupported.
- [ ] Verify controlled streaming, transport cleanup and credential privacy; independent review and the repository gate remain required.
- [ ] Human acceptance remains pending; this work authorizes no live request.

Public contracts: [function strictness](https://developers.openai.com/api/docs/guides/function-calling), [structured output](https://developers.openai.com/api/docs/guides/structured-outputs).
