# Pluggable API and LLM providers

This is the authoritative boundary and browser-session requirements document. It supersedes conflicting wording in earlier checklists. The [version-1 foundation](provider_contracts_v1.md) records current exports and owners; its consumer surface remains provisional until [contract hardening](../prds/implementation_plan_m3.md). Follow the [shared provider task list](../prds/implementation_plan_m.md), [included GraphQL tasks](../prds/implementation_plan_m1.md), [external API extension tasks](../prds/implementation_plan_m2.md), and [LLM adapter tasks](../prds/implementation_plan_f1.md).

## Responsibility boundaries

| Component | Responsibility | Scope |
| --- | --- | --- |
| api-agent core | Typed configuration, provider loading, routing, credential broker, mode/account selection, browser allocation, evidence, usage aggregation and cancellation/cleanup | Selected runtime and credential binding |
| Included `api-providers/graphql-api-provider/` | Generic GraphQL transport, schema evidence, operation execution and upstream extension interfaces | Selected API provider |
| External upstream API extension | Authentication, signing, request/identity validation, upstream configuration and optional routes | Selected extension; no private default in core |
| Included personal GPT provider or replaceable external LLM provider | Discovery, inference, streaming, structured output, upstream metadata, optional authentication, usage and pricing | Selected inference provider; [early GPT task](../prds/implementation_plan_f2.md) |
| Optional external access adapter | Non-inference discovery/usage checks, scoped authentication and routes | Separately activated access operation; no completion/streaming capability |
| Optional UI provider | Frontend launch plan, authoritative proxy configuration, instance-owned readiness and process lifecycle | Selected pack and operation |
| Resource pack | Knowledge/navigation/prompts, scenario tests, UI authentication strategy and optional credential roles | Selected pack; UI hook once per newly created session |

Core is independent of the API type. A provider at a different path may implement REST or another API without accepting GraphQL queries. The included GraphQL package is `graphql_api_provider`; it requires a separately selected upstream extension. Product-specific code and examples belong to their external owners. Public tests use synthetic adapters, identities and API data.

Core owns shared UI browser resources operationally. A lazy setup provider may own its authentication browser/client and must dispose them through its session owner. A provider exclusively uses its LLM session until disposal finishes; a pack exclusively prepares its UI session before handing it to the test/user. Login pages, selectors, signing, cookie contents, protocol transactions and token interpretation remain outside core. Core request-session storage contains opaque request ownership, not provider tokens.

## Implementation phases

Contract hardening and synthetic consumer proofs come first, then the included personal GPT provider and external API/UI/access consumers. Real public demo UI/Nx applications, demo GraphQL servers and demo non-GraphQL providers are deferred. The generic GraphQL adapter library remains needed for external API consumers; it is not a demo server. A synthetic REST fixture proves replaceability without implementing a production demo provider.

Static API-key LLM providers require no login browser. They may use a scoped password-manager login descriptor; the included GPT provider retrieves its API key from that entry's password field, with no separate API-key config value or intermediary user login. An absent or explicit no-op browser authentication hook is valid; selected credentials are applied by that provider per request. Browser-free credential acquisition needs its own scoped lifecycle contract. API caller identity and unrelated LLM service credentials remain separate. Optional access adapters must not force selection of an inference provider just to test authentication/discovery.

The included GPT provider contributes its own `GPT_AUTH_MODE`, `GPT_API_MODE`, credential-role declarations, endpoint and applicable OAuth settings through provider-local TOML. Its API-key mode uses the selected vault password instead of a separate key field. Its approved matrix accepts API-key/completions, API-key/responses and eligible subscription/responses; subscription/completions fails before side effects. Conditional credential requirements and wire-format translation stay inside that provider, not generic core. See [the concrete GPT tasks](../prds/implementation_plan_f2.md).

## Selection, configuration and routes

`LLM_PROVIDER`, `API_PROVIDER`, `UI_PROVIDER` and `ACCESS_PROVIDER` select Python-file factories (`path.py:factory`). The loader accepts synchronous factories returning an integer `version = 1`, exposes lifecycle/optional route hooks, and reads adjacent `cfg/config.toml` without importing the factory during schema discovery. Factories are instantiated at awaited runtime startup. The protocol and owner foundation does not complete external adapters or operation consumers.

