# Local-run authentication and provider contract

## Provider authentication handler integration

- [ ] Implement selected-provider authentication handlers through generic route registration. Providers own their authentication protocols, transactions, cookies and completion behavior; core owns runtime routing, generic request binding and lifecycle. Verify public integration with synthetic providers and establish UI contracts independently.

## Checklist

- [ ] Verify generic authentication contracts independently: upstream API extensions own credential expiry and validation; the selected LLM provider owns its authentication configuration, sessions and routes. Public handlers and live flows remain to be verified.

- [ ] Review the public provider authentication contracts and unattended authentication integration points. The LLM provider owns its authentication sessions and routes; upstream API-provider extensions own signing, login hints and required identity validation. Implement and verify the handlers and authentication behavior described below.

- [x] Provide pluggable authentication extensions. The api-agent and UI renderer must work without resource-pack-specific API credentials. The selected LLM provider owns its authentication, token/session handling and optional route setup. Selected upstream API-provider extensions own additional authentication, request validation and optional routes. Keep identities isolated and verify standalone and resource-pack isolation deterministically.

- [ ] Verify dependency compatibility independently for each selected LLM provider. Protocol validation and signing-test dependencies belong to that provider; core integration tests use synthetic providers.

- [ ] Keep bounded request-session ownership in core and provider authentication transactions and token ownership in the selected LLM provider. Verify expiry, cancellation, coalesced refresh and principal isolation through generic provider integration tests and provider-owned protocol tests.
  - [x] Implement generic bounded request-session ownership in `src/api_agent/auth/session.py`: opaque scoped identifiers, principal isolation, capacity, expiry and disposal. Provider authentication transactions and tokens are outside this store. Browser continuity and provider authentication remain separate integration work.
  - [ ] Define the UI cookie/proxy and attended challenge contract independently. Provider completion handlers own their bound authentication transactions. Keep browser credential retrieval explicitly enabled and bound to the initiating session; isolate cookie names across resource packs and callback origins.
  - [ ] Bind an owned unattended browser context explicitly to the initiating agent session. On authentication cookie rotation, atomically migrate ownership and return the replacement cookie to the original API client as well as the browser callback. Test concurrent in-flight requests, changed principals and newly arriving invalidated-cookie requests; a successful browser callback alone must not strand the original client.

- [ ] Local-run does not invoke test-runner authentication preflight. With RUN_UI=True, create a headed UI session and automatically dispatch the selected pack's optional authenticate hook before user handoff; with RUN_UI=False create neither UI session nor hook. On-demand LLM authentication uses a separate lazy headless session. See [approved browser contracts](provider_architecture.md).
- [ ] Define the authentication contract with the selected LLM provider and UI renderer using independently authored requirements and public upstream documentation. The provider supplies its authentication implementations, configuration declarations, and optional `cfg/config.toml`; combine them with api-agent configuration through the shared type, applicability, precedence, and documentation pipeline. Bootstrap the required provider location before resolving the combined configuration.
  - [ ] Verify the configured provider completion routes, HTTPS, authentication exchange and session behavior in the selected deployment. Provider configuration owns any required external registration; do not infer it from earlier application verification.
- [ ] Define generic provider setup hooks for authentication operations and route registration. After resolving configuration, call the provider to register any required start/status/callback handlers on the main agent through a shared route registrar. Provider implementations own their handler logic and protocol details. Define generic authentication challenge responses and stream events with the UI renderer, and reject incompatible configuration declarations or conflicting method/path registrations.
- [ ] Initialize selected LLM provider authentication lazily when an authenticated operation needs it, using the selected LLM-user index and separate headless browser. Resolve a fresh opaque token for every provider call. Keep attended fallback and resource-pack UI authentication separate; see [provider sessions and usage](implementation_plan_a7.md).
  - [ ] Enable this browser automation only for explicitly enabled local unattended execution. Keep the same public attended authentication challenge outside that mode; neither hostname nor debug configuration enables it. Health probes, OPTIONS and provider completion callbacks never initiate unattended login, and explicit authorization-start handling cannot recursively invoke the broker.
  - [ ] Ask the provider to reuse or refresh a valid session first. When browser login is required, let the provider initiate its documented authorization/callback flow and drive the observed sign-in branches using shared credential helpers. Verify completion and usable authentication state before continuing the request. Follow the implementation-time [Playwright MCP walkthrough](implementation_plan_a4.md) for browser-based flows.
  - [ ] Keep authorization state, cookies, and token/session ownership bound to the initiating identity. Coalesce concurrent login needs for the same session rather than creating a browser for every status poll/request; do not share tokens across users. Never mix the pack's UI login cookies with LLM provider session state.
  - [ ] Bound login/refresh attempts and preserve the attended frontend contract when credentials are absent/cleared or the automatic attempt fails. Successful automatic authentication proceeds without first emitting a frontend auth challenge. Resume/retry only operations that have not executed; never blindly replay an already applied mutation/approval after a late authentication failure.
