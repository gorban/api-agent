# Pluggable API and LLM providers

This is the authoritative boundary and browser-session requirements document. It supersedes conflicting wording in earlier checklists. The [version-1 foundation](provider_contracts_v1.md) records current exports and owners; its consumer surface remains provisional until [contract hardening](implementation_plan_m3.md). Follow the [shared provider task list](implementation_plan_m.md), [included GraphQL tasks](implementation_plan_m1.md), [external API extension tasks](implementation_plan_m2.md), and [LLM adapter tasks](implementation_plan_f1.md).

## Responsibility boundaries

| Component | Responsibility | Scope |
| --- | --- | --- |
| api-agent core | Typed configuration, provider loading, routing, credential broker, mode/account selection, browser allocation, evidence, usage aggregation and cancellation/cleanup | Selected runtime and credential binding |
| Included `api-providers/graphql-api-provider/` | Generic GraphQL transport, schema evidence, operation execution and upstream extension interfaces | Selected API provider |
| External upstream API extension | Authentication, signing, request/identity validation, upstream configuration and optional routes | Selected extension; no private default in core |
| Included personal GPT provider or replaceable external LLM provider | Discovery, inference, streaming, structured output, upstream metadata, optional authentication, usage and pricing | Selected inference provider; [early GPT task](implementation_plan_f2.md) |
| Optional external access adapter | Non-inference discovery/usage checks, scoped authentication and routes | Separately activated access operation; no completion/streaming capability |
| Optional UI provider | Frontend launch plan, authoritative proxy configuration, instance-owned readiness and process lifecycle | Selected pack and operation |
| Resource pack | Knowledge/navigation/prompts, scenario tests, UI authentication strategy and optional credential roles | Selected pack; UI hook once per newly created session |

Core is independent of the API type. A provider at a different path may implement REST or another API without accepting GraphQL queries. The included GraphQL package is `graphql_api_provider`; it requires a separately selected upstream extension. Product-specific code and examples belong to their external owners. Public tests use synthetic adapters, identities and API data.

Core owns browser resources operationally. A provider exclusively uses its LLM session until disposal finishes; a pack exclusively prepares its UI session before handing it to the test/user. Login pages, selectors, signing, cookie contents, protocol transactions and token interpretation remain outside core. Core request-session storage contains opaque request ownership, not provider tokens.

## Implementation phases

Contract hardening and synthetic consumer proofs come first, then the included personal GPT provider and external API/UI/access consumers. Real public demo UI/Nx applications, demo GraphQL servers and demo non-GraphQL providers are deferred. The generic GraphQL adapter library remains needed for external API consumers; it is not a demo server. A synthetic REST fixture proves replaceability without implementing a production demo provider.

Static API-key LLM providers require neither browser login nor credential-array selection. An absent or explicit no-op authentication hook is valid; configured keys are applied by that provider per request. API caller identity and unrelated LLM service credentials remain separate. Optional access adapters must not force selection of an inference provider just to test authentication/discovery.

The included GPT provider contributes its own `GPT_AUTH_MODE`, `GPT_API_MODE`, key, endpoint and applicable OAuth settings through provider-local TOML. Its approved matrix accepts API-key/completions, API-key/responses and eligible subscription/responses; subscription/completions fails before side effects. Conditional credential requirements and wire-format translation stay inside that provider, not generic core. See [the concrete GPT tasks](implementation_plan_f2.md).

## Selection, configuration and routes

`LLM_PROVIDER`, `API_PROVIDER` and `UI_PROVIDER` select Python-file factories (`path.py:factory`). The loader accepts synchronous factories returning an integer `version = 1`, exposes lifecycle/optional route hooks, and reads adjacent `cfg/config.toml` without importing the factory during schema discovery. Factories are instantiated at awaited runtime startup. The protocol and owner foundation does not complete external adapters or operation consumers.