- [ ] Consume the optional `API_PROVIDER` and `UI_PROVIDER` selections and an API-contributed `UPSTREAM_API_PROVIDER` at executable operation boundaries. The selections/loader are available; the included GraphQL provider and actual operation wiring remain tasks. Require each only for an operation using that capability; the initial demo renderer does not need an upstream API.
- [ ] Resolve relative factory locations against a declared configuration owner root, independent of CWD. Support bundled and external locations and provider-local imports/dependencies without changing global `sys.path`.
- [ ] Bootstrap selected locations before full option validation, then merge all selected providers' optional typed declarations through the same defaults, applicability, explicit override, dotenv, ENV, CLI, help and TUI pipeline. Reject incompatible declarations and recursive selection; no unselected provider is imported or configured.
- [x] Resource packs may list ordered `providers.configs` declaration paths in pack.toml. Compatible provider declarations take precedence over pack defaults and appear as pack_default in the TUI; .env, ENV and CLI remain higher. Resolve these paths from the pack root without importing executable providers. Selected LLM file paths resolve against the configuration owner, and declared factory defaults participate in schema discovery.
- [ ] Let providers/packs contribute credential roles and optional `UNATTENDED_USER_INDEX`, including an LLM-only role. Bare core/demo does not declare an account index. Share one index across the roles consumed by an operation; do not require every declared role for every mode.
- [ ] Invoke optional `setup_routes(registrar, services)` once per runtime, after configuration and before serving. LLM and upstream API extensions may both supply it. Reject method/path collisions with core or another provider. Routes remain available without successful automatic login; route setup is separate from browser hooks and token retrieval.
- [ ] Keep executable listener configuration (`AGENT_DJANGO_URL`, HTTPS authority) independent of provider callback/registration settings. Supply a verified TLS context and generic lifecycle, authentication, credential and privacy services; never infer protocol endpoints in core.

## Browser and authentication contracts

The protocol shapes exported from [contracts.py](../../src/api_agent/providers/contracts.py) are:

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

class LLMSetup(Protocol):
    async def asetup(
        self, *, interactive: bool, user_index: int | None,
        credentials: LLMCredentialReader | None,
    ) -> LLMAuthSession: ...