- [ ] The unused AUTH_BROWSER_DEBUG declaration is removed. Implement shared non-E2E debug retention with the approved policy: the unattended LLM browser stays headless even in debug. Debug retains every created browser after provider disposal; UI visibility follows mode/RUN_UI/E2E_DEBUG. Remove conflicting headed-LLM semantics during implementation rather than maintain duplicate policies.
- [ ] Dispatch optional resource-pack authentication for every newly created local UI session; do not add a separate Authenticated UI action. The pack owns login strategy and may delegate cookie preparation to its upstream extension. An absent/explicit no-op hook is valid; malformed hooks and preparation failures are explicit errors.
  - [ ] Supply the ordinary mode, resolved account index or None for attended use, role-bound credentials and the actual BrowserSession. Hand the same context and updated active page to the user, with no background navigation left behind. Keep LLM login state separate. Ordinary non-session links may still use the host browser.
- [ ] Capture current-state all-tab diagnostics on local authentication errors through the shared browser-evidence handler before cleanup. Local-run artifacts/logs stay outside executed-test Allure results; test authentication errors continue to attach to Allure. No credentials are written into diagnostics.
- [ ] Add deterministic/controlled-page tests for the public provider authentication contract, lazy discovery/action login, concurrent requests, refresh success/failure, attended fallback, identity isolation, and prevention of mutation replay. Verify pack browser handoff and the approved visibility/retention policy without requiring a live LLM or a full E2E suite.

## Shared implementation destinations

```text
src/api_agent/
├── auth/
│   ├── provider_auth.py          # Provider-neutral authentication orchestration
│   └── browser_login.py          # Generic cancellation/credential support; login steps belong to adapters
└── runtime/resource_pack.py      # Optional per-session UI authenticator declaration
```

## Identity extension contract

This describes the existing pack identity hook. As selected API providers are composed, bridge any pack request-identity capability to the selected upstream extension without moving its protocol into core. The extension owns required upstream signing/validation; see [external API tasks](implementation_plan_m2.md).

`pack.toml` may declare `[authentication.identity]` with `version = 1`, a pack-relative Python `path` and a synchronous `factory` name. Discovery validates without importing; explicit application composition calls the factory with immutable resolved configuration. Its version-1 adapter supplies async `resolve_request`, `login_hint` and `match_llm_identity`. Per-load module registration isolates concurrent composition without altering `sys.path`. Declare pack production directories in `production` so unit coverage measures executable pack code, including unselected owners. A declared adapter must be inside those directories or the owner's normal `src` tree.

- [ ] When adding HTTP transport, give its client explicit lifespan ownership and close/reap it during Runtime shutdown, including partial-startup and failure tests; callable injection alone is not a transport lifecycle.

The deterministic owner exposes async `aopen` for consumers that may retire active work; it awaits owned cleanup before returning. Operations remain bounded including retiring work, and stable flight ownership survives cookie rotation so replacement-cookie callers join existing work. Canceling one valid waiter still leaves other valid waiters intact.

- [x] Strengthen deterministic ownership before handler consumers: retirement cancels owned auth work, async replacement drains cleanup even with free capacity, rotating cookies retain flight identity, retiring operations count toward bounded capacity, and shutdown drains both active and retiring tasks. Verify these with controlled async events and fake clocks, without a real provider/browser.

- [x] Keep retired-owner cleanup independent of opener/shutdown waiter cancellation. Shield owned operations while draining; cancel a retiring operation only once, hold session/operation reservations until its finally cleanup finishes, and defer replacement-session allocation until after cleanup and ownership revalidation. Reinspect retirements added during earlier cleanup, including TTL expiry; disarm pending operation deadlines on retirement and avoid recanceling timeout-started cleanup. Verify repeated canceled waiters, spare capacity, concurrent shutdown, both deadline orderings and post-await cookie invalidation.

- [x] Shield shared Runtime startup from cancellation of its awaiters; only owned shutdown cancels and drains startup. Repeated cancellation cannot interrupt an owner's awaited finally cleanup, and concurrent startup waiters share one startup task. Verify with gated asynchronous cleanup and substantive cancellation regressions.

- [ ] Let the selected LLM provider validate completion route configuration and own authorization transactions. Core binds requests and browser authentication to the initiating session without interpreting provider protocol state.
