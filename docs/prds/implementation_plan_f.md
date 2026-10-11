# LLM providers, authenticated discovery, and usage tracking

Follow the [shared contracts](implementation_plan_m.md) and [external LLM adapter checklist](implementation_plan_f1.md). Core tests exercise synthetic adapters; the selected provider owns authentication protocols, cache/refresh, headers and pricing.

The [included personal GPT adapter](implementation_plan_f2.md) is early work; real public demo UI/API applications are deferred. Non-inference access adapters are separately activated and cannot serve completions. Keep their quota snapshots separate from selected GPT inference usage/cost. The application picker describes its actual inference provider, not an unrelated access-test catalog. Static API-key operation skips browser/indexed-account authentication tasks.

## Checklist

- [ ] Define typed, runtime-parsed configuration with a required value specifying where to load the LLM provider. Combine the provider's own `cfg/config.toml`, when present, with api-agent's configuration as though it were api-agent's own. Preserve the UI-selected model throughout the workflow. Review the provider configuration and authentication setup.
  - [ ] Implement authentication and connectivity through the selected LLM provider, including auth-token passthrough and support for the UI-selected model and structured output.
    - [ ] Implement provider-owned authentication transport and configuration. Core loads the configured provider, combines contributed declarations, registers optional routes and owns generic lifecycle and request-session binding. Verify protocol behavior in the provider and public integration with synthetic adapters.
- [x] Implement deterministic `src/api_agent/telemetry/usage.py`: generic CSV columns, measured-zero versus unavailable metrics, thread/process-safe append, stable prompt identities, snapshot validity/reset handling, final quota/countdown and provider-supplied cost estimation. `src/api_agent/testing/recorded.py` retains a per-run CSV and one frozen summary, even if an optional external CSV append fails. Provider hooks and actual quota requests remain below.
- [x] Add TOKEN_USAGE_CSV_ENABLED=False and retain TOKEN_USAGE_CSV as the path. When enabled and not explicitly provided, conditionally default to token_usage.csv under the selected resource pack root, or api-agent root without a resource pack. The recorded integration adapter appends only with the toggle True and a nonempty path; explicit empty disables it even with the toggle True. Per-run usage artifacts remain independent. Ignore /token_usage.csv in both repositories.
- [ ] Apply the same enabled/nonempty-path gate when wiring actual local-run and E2E consumers; preserve their required usage summaries and suite artifacts even when the cumulative CSV is disabled.
- [ ] Implement token usage recording before the first integration or E2E run, including authenticated model-discovery tests and the initial "Ok" milestone. Declare `TOKEN_USAGE_CSV` in runtime configuration and implement the CSV behavior.
  - [ ] Preserve provider/model, call count, session ID, input/output/total tokens, cache-read/cache-creation tokens, and reasoning tokens where returned by the provider. Track structured-output and streaming calls, including retries; distinguish unavailable metrics from measured zero usage.
  - [ ] Associate usage artifacts with the suite run identity and retain them with the persistent Allure results. Ensure the test runner enables recording from the first run.
- [ ] Capture LLM provider quota/spend used immediately before and after the whole integration/E2E suite execution, and report both snapshots and `after - before` as a quota/spend used delta in the TUI suite results and persisted Allure run artifacts. Do this for successful, failed, and interrupted runs where a final snapshot can be obtained.
  - [ ] Record quota resets, unavailable snapshots and possible unrelated account usage explicitly. Parallel selected tests share one suite bracket. Quota snapshots remain separate from token counts and provider cost estimation.
- [ ] After declarative config, LLM provider authentication, Allure, and usage tracking are ready, implement `GET /app/agent/models` as an early API/authentication milestone before any token-spending "Ok" test.
  - [ ] Establish the model-list schema and route/frontend contract. Preserve the response shape required for selecting a model.
  - [ ] Apply providers.selection.ModelSelectionPolicy to GET /app/agent/models
    before serialization. Preserve allowed IDs, display names, family/version
    metadata and optional token limits. Resolve the default only within that
    filtered catalog. Keep no-match/multiple-family/highest-tie errors explicit.
  - [ ] Apply the same admission guard to every public inference path before
    provider authentication/calls; bypassing the UI cannot bypass the allowlist.
    Prove forbidden models cause zero provider loading/authentication/inference
    and generic policies work with another model family.

  - [ ] Add a targeted integration test of authenticated LLM provider model discovery, plus deterministic tests of missing/invalid authentication handling. These model-list requests must not invoke LLM inference or generate a completion. Record the integration result in Allure and show its suite-wide quota delta in the TUI even when usage is unchanged.