```

Version and validate optional capability contracts. An absent UI hook or explicit async no-op is valid; a present malformed hook errors. Absence/no-op does not claim verified login. `user_index=None` means attended UI use, never unresolved unattended selection. There is no extra scenario or identity-mode argument: the pack chooses its strategy from ordinary mode and account selection.

The role reader accepts declared role, index and credential kind; the LLM reader is already bound to its provider-declared LLM role and accepts index/kind only. Reject undeclared roles, unsupported kinds, ambiguous/out-of-range selections and attempts to switch the bound account. Retrieve the actual username separately from its saved item name, passwords only when needed and fresh TOTP at the challenge. Credentials and opaque tokens never enter ordinary logs, traces or report attachments, even with content logging enabled.

### UI session policy

| Mode | UI session | Visibility and preparation |
| --- | --- | --- |
| Unit, integration, help, collection | None | No frontend regardless of `RUN_UI`; integration may need a separate LLM login browser |
| Local-run, `RUN_UI=False` | One headed startup browser until local-run ends | No frontend tab or UI hook; started-component dashboard tabs remain available |
| Local-run, `RUN_UI=True` | One headed startup browser until local-run ends | Open the verified frontend and other started-component tabs; invoke pack hook for the UI session before handoff |
| E2E | Fresh separate browser instance/context/session for every attempt, including confirmation retries | Always real UI; headless only when neither `RUN_UI` nor debug is enabled; pack hook before test body |

- [ ] Yield the exact same `BrowserSession`, context and updated active page from pack preparation to test/user. Finish pack navigation tasks before handoff; do not open a different default-browser profile to continue an authenticated session. Attribute failed hook setup to that E2E attempt and do not run its test body.
- [ ] Map `E2E_DEBUG` to the debug policy. Debug retains every created browser, including failed setup attempts, through an explicit inspection owner/supervisor. Keep worker/driver ownership alive until inspection cleanup; retention cannot rely on pytest workers surviving implicitly.
- [ ] Apply the same visibility policy to every LLM login consumer: unattended is headless unless RUN_UI or DEBUG makes it visible; attended is always headed. DEBUG retains its original session after provider auth disposal. The unused `AUTH_BROWSER_DEBUG` declaration is removed. Extend the implemented integration debug retention to other non-E2E consumers through the shared owner; map `E2E_DEBUG` to that owner without creating a second visibility policy.
- [ ] Ordinary completion/cancellation closes all browser resources, including partial setup. Explicit inspection cleanup closes retained contexts, browsers and drivers. Retention never keeps provider refresh tasks or a disposed token getter active.

### Shared LLM session policy

The optional `asetup` hook supports provider-owned attended setup and unattended cache reuse. Factories, help and collection remain inert; tests request setup explicitly through `llm_provider.ensure_ready()`. The [lazy setup checkpoint](../prds/implementation_plan_f2b.md) owns one loop/session per pytest worker; backend-wide shared authentication and inference consumers remain subsequent work.

- [ ] Initialize unattended LLM authentication lazily at the first authenticated provider call, only if the optional capability exists. Coalesce concurrent initialization. Factory validation, config/capability inspection and local cost arithmetic do not authenticate. Authenticated discovery/status/usage reads use the same mechanism without inference.
- [ ] Before every authenticated provider call, await the returned session's getter and pass its opaque value unchanged. The provider owns freshness, expiry, refresh and any persistent cache; core does not parse token claims or cache token values.
- [ ] Share one session across the selected backend runtime and credential binding, including a whole suite/local run. UI attempts have separate sessions/cookies; finishing one UI test does not dispose suite LLM authentication. Changing provider/account/credential binding disposes the old session.
- [ ] Retain provider-owned attended challenge/status/completion routes. Attended calls do not silently launch unattended login or borrow another user's getter.
- [ ] At shutdown, reject new calls, drain/cancel in-flight operations, finalize usage while authentication remains usable, await provider/session disposal, then close or retain browsers under bounded cleanup. Close is idempotent; getter calls after close raise a typed session-closed error.
- [ ] Keep browser objects and the getter inside their creating process/event loop. Test workers own UI sessions; the backend owns the suite LLM session. IPC carries generic credential requests/results, never Playwright objects or serialized opaque tokens.

## Integration configuration policy

Shared provider-independent assertions live in the base; provider-specific assertions live with the selected provider; pack-dependent assertions live in the selected pack. Mode-specific integration tests supply explicit required settings after ordinary configuration resolution, overriding defaults, dotenv, ENV and CLI only for that test. Preserve real endpoints, credentials and unrelated launch settings unless those settings are deliberately under test; never write test overrides into a user's configuration. Isolate provider/session ownership when changing test configuration.

All loaded, non-skipped integrations should be runnable with valid supported combinations and supplied required inputs. Detectable missing required/optional configuration skips only the dependent test, with named settings as the reason; malformed values remain failures. Tests declare prerequisites using the integration_config fixture’s require/require_combination helpers before side effects. The common lazy setup fixture converts only SetupPrerequisiteError to a skip; authentication, transport and other runtime errors remain failures. An invalid selected combination skips dependent operational tests with that explicit reason before authentication or other side effects. Its rejection-behavior test remains runnable and passes by asserting the correct enforced error. Under valid launch configuration, the same rejection test explicitly supplies its own invalid settings and still runs and passes; independent isolated checks need not be skipped. Completed results group failures/errors first, skipped second and remaining tests last, stably within groups. Enter on skipped opens its reason; summaries include distinct skipped counts, are neither passes nor failures, and must not make a partial run look like complete suite execution. Ordinary intentional integration skips do not fail the runner; actual test errors, startup/recording failure and interruption do. Common setup/conformance must cover each implemented valid mode, not only the default. This gate and the additional supported-mode wiring are tracked in [the setup checklist](../prds/implementation_plan_f2b.md); setup skips and result reporting are implemented; additional supported-mode wiring remains pending. Declaration discovery alone does not execute prerequisite checks.

## Usage and evidence

Core aggregates token counts, optional quota/reset snapshots and provider-returned finite nonnegative currency estimates. Pricing and quota-to-currency conversion belong to the provider. Missing values remain unavailable rather than measured zero; per-prompt currency requires a nonzero prompt count. Bracket the selected parallel workload once, count user turns rather than internal nodes, and disclose possible unrelated account consumption. See [session/usage lifecycle](../prds/implementation_plan_a7.md).

Allure records integration/E2E, including authentication/setup failures, from the first run. Unit tests remain excluded. Capture all-tab current-state browser failure evidence before teardown, with credential-safe attachments; successful response evidence follows the maximized/top-first rules. E2E requires its human-acceptance screen or explicit START=E2E. Integration requires interactive Start, or START=True/E2E or DEBUG for NO_STD_IN execution. DEBUG runs serially with browser interaction and retained inspection; it does not bypass the E2E acceptance gate.

## Acceptance

- [ ] Verify synthetic GraphQL and outside-path REST replacement, typed config/route collisions, absent/no-op/invalid hooks, identical pack/test sessions, fresh per-attempt cookies, every visibility/retention combination, worker ownership, lazy/coalesced LLM initialization, getter freshness, credential isolation, cancellation and bounded/idempotent disposal.
- [ ] Review concrete declarations, provider boundaries and deterministic evidence with the user before live provider traffic. Record real authentication, model discovery, tracing/history and initial renderer checks separately; migrated foundation checks do not prove those operations.
- [ ] After early external-consumer acceptance, give the included demo independently designed UI/API providers and listener/completion routes, with its own ports and callback paths. These real demo applications are not early-phase prerequisites. The personal GPT provider is the explicit early exception; the generic repository currently has no standalone live-provider acceptance.

- [ ] Remove the unused legacy `src/api_agent/config/shared.toml` snapshot in final cleanup. Runtime and wheel packaging use `cfg/config.toml`; do not maintain a second declaration source.

Enabled credential roles require account descriptors and a valid shared password manager/CLI for unattended operations. Plain startup offers Setup / Skip for incomplete profiles; explicit TUI_MODE bypasses that startup suggestion and Skip preserves attended use. Remove the superseded credential_role_optional metadata rather than exempting empty role arrays from setup checks. Subscription cache matching retrieves only the selected descriptor username; fresh observed login fetches password/TOTP only at their corresponding page. CLI retrieval stays lazy, bounded and owned until shutdown.

## Development inspection follow-up

Integration DEBUG contributes `browser_inspection` through factory services. Providers may pass that owner to BrowserOwner; BrowserSession carries an optional non-repr inspection_endpoint. The owner observes original sessions, keeps driver/context/browser resources after provider disposal, and drives the creating loop until retained windows close. The bundled provider currently offers loopback CDP only in this opted-in debug path; private adapters must use the same generic seam. Normal timeout/completion closes browsers. Current integration debug is serial per worker, not a shared cross-process authentication owner. Final pytest teardown and report publication wait for window closure.

Use api_agent.runtime.managed_browser.managed_browser for real provider, integration/E2E and local UI browsers. It returns the common BrowserOwner and lazily prepares Chromium. Before launching, the driver applies Playwright Stealth 2.0.3 to its Playwright instance; launch options, context headers and page initialization are shared across all contexts created from that instance. The browser/dev dependency groups include Stealth. Visibility and inspection remain caller-selected; initialization failure retains owned resources for cleanup rather than silently launching without the policy. BrowserOwner remains available for injected test drivers. Stealth reduces known automation indicators; it does not guarantee that a remote service will accept a browser or omit human verification.

- [ ] Establish one backend-owned suite authentication session before restoring parallel debug execution; concurrent requests coalesce and use the same selected account/session without multiple login windows. Workers receive generic operation/credential IPC, never browser objects or raw provider tokens.
  - [x] Ordinary integration readiness uses a [shared backend](shared_authentication.md) across base/pack/provider suites and parallel workers. Workers receive readiness/error/timing leases only; scoped credentials and tokens remain backend-local. Shared component deadlines are independent of individual waiters. Retained DEBUG remains serial and requires its own backend join.
- [ ] Separate browser attachability from retention. An explicitly inspection-enabled operation can expose the same original page without DEBUG; it may close normally when its purpose ends. DEBUG retains it beyond setup/test completion. Unattended visibility and automation remain provider-owned, with headed retention for explicit debug; attended consent is always headed. Apply this policy uniformly to included and private LLM providers, and to E2E/local-run UI sessions.

### Content emission boundary

Integration factories receive `services["emissions"]`, a shared credential-scrubbing
boundary. Register newly obtained secrets before emitting payloads, including
opaque authentication fields that core cannot inspect. Core registers configured
secrets, credential-helper results and string session tokens. Explicit
`LOG_CONTENT=True` enables per-call provider-neutral request/response Allure
attachments; default False omits them without disabling usage accounting.
Future tool, streaming and browser evidence must use the same gate and scrubber.