- [ ] Consume the optional `API_PROVIDER` and `UI_PROVIDER` selections and an API-contributed `UPSTREAM_API_PROVIDER` at executable operation boundaries. The selections/loader are available; the included GraphQL provider and actual operation wiring remain tasks. Require each only for an operation using that capability; the initial demo renderer does not need an upstream API.
- [ ] Resolve relative factory locations against a declared configuration owner root, independent of CWD. Support bundled and external locations and provider-local imports/dependencies without changing global `sys.path`.
- [ ] Bootstrap selected locations before full option validation, then merge all selected providers' optional typed declarations through the same defaults, applicability, explicit override, dotenv, ENV, CLI, help and TUI pipeline. Reject incompatible declarations and recursive selection; no unselected provider is imported or configured.
- [x] Resource packs may list ordered `providers.configs` declaration paths in pack.toml. Compatible provider declarations take precedence over pack defaults and appear as pack_default in the TUI; .env, ENV and CLI remain higher. Resolve these paths from the pack root without importing executable providers. Selected LLM file paths resolve against the configuration owner, and declared factory defaults participate in schema discovery.
- [ ] Let providers/packs contribute credential roles and optional `UNATTENDED_USER_INDEX`, including an LLM-only role. Bare core/demo does not declare an account index. Share one index across the roles consumed by an operation; do not require every declared role for every mode.
- [ ] Invoke optional `setup_routes(registrar, services)` once per runtime, after configuration and before serving. LLM and upstream API extensions may both supply it. Reject method/path collisions with core or another provider. Routes remain available without successful automatic login; route setup is separate from browser hooks and token retrieval.
- [ ] Keep executable listener configuration (`AGENT_DJANGO_URL`, HTTPS authority) independent of provider callback/registration settings. Supply a verified TLS context and generic lifecycle, authentication, credential and privacy services; never infer protocol endpoints in core.

## Browser and authentication contracts

The protocol shapes exported from [contracts.py](../src/api_agent/providers/contracts.py) are:

```python
class BrowserSession:
    browser: Browser
    context: BrowserContext
    page: Page  # Active page; the pack may update it after redirects/popups.

class ResourcePackAuthenticator(Protocol):
    async def authenticate(
        self, *, session: BrowserSession, tui_mode: RunMode,
        user_index: int | None, credentials: RoleCredentialReader,
    ) -> None: ...

class LLMAuthSession(Protocol):
    async def __call__(self) -> object: ...  # Fresh opaque authentication value.
    async def aclose(self) -> None: ...

class LLMAuthenticator(Protocol):
    async def authenticate(
        self, *, session: BrowserSession, user_index: int,
        credentials: LLMCredentialReader,
    ) -> LLMAuthSession: ...
```

Version and validate optional capability contracts. An absent UI hook or explicit async no-op is valid; a present malformed hook errors. Absence/no-op does not claim verified login. `user_index=None` means attended UI use, never unresolved unattended selection. There is no extra scenario or identity-mode argument: the pack chooses its strategy from ordinary mode and account selection.

The role reader accepts declared role, index and credential kind; the LLM reader is already bound to its provider-declared LLM role and accepts index/kind only. Reject undeclared roles, unsupported kinds, ambiguous/out-of-range selections and attempts to switch the bound account. Retrieve passwords only when needed and fresh TOTP at the challenge. Credentials and opaque tokens never enter ordinary logs, traces or report attachments, even with content logging enabled.

### UI session policy

| Mode | UI session | Visibility and preparation |
| --- | --- | --- |
| Unit, integration, help, collection | None | No frontend regardless of `RUN_UI`; integration may need a separate LLM login browser |
| Local-run, `RUN_UI=False` | None | No UI hook |
| Local-run, `RUN_UI=True` | One until local-run ends | Always headed; invoke pack hook before user handoff |
| E2E | Fresh separate browser instance/context/session for every attempt, including confirmation retries | Always real UI; headless only when neither `RUN_UI` nor debug is enabled; pack hook before test body |

