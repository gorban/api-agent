# LLM providers, authenticated discovery, and usage tracking

## Checklist

- [ ] Define typed, runtime-parsed configuration with a required value specifying where to load the LLM provider. Combine the provider's own `cfg/config.toml`, when present, with api-agent's configuration as though it were api-agent's own. Preserve the UI-selected model throughout the workflow. Review the provider configuration and authentication setup.
  - [ ] Implement authentication and connectivity through the selected LLM provider, including auth-token passthrough and support for the UI-selected model and structured output.
- [ ] Implement token usage recording before the first integration or E2E run, including authenticated model-discovery tests and the initial "Ok" milestone. Declare `TOKEN_USAGE_CSV` in runtime configuration and implement the CSV behavior.
  - [ ] Preserve provider/model, call count, session ID, input/output/total tokens, cache-read/cache-creation tokens, and reasoning tokens where returned by the provider. Track structured-output and streaming calls, including retries; distinguish unavailable metrics from measured zero usage.
  - [ ] Associate usage artifacts with the suite run identity and retain them with the persistent Allure results. Ensure the test runner enables recording from the first run.
- [ ] Capture LLM provider quota/spend used immediately before and after the whole integration/E2E suite execution, and report both snapshots and `after - before` as a quota/spend used delta in the TUI suite results and persisted Allure run artifacts. Do this for successful, failed, and interrupted runs where a final snapshot can be obtained.
  - [ ] Record quota-window resets, unavailable snapshots, and possible concurrent account usage explicitly rather than presenting an invalid delta as attributable suite consumption. This quota/spend metric is separate from per-call token counts.
- [ ] After declarative config, LLM provider authentication, Allure, and usage tracking are ready, implement `GET /app/agent/models` as an early API/authentication milestone before any token-spending "Ok" test.
  - [ ] Establish the model-list schema and route/frontend contract. Preserve the response shape required for selecting a model.
  - [ ] Add a targeted integration test of authenticated LLM provider model discovery, plus deterministic tests of missing/invalid authentication handling. These model-list requests must not invoke LLM inference or generate a completion. Record the integration result in Allure and show its suite-wide quota delta in the TUI even when usage is unchanged.
