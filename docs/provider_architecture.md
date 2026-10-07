# Pluggable API and LLM providers

This is the current provider architecture plan. It supersedes earlier plan wording about provider ownership, UI authentication invocation, and browser lifetime.

## API providers

- [ ] Keep api-agent independent of the upstream API type. Select an API provider through a required configured factory location, with no implicit provider or private-path default.
- [ ] Include the public GraphQL example under `api-providers/graphql-api-provider/`, with its importable Python package `graphql_api_provider`. It supplies generic GraphQL transport, schema and execution behavior; other API types can supply their own implementation of the API-provider contract.
- [ ] Require a separate upstream-provider factory location for the GraphQL example. Its selected upstream implementation supplies authentication and any extra validation. Run validation before upstream execution and pass only the declared generic authentication context. Provider-specific identity rules remain in that extension.
- [ ] Define versioned capability contracts and validate configured factories before starting the application. Load the required provider locations first, then combine optional provider `cfg/config.toml` declarations with base configuration using the shared type, applicability, precedence, help and documentation pipeline. Reject incompatible declarations; never infer a private provider location.
- [ ] Allow both LLM providers and upstream API-provider extensions to contribute optional `setup_routes(registrar, services)` capabilities. Register handlers once at startup after resolving configuration and before serving requests. Reject method/path conflicts. Routes must be available for attended authentication and recovery even when automatic authentication has failed. Handler logic and protocol details belong to the provider. Route setup must not depend on a successful unattended login.

## UI authentication

- [ ] Each E2E attempt uses the real UI and a fresh worker-owned Playwright browser session. Before yielding that same session to the test, call the selected resource pack's optional `authenticate` capability. Missing capabilities and explicit no-op implementations are valid; a failed hook prevents the test body from starting.
- [ ] Supply the ordinary `tui_mode` enum, the browser session, a resolved unattended account index or `None` for attended use, and a credential callback accepting the declared user role and requested credential kind. Do not require a scenario or identity-mode argument. Resource packs choose their normal strategy from mode and account selection and own specialized authentication tests.
- [ ] Local-run with `RUN_UI=True` uses one visible UI session and dispatches that hook before handing over the UI. Local-run with `RUN_UI=False` creates no UI session. Unit/integration execution never requires a UI regardless of `RUN_UI`. E2E always requires `UI_SOURCE_DIR`; `RUN_UI=False` changes E2E visibility, not UI participation.
- [ ] E2E UI sessions are headless only when neither `RUN_UI` nor debug is enabled. Apply the existing E2E debug setting to this policy. Debug retains every Playwright browser, including failed setup attempts, until explicit inspection cleanup. Keep the owning worker/driver alive or supervise retained resources so inspection is possible.
- [ ] Keep Playwright objects inside the process/event loop that created them. Send credential requests through generic services when crossing process boundaries; never serialize browser objects between workers and the agent runtime.

## LLM authentication and usage

- [ ] Support an optional unattended LLM authentication capability. When an authenticated provider operation first needs it, create a separate headless Playwright session and call the provider with the resolved unattended account index and a credential callback bound to the LLM User role. Coalesce concurrent initialization. Providers without this capability need no authentication browser.
- [ ] Return a callable `LLMAuthSession` with asynchronous token retrieval and `aclose()`. Before each authenticated provider operation, retrieve a fresh opaque token and pass it through unchanged. The provider owns token format, expiry and refresh. Pure local operations such as cost arithmetic do not initiate authentication.
- [ ] Share that session across the selected agent runtime, including an E2E suite or local-run. Keep UI cookies and LLM cookies separate. Do not dispose the shared LLM session at the end of an individual UI test. Stop provider tasks and dispose the session at runtime shutdown; token calls after disposal fail explicitly.
- [ ] Keep the LLM browser headless even in debug. Debug retains it too, after provider work is disposed at runtime shutdown. Explicit inspection cleanup finally closes retained browsers, contexts and drivers. Ordinary runs close resources automatically, including on cancellation and partial initialization failure.
- [ ] Allow LLM completion responses to report token counts and optional quota percentage used and quota reset datetime in UTC. Define a provider cost-estimation capability accepting these counts and optional before/after quota snapshots, returning an estimate with currency or an explicit unavailable result. Pricing and quota arithmetic belong to the provider.
- [ ] Resolve only the credential roles consumed by an operation. Validate configured indices across those roles; use attended fallback for empty arrays and reject noninteractive ambiguity. Retrieve passwords only when needed and fresh TOTP values at the challenge. Keep credentials and opaque tokens out of ordinary logs and artifacts.

## Validation

- [ ] Use synthetic providers to verify optional/no-op/invalid capabilities, configuration and route collisions, failed authentication before a test, identical pack/test sessions, worker ownership, lazy coalesced LLM initialization, fresh token retrieval, identity isolation, cancellation, disposal and all visibility/retention combinations. Verify cost estimates with deterministic usage fixtures. Verify the configured real UI with Playwright for E2E.

Executable local API operations require a non-defaulted `API_LISTENER_URL` with an HTTPS listener authority. Configure it independently of provider completion routes and external registration. Providers receive a verified TLS context through generic services.

Core usage accounting retains token counts and optional quota snapshots. A provider-supplied cost estimator accepts usage rows and before/after snapshots; absent estimates remain unavailable. Quota percentage change is displayed separately from estimated currency, and per-prompt cost requires a nonzero prompt count.