- [ ] Track suite-wide user prompts independently of LLM call counts: initial conversation turns, clarification answer rounds, and mutation approval turns each count once. Include failed attempts and confirmation reruns; associate counts with the suite run and retain them in Allure artifacts for the [TUI summaries](implementation_plan_a.md).
- [ ] Capture provider usage snapshots immediately before and after suite execution without invoking LLM inference. Retain token counts from completion responses and optional quota percentage used and quota reset datetime in UTC. Pass the recorded counts and snapshots to the provider's cost-estimation function, then divide the returned estimate by user-prompt count when nonzero. Do not report reset-spanning or otherwise invalid quota differences as attributable consumption.
- [ ] Define the public LLM provider cost-estimation API to accept token counts and optional before/after quota snapshots, and return an estimated amount and currency or an explicit unavailable result. Keep pricing data, quota allocations, and calculation rules inside the provider. Validate the application integration with deterministic provider fixtures, including token-based estimates, quota-based estimates, missing quota fields, resets, and unavailable estimates.
- [ ] Define completion responses to include token counts and optional quota percentage used and quota reset datetime in UTC. Expose a usage-snapshot function for obtaining the optional quota values without invoking inference. Reject quota-based cost estimates when a reset or unavailable snapshot makes the difference unreliable, and avoid counting cache or reasoning tokens twice when they overlap other token totals.

## Request metadata and first outbound call

- [ ] Document request metadata from independently established provider contracts. Providers own their upstream headers and validation; core supplies generic request and trace context without interpreting protocol-specific metadata.
- [ ] Let each selected provider own request metadata across discovery, completion, streaming, retries and quota reads. Pass application/resource-pack identity and tracing context through generic services; provider declarations own any additional configuration.
- [ ] Load optional provider request-metadata declarations through contributed configuration. Review contributed defaults before the first executable provider operation; core has no provider-specific metadata defaults.
- [ ] Before live provider operations, complete human configuration review, including contributed identity, authentication and storage/tracing settings. Imports, configuration review and collection do not call providers.
- [ ] Prove attended and unattended LLM provider authentication using the [test-authentication tasks](implementation_plan_a4.md). During authentication-only integration, forbid inference and leave quota unavailable when not queried. Verify authenticated model-list response separately; an authentication token alone does not prove discovery.

- [ ] Verify request metadata within the selected provider. Public integration tests use synthetic metadata consumers and do not assert a private upstream header set.
- [ ] Verify protocol-specific metadata validation and closed-operation behavior within the selected provider. Public core verifies cancellation and runtime ownership using synthetic adapters.

- [ ] Supply application/resource-pack service identity to the selected provider through generic context. Provider-owned metadata configuration observes normal precedence; core tests verify identity selection independently of working directory or installed paths.
- [ ] Wire selected provider transports to their own metadata builders, including their separately documented usage-read contract. Complete human configuration review and authentication proof before live inference.

- [ ] Implement [provider session and operation usage reporting](implementation_plan_a7.md) with optional starting/final quota snapshots, provider-supplied cost estimates and one frozen stdout/TUI/report summary.

## Completed prerequisite before discovery

- [x] Implement auth-only common integration setup, selected-provider roles/configuration, username-bound GPT cache reuse, owned refresh/getter and observed unattended login. Verify fresh-cache DEBUG/non-DEBUG setup without inference. See [setup proof](implementation_plan_f2b.md).

Authenticated model discovery, generation, suite-wide quota/cost/token accounting and backend-owned shared authentication remain open; a verified token does not establish these capabilities.