- [ ] Yield the exact same `BrowserSession`, context and updated active page from pack preparation to test/user. Finish pack navigation tasks before handoff; do not open a different default-browser profile to continue an authenticated session. Attribute failed hook setup to that E2E attempt and do not run its test body.
- [ ] Map `E2E_DEBUG` to the debug policy. Debug retains every created browser, including failed setup attempts, through an explicit inspection owner/supervisor. Keep worker/driver ownership alive until inspection cleanup; retention cannot rely on pytest workers surviving implicitly.
- [ ] Keep the dedicated LLM browser headless even in debug, while retaining it too after provider disposal. The unused `AUTH_BROWSER_DEBUG` declaration is removed. Define shared debug retention for non-E2E operations when the browser owner is implemented; map `E2E_DEBUG` to that owner without creating a second visibility policy.
- [ ] Ordinary completion/cancellation closes all browser resources, including partial setup. Explicit inspection cleanup closes retained contexts, browsers and drivers. Retention never keeps provider refresh tasks or a disposed token getter active.

### Shared LLM session policy

- [ ] Initialize unattended LLM authentication lazily at the first authenticated provider call, only if the optional capability exists. Coalesce concurrent initialization. Factory validation, config/capability inspection and local cost arithmetic do not authenticate. Authenticated discovery/status/usage reads use the same mechanism without inference.
- [ ] Before every authenticated provider call, await the returned session's getter and pass its opaque value unchanged. The provider owns freshness, expiry, refresh and any persistent cache; core does not parse token claims or cache token values.
- [ ] Share one session across the selected backend runtime and credential binding, including a whole suite/local run. UI attempts have separate sessions/cookies; finishing one UI test does not dispose suite LLM authentication. Changing provider/account/credential binding disposes the old session.
- [ ] Retain provider-owned attended challenge/status/completion routes. Attended calls do not silently launch unattended login or borrow another user's getter.
- [ ] At shutdown, reject new calls, drain/cancel in-flight operations, finalize usage while authentication remains usable, await provider/session disposal, then close or retain browsers under bounded cleanup. Close is idempotent; getter calls after close raise a typed session-closed error.
- [ ] Keep browser objects and the getter inside their creating process/event loop. Test workers own UI sessions; the backend owns the suite LLM session. IPC carries generic credential requests/results, never Playwright objects or serialized opaque tokens.

## Usage and evidence

Core aggregates token counts, optional quota/reset snapshots and provider-returned finite nonnegative currency estimates. Pricing and quota-to-currency conversion belong to the provider. Missing values remain unavailable rather than measured zero; per-prompt currency requires a nonzero prompt count. Bracket the selected parallel workload once, count user turns rather than internal nodes, and disclose possible unrelated account consumption. See [session/usage lifecycle](implementation_plan_a7.md).

Allure records integration/E2E, including authentication/setup failures, from the first run. Unit tests remain excluded. Capture all-tab current-state browser failure evidence before teardown, with credential-safe attachments; successful response evidence follows the maximized/top-first rules. Human E2E acceptance and interactive integration/E2E selection remain mandatory.

## Acceptance

- [ ] Verify synthetic GraphQL and outside-path REST replacement, typed config/route collisions, absent/no-op/invalid hooks, identical pack/test sessions, fresh per-attempt cookies, every visibility/retention combination, worker ownership, lazy/coalesced LLM initialization, getter freshness, credential isolation, cancellation and bounded/idempotent disposal.
- [ ] Review concrete declarations, provider boundaries and deterministic evidence with the user before live provider traffic. Record real authentication, model discovery, tracing/history and initial renderer checks separately; migrated foundation checks do not prove those operations.
- [ ] After early external-consumer acceptance, give the included demo independently designed UI/API providers and listener/completion routes, with its own ports and callback paths. These real demo applications are not early-phase prerequisites. The personal GPT provider is the explicit early exception; the generic repository currently has no standalone live-provider acceptance.

- [ ] Remove the unused legacy `src/api_agent/config/shared.toml` snapshot in final cleanup. Runtime and wheel packaging use `cfg/config.toml`; do not maintain a second declaration source.
