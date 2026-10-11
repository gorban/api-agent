# Included personal GPT LLM provider

This is the early exception to deferred public demo applications. Build a replaceable included provider, not inference code inside orchestration. Complete the remaining [consumer contract hardening](implementation_plan_m3.md) before additional consumers or stable acceptance; apply [shared adapter requirements](implementation_plan_f1.md). Actual live GPT calls need the user's personal credentials and reviewed settings.

## Checklist

- [ ] Complete the [subscription connection foundation](implementation_plan_f2a.md) and [launch-managed lazy setup](implementation_plan_f2b.md), then the human sign-in checkpoint, before wiring real subscription inference. Normal users need only launch.py; keep saved OAuth accounts separate from vault API-key setup.
- [x] Agree the authentication/API matrix with the user: support API-key access with `completions` or `responses`, and eligible subscription integration with `responses` only. Reject subscription plus `completions`; this approves the design, not a live call or completed implementation.
- [x] Implement provider-contributed `GPT_AUTH_MODE` (`subscription`, `api_key`) and `GPT_API_MODE` (`completions`, `responses`) in [the bundled provider declaration](../../src/api_agent/llm_providers/gpt/cfg/config.toml), not shared core configuration. Default authentication to subscription for the preferred personal-plan route; conditionally default the API to responses for subscription and completions for API-key access. An explicit API value takes precedence and is never silently changed to repair an invalid combination.
- [x] Contribute `GPT_BASE_URL` and use the existing provider-declared `LLM User` role for both authentication modes. Do not add a second API-key role or a separate `GPT_API_KEY` setting. For unattended API-key setup, retrieve only the selected login/item password and keep the nonempty key in an owned, disposable in-memory session. Open no browser, OAuth cache or network client. Validate the API/authentication modes and HTTPS endpoint before retrieval; preserve selected credential failures without fallback. Subscription-only settings are inactive for API-key operations; startup still acknowledges all declared defaults across purposes. Plain startup offers Setup / Skip when incomplete; attended OAuth determines verified identity.
- [ ] Design attended API-key acquisition separately. Until implemented, missing selection/credential reader raises an actionable setup prerequisite; malformed or failed retrieval remains a failure. Do not introduce plaintext key configuration or silently choose another credential mode.
- [ ] Validate the resolved combination before opening clients/browsers or making requests, including explicit .env, environment and CLI overrides and TUI mode changes. Subscription plus completions raises a useful configuration error naming both fields and supported alternatives. Reject unknown enum values, invalid endpoints and missing applicable vault descriptors/credentials separately; help/collection never authenticates. Do not use an API key as fallback when subscription login fails, or a subscription token when API-key mode is selected.
- [x] Establish the importable `api_agent.llm_providers.gpt` package in `src/api_agent/llm_providers/gpt/`, adjacent typed configuration and integration declarations, distribution/dependency metadata and independently authored authentication contract tests. Load its factory through the ordinary LLM contract. Core defaults LLM_PROVIDER to this real bundled factory relative to base/package, independently of pack/CWD; pack defaults and explicit .env/ENV/CLI selections override it. Source and narrow installed-wheel factory/declaration checks pass. Complete whole relocated-pack acceptance under [distribution hardening](implementation_plan_m3.md).
- [ ] For the API-key route, contribute endpoint and GPT selection settings. Acquire the selected vault entry password through a scoped credential reader without allocating a login browser. Keep the key only in an owned in-memory auth session and supply it as the bearer key on each authorized request. Do not add an intermediate service login or retrieve username/TOTP for this role. Implement the browser-free credential acquisition contract before this consumer. Register secrets before diagnostics. Keep endpoint/key overrides reviewed and avoid exposing the key in traces, URLs or artifacts.
- [ ] Implement eligible subscription OAuth registration/consent, account selection, token lifetime and Responses streaming inside this provider. Verify preview feature/parameter limits against required tools and structured output, rejecting unsupported requests rather than ignoring parameters. Use the documented public subscription endpoint with store=false and stream=true; never send its OAuth token to a configurable third-party endpoint. Do not scrape another application's auth cache. Report limits and unavailable cost data honestly; eligibility and actual account access require live verification.
- [ ] Implement both API-key transports, mapping `completions` to `/chat/completions` (not legacy `/completions`) and `responses` to `/responses` under the configured API base. Share common LLM results/events/usage while preserving format-specific payloads internally. Validate the chosen endpoint/selection's supported features; a valid authentication/API combination does not guarantee every compatible service implements both formats.
- [ ] Support GPT-only discovery/selection, completion, streaming, tools and structured output where advertised. Translate transport-specific messages/events/usage into the shared schemas. Reject unsupported features or selections; do not silently fall back to another LLM family.
- [ ] Prepare selected dependencies from either launcher through managed bootstrap in its venv. Do not require manual installation of an editable sibling or unrelated optional providers.
- [ ] Normalize usage and provider-owned cost estimates without inventing prices or zero cost when unavailable. Keep personal inference accounting separate from any external access adapter's quota snapshots. Preserve the exact selected GPT throughout workflow personas.
- [ ] Test API-key header forwarding, browser-free password-only credential acquisition, zero username/TOTP retrieval, supported feature rejection, stream early close, deadlines, errors, privacy and disposal with controlled transports. Compare the same consumer with a second synthetic LLM adapter.
- [ ] Preserve upstream API error details through provider normalization, shared-backend communication, test failures, TUI details and recorded diagnostics. Include the original safe message and available HTTP status, error code/type and request identifier; redact credentials and other protected content rather than replacing the cause with a generic setup failure. Permission rejection or an expired/revoked key is a real failure when supplied configuration otherwise permits the test, not a missing-configuration skip. Do not silently switch users, API modes or endpoints to recover.
- [ ] Cover restricted keys with controlled transports: Responses allowed but GPT listing denied; GPT listing and Chat Completions allowed but Responses denied; and a formerly valid key rejected after expiration/revocation. Keep GPT discovery a separately tested capability rather than an unconditional prerequisite for a request with an explicitly selected GPT. Assert the upstream explanation survives to the displayed/recorded failure, secrets do not, and permission errors do not become skipped tests. Present equivalent explicitly selected live checks for human review once the relevant consumers exist; an operation requiring inference remains subject to the personal-inference checkpoint.
- [ ] Cover all four authentication/API combinations with deterministic tests: three accepted, subscription/completions rejected before any side effect. Test absent/incomplete/multiple vault descriptors and empty/nonempty retrieved passwords in API-key mode, zero API-key retrieval in subscription mode, conditional defaults and explicit overrides, no credential fallback, inactive subscription settings, OAuth endpoint binding and both transports through one unchanged renderer. API mode/account changes cannot reuse stale clients or auth sessions. Complete applicable unit/coverage checks before code commits.
- [ ] Human-review contributed settings and personal funding/transport, then verify no-inference discovery before the first small personal GPT renderer call. Record integration/E2E history from the first live run, preserving interactive gates and human E2E acceptance.

This task chooses no particular GPT variant. Live authentication and catalog subsets have been verified; text completion transports have deterministic proofs, with live inference acceptance recorded separately. Demo UI/API implementations remain deferred and are not prerequisites for this provider.

## Configuration acceptance matrix

| GPT_AUTH_MODE | GPT_API_MODE | Configuration rule |
| --- | --- | --- |
| subscription | responses | Valid with eligible integration/account; LLM User supplies subscription login credentials when unattended |
| subscription | completions | Invalid; fail before OAuth/client/request activity |
| api_key | responses | Valid with a valid selected vault login whose password is a nonempty API key, and supporting endpoint/selection |
| api_key | completions | Valid with a valid selected vault login whose password is a nonempty API key, and supporting endpoint/selection |

All GPT settings are provider-contributed and use normal effective-pack defaults and .env/environment/CLI precedence. Subscription/responses authentication and selected-password API-key setup in both valid API modes are implemented; subscription/completions rejects before side effects. API-key setup validates credential acquisition, not remote key validity, permissions or expiration. Missing attended key acquisition remains an explicit prerequisite. No-inference discovery is implemented through the loaded provider and shared backend/worker boundary; live catalog acceptance remains pending. Text completion transports are implemented; broader tools, structured output and public incremental streaming remain separate work.

## Vault and replaceability acceptance

- [ ] Startup auth setup derives active roles from selected-provider declarations, never a universal mandatory LLM User. The included provider keeps the same LLM User role in subscription and API-key modes. A complete selected API-key descriptor plus manager/CLI satisfies unattended API-key setup; no intermediary account or second role is required. Additional pack UI-login roles remain independent.
- [ ] Support a single key entry or indexed alternatives through existing role arrays and scoped password retrieval. Never persist fetched keys into .env or config; .env contains only descriptors/index. Missing setup fails before inference with an actionable error.
- [ ] Test core launch and an external pack selecting this bundled provider by override: provider settings appear, previous provider settings disappear, generic/pack defaults and explicit overrides remain correct, and an unchanged consumer works. Preserve inactive .env assignments on disk without exposing them as active settings.

## Completed authentication subset

- [x] Validate the declared authentication/API enums and reject subscription/completions before authentication/client/request activity, preserving explicit overrides. No credential-mode fallback is used. API-key setup also validates its endpoint before credential access; authorized request consumers remain pending above.
- [x] Derive credential-role declarations from the selected provider; the included GPT provider defaults to LLM User in both authentication modes. Switching modes preserves its configured entries and selected index, without prompting for another role or executing factories. Provider changes still rebind contributed configuration and roles. Selected-password API-key acquisition is implemented; live acceptance remains open.
- [x] Prepare managed browser dependencies and matching Chromium lazily for uncached GPT login; help/collection and cached reuse do not launch browsers. Selected-provider dependency manifests remain in contract hardening.
- [x] Complete observed subscription OAuth/login, verified account binding, protected cache reuse and owned refresh/disposal through launch.py. See [connection tasks](implementation_plan_f2a.md) and [setup proof](implementation_plan_f2b.md). Full sign-out/revocation UI and richer inference capabilities remain open; the text-completion subset is recorded below.
- [x] Verify base and external-pack provider selection/declaration replacement with deterministic TUI checks; unchanged inference-consumer proof remains pending.

## Shared role and indexed accounts

Most users configure one provider/authentication mode and one LLM User entry per account. If switching modes or providers, users must select an appropriate entry and matching configuration; the launcher does not infer provider/mode from entry names or indexes. Multiple LLM entries can share a resource-pack identity by repeating that pack's login/item descriptor at each corresponding index. For example, LLM User index 0 can name a subscription login and index 1 an API-key item, with the same pack account repeated at indexes 0 and 1. Providers retain their own credential interpretation and session isolation; role reuse does not permit credential fallback.

## API-key and discovery foundation

- [x] Complete actual API-key authentication verification before the configured-provider integration claims authentication success. A selected subscription login password, invalid key or rejected/revoked key must fail rather than passing solely because its password field is nonempty. Keep the same LLM User role and selected index; do not guess credential purpose from item names or fall back to subscription state. Verification belongs to the selected provider. Use the approved no-inference catalog/key-verification operation: catalog permission and selected inference-format permission are distinct, and ordinary inference with an explicit GPT must not gain an unconditional catalog prerequisite. Keep acquisition-only unit proofs separate from remote authentication conformance and preserve safe upstream rejection details in reports.
- [x] Default API-key `GPT_BASE_URL` to `https://api.openai.com/v1`; accept explicit HTTPS compatible-backend bases with matching keys. Reject credentials, query/fragment, malformed origins/ports, unsafe paths and decoded controls before retrieval. Subscription keeps its fixed official endpoint and never sends OAuth credentials to a configurable alternate.
- [x] Preserve key session revocation on close, idempotent cleanup and rejection after provider disposal during pending retrieval. Verify both API modes and actual selected-index password-only access through serial and parallel recorded workers; readiness is shared and keys do not cross worker/reporting boundaries.
- [x] Implement bounded, caller-client-owned GPT catalog discovery under the shared component timeout. Normalize API-key and subscription catalog shapes, preserve order, filter GPT identifiers, avoid redirects/retries and advertise no unverified inference features. Preserve bounded/redacted upstream status, message, code/type and request identifier for rejected keys; transport/deadline/malformed failures remain explicit.
- [x] Independently review the three lanes and repair GPT-family filtering, mixed-case endpoint redaction, decoded control validation and actual-loader test guards. Verify mutations detect the repaired boundaries. No live credential retrieval, discovery or inference is part of these implementation checks.
- [x] Pass 3,785 combined unit tests with 100% per-file line and branch coverage and build the wheel. Record the private public-protocol audit and separate pending human acceptance outside the package.
- [x] Join the discovery helper to the loaded provider and shared backend through a bounded no-token invocation contract, before adding the live GPT-catalog integration. Preserve safe provider error detail through worker/TUI/Allure recording; readiness-only IPC is not that contract.
- [ ] Human-review contributed settings and browser-free setup after the pending password-manager editor/recovery checkpoint; then select explicit no-inference discovery checks when the consumer join exists. Permission/expired-key acceptance and personally funded inference stay separate.

## Authenticated catalog consumer

- [x] Replace acquisition-only `tests/integration/test_llm_setup.py` with `tests/integration/test_llm_discovery.py`. The shared test calls `llm_provider.describe()` and requires usable selections. Keep inert fixture acquisition and readiness unit proofs; missing capability/configuration is a prerequisite skip, but a supplied credential's remote rejection is a failure.
- [x] Probe the configured catalog without credentials. On 401, retry that catalog with the selected credential. A public API-key catalog requires a successful credentialed key-verification response with bounded key metadata; reject malformed or rejected verification. Subscription uses the fixed official endpoint and rejects public catalogs before sending its token. Other status codes fail without protocol guessing or fallback.
- [x] Preserve optional display name, explicit version, output-token and context limits in generic selection metadata; unknown limits/version remain absent. Recognize direct and routed GPT IDs. Bound response allocation, normalized selections and wire metadata; keep credentials and key-verification metadata inside the provider.
- [x] Share a single lazy catalog request/result through the operation backend, including parallel base/pack/provider workers. Extend strict capability/lease IPC with bounded catalog metadata and provider-normalized safe upstream failures. Worker cancellation/deadlines do not cancel sibling work; disposal drains requests before auth resources. Keep DEBUG's current serial worker-local ownership.
- [x] Prove wrong-password, public-catalog/invalid-key and catalog-permission rejection through actual loaded-factory serial/parallel recorded tests. Assert the selected password is retrieved once, subscription state/username/TOTP stay untouched for API-key access, upstream details reach Allure, secrets do not, and failures remain failures. Independent review repaired aggregate frame bounds, malformed key verification and initiating-waiter deadline inheritance.
- [x] Preserve bounded numeric upstream error codes as strings and use a bounded Bearer authentication-challenge error identifier when the JSON error has no type. Verify expired-key details survive the actual serial/parallel worker, console and Allure paths without exporting raw headers or arbitrary response metadata.
- [x] Normalize diagnostic controls before final credential redaction; omit any detail that still contains the selected credential after masking. Prove control-interrupted reflections stay absent from error fields, worker frames, console and all recorded artifacts in serial and parallel runs. Request identity-encoded catalog responses and reject other content encodings before body iteration/decompression; retain the bounded identity-body guard and owned-response cleanup.
- [x] Mask Bearer credentials before and after control normalization so space/tab/newline/CRLF separators cannot expose a secondary credential. Preserve final selected-key redaction. Verify provider/error-frame fields and actual serial/parallel console/Allure artifacts with a credential distinct from the selected key; mutations detect removal of either masking pass.
- [x] Human-review API-key discovery rejecting an intentionally expired credential with visible expiry/status details, then passing with a replacement valid key at the matching compatible endpoint. Other live catalog/account scenarios remain separate.
- [ ] Human-review no-inference discovery with matching and mismatched personal credentials/endpoints, including restricted/expired keys. Catalog PASS does not prove Responses/Chat Completions permissions; implement and test those transports separately with safe upstream failure propagation. Do not require catalog access for explicitly selected inference.

## Minimal inference integration checkpoint

- [x] Implement the smallest shared LLM-request integration through the loaded provider and owned session, using a short prompt and bounded output. Prove the transports with deterministic unit tests before live checks. Resolve the shared configurable default through the allowed catalog; this test requires catalog permission. Explicit allowed inference remains independent of discovery permission.
- [ ] Verify subscription Responses first, then API-key Chat Completions and, when funded, API-key Responses against compatible endpoints. Use the same consumer assertion and generic result/usage contracts. Keep repeated live checks limited to the approved personal account and spending plan maintained outside this repository.
- [ ] Exercise deliberately unfunded or exhausted API-key access in both API formats, preserving the actual upstream message, HTTP status, code/type and available request identifier through test details and Allure. Distinguish observed billing/credit rejection from permission rejection; never manufacture a quota diagnosis or fall back to another account, endpoint or authentication mode. A normal request integration fails on rejection; a dedicated rejection test passes only when it asserts that expected error behavior.
- [ ] Record each live outcome and unavailable funded combination separately; a funding-limited deferred check is not transport acceptance. Use the verified subscription Responses route for subsequent development while retaining deterministic coverage of both API-key transports.

Validation: 3,922/3,922 combined base/pack units pass with 100% line and branch
coverage in every measured file. Serial/parallel recorded synthetic runs preserve
PASS/FAIL and safe upstream details in Allure. Independent adversarial review is
clear; focused mutations detect disabled key rejection, verification shape,
subscription/public-catalog rejection and shared deadline isolation. The wheel
builds and its relocated catalog/factory contract loads without authentication.
The expired/replacement API-key discovery walkthrough is human-approved; other
live catalog scenarios and inference-format permissions remain pending.

## Completed text transport subset

- [x] Add `complete(request)` to ordinary shared and serial DEBUG integration
  consumers using the existing LLMRequest/LLMResult/Usage contract. Keep explicit
  inference independent of catalog permission, never cache results, and stop
  cancelled calls before returning cancellation. Revoked leases reject replies;
  completed tasks leave both execution and cancellation tracking.
- [x] Implement bounded identity JSON API-key Responses/Chat Completions and
  subscription SSE Responses. Subscription binds to the official endpoint, uses
  store=false/stream=true, maps system instructions to developer, and requires
  response.completed. It omits unsupported server token caps and rejects an
  explicit cap; limits are local response bytes and deadlines. API-key access
  uses a small default output cap (128), with explicit supported caps available.
- [x] Preserve sanitized direct-admission detail as well as error objects,
  HTTP status/code/type/request identifiers, strict JSON/SSE terminal validation
  and rejection before decompression. Reject selected-credential echoes in
  successful output, including controls that could later expose the credential.
  No retries or endpoint/account/mode fallback are used.
- [x] Add generic test_llm_request integration using the shared configured
  default selected from the allowed catalog. Remove obsolete provider-authored
  request selections and the inference-selection runtime opt-in. Normal start
  gates authorize integration execution; actual remote rejection fails. Capture
  attempted turns and returned token totals without prompt/output/auth content.
- [ ] Periodically review integration/E2E tests for value and selectively mark
  low-value cases skipped by default; do not require a separate inference opt-in.
- [ ] Complete the separate live compatibility and human checkpoints above.
  Text transport unit coverage is not live account or funding acceptance.
- [ ] Extend transports and consumer wire contracts for tools/structured output
  and incremental streams before advertising those capabilities. Keep this
  checkpoint's capabilities limited to describe/complete and supported options.

Protocol follows the official OpenAI Responses, Chat Completions and subscription
preview requirements, including its unsupported-option restrictions.

## Live text compatibility and capability follow-up

- [x] Accept subscription SSE with missing MIME only when strict event parsing
  and successful terminal validation succeed. Recover finished assistant items
  from item-done events when terminal output is empty; require matching unique
  indexes/identifiers, no unfinished items, and agreement with nonempty terminal
  output. Preserve bounded allocation, original usage and credential guards.
- [x] Verify the ordinary subscription Responses request and tiny compatible
  API-key requests in both API formats. Preserve actual exhausted-credit and
  missing-permission rejections in failed tests and successfully published
  history; absence of a funded positive check is not acceptance.
- [ ] Extend the common contracts and each selected adapter for incremental
  streaming, strict structured output and tool calls/results. Preserve tool
  identifiers, argument fragments, explicit completion/refusal/truncation states,
  cancellation and unknown usage after interrupted streams. Never execute a
  partially received tool call or automatically repeat a possibly executed turn.
- [ ] Add configurable reasoning effort with per-selection validation and
  transport mappings. Map common output limits to supported request fields;
  account for reasoning tokens sharing the output budget. Reject unsupported
  options explicitly, including subscription server output caps.
- [x] Implement core LLM_CACHE_POLICY presets: empty omits controls; off disables
  prompt caching on supported newer official OpenAI selections; medium/high
  select the shortest/longest documented retention. Newer selections map both
  enabled presets to implicit/30m; documented older families map to in_memory/24h,
  while selections supporting only 24h map both to it. Subscription never sends
  the unsupported old retention field. Unverified compatible backends, unknown
  selections and older off reject explicitly before credentials/inference.
  Configured policies cross the ordinary backend and DEBUG local consumer;
  explicit request policies win. Authentication and answer caching stay separate.
- [x] Implement explicit cache presets for canonical `https://openrouter.ai/api/v1` and known namespaced OpenAI GPT-5.6/6/6.1 families, including `openai/gpt-6-luna` and dated suffixes. Off sends explicit mode without breakpoints; medium/high use documented explicit mode with 30m TTL and a final text-block breakpoint covering the whole input prefix. Official OpenAI mappings stay unchanged. Deterministically verify both Responses and Chat Completions mappings for off/medium/high, retaining rejection for unknown routes/models or unsupported retention. Empty must continue omitting controls and allowing upstream automatic caching; a short prompt is not a cache-hit acceptance test. Use deterministic request/usage/accounting proofs first; live cache-hit checks need an eligible repeated prefix and separately authorized spend. See [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching).
- [x] Independently review cache preflight and accounting error paths. Require ASCII calendar-valid dated suffixes without freezing supported families to one catalog snapshot; preserve validated token/cache counts when OpenRouter billing metadata is rejected. Verify both transports and isolate mutations of the intended checks.
- [x] Add one provider-owned normal-English integration cache test, skipped before discovery/authentication when caching is off. Require exactly 1,027 API-reported input tokens on both identical owned requests, a one-word Paris answer, and at least 1,024 cached input tokens on the repeat. Document the observed GPT-6 Luna framing difference: 1,024 total reported tokens gave no cache hit, while 1,027 allowed a 1,024-token cache prefix. Keep fixtures static; no tokenizer download, calibration calls or automatic retries during the test. Preserve both calls' usage and charges in ordinary suite accounting.
- [x] Independently review exact input/cache thresholds, off-mode early skip, immutable repetition and failure propagation with isolated mutation controls. At the user's authorization, verify live OpenRouter GPT-6 Luna cache hits with medium in both Chat Completions and Responses; this is scoped evidence, not acceptance of every supported family/backend.
- [ ] Human acceptance: inspect the new cache test, its framing explanation, skip behavior and two-call Allure usage. Other backend/model fixture compatibility needs explicit live verification before claiming acceptance.
- [ ] Add bounded image content to shared requests and worker frames, with
  provider-owned Chat Completions/Responses mappings and selection validation.
  Check media type, size, supported detail and safe source handling before
  submission. Preserve LOG_CONTENT=False privacy for image bytes/URLs across
  logs, traces, errors and reports. Test image+text and unsupported selections.
- [ ] Prove these capabilities through the same consumer and a synthetic second
  adapter, with capability-specific integration coverage. Follow the separately
  approved personal live-call budget; broader live inference remains a separate
  human checkpoint.

## Subscription cache and diagnostic follow-up

- [x] Adapt subscription cache policy to its observed endpoint limits: empty keeps upstream defaults; medium permits automatic caching while omitting explicit cache controls and promising no lifetime. Reject off/high before completion authentication or inference because disabling caching and extended retention cannot be enforced. A real request previously rejected `prompt_cache_options`; API-key mappings remain separate. Advertise cache-policy handling so shared consumers can use medium, with provider validation enforcing unsupported choices. Preserve the configured/request policy as accounting evidence even when no cache field is sent. Update the provider-owned repeated-prefix test to allow medium subscription, retaining off/high skips. Verify actual streaming request bodies and both direct/backend paths, immutable overrides, cached-token preservation and isolated guard mutations. This supersedes the initial all-presets subscription rejection.
- [x] Correct subscription cache affinity: generate a non-secret UUID once per owned provider and send it consistently as both prompt_cache_key and HTTP session-id through refreshes and newly allocated HTTP clients. Isolate provider instances; never send these fields on API-key routes. Validate header identity before transport and preserve immutable requests and upstream usage. Public upstream routing evidence and live subscription probes establish reuse; explicit TTL controls remain unsupported. Upgrade the single repeated-prefix integration test: keep exact 1,027-input / at-least-1,024-cached acceptance for API keys; use the verified 4,045-input subscription fixture and require greater than 90% cached input, with Paris processing assertions on both calls. Document smaller subscription misses and exact route-specific framing without retrying or weakening failures.
- [ ] Human acceptance: rerun the provider-owned prompt-cache test with subscription/Responses/medium, inspect both input/cache counts in Allure, and verify API-key cache acceptance is unchanged. Cache affinity improves reuse but does not guarantee a hit or enforce a lifetime.
- [x] Refactor cache acceptance into one straight-line test body with named processing/reuse assertions, immutable expectation data and a fixture that determines eligibility before model/provider access. Consolidate identical transport-specific text copies into one calibrated prompt; preserve both route-specific request bytes and strict token/cache thresholds. Use parameterized routing controls rather than mode-dependent assertions. Check native pytest fixture ordering and off/high early skips. The shared discovery/request integration bodies already need no conditional setup cleanup; future configuration coverage still requires rerunning applicable tests with each supported configuration.
- [x] Isolate prompt-cache attempts from previous runs: put a fresh UUID4-derived nonce at the first bytes of the shared prompt, retain it unchanged within the warm/repeat pair, and verify the first reply reports zero cached input. Encode all UUID bits as 32 space-separated A-P letters to preserve token length; trim the English passage by the same 33-token header budget. Retain exact 1,027/4,045 input targets and repeat-cache thresholds. Test nonce bit preservation, fresh per-attempt generation, immutable paired requests, cold-read rejection, setup ordering and isolated assertion mutations. Preserve genuine upstream cache misses as failures; do not retry or reinterpret them as successes.
- [x] Clear unwound provider traceback locals before pytest records failures, including cause/context cycles and exception-group children. Preserve stack locations and safe upstream error details. Exercise serial, parallel and actual DEBUG subprocess recording with controlled credentials and detect disabled clearing through isolated mutations.
- [x] Allow START=True/E2E or DEBUG at both launch and recording boundaries for NO_STD_IN integrations. DEBUG alone retains the approved terminal-free integration exception; E2E still requires explicit START=E2E.

## Shared model policy

- [x] Declare LLM_ALLOWED_MODELS and LLM_DEFAULT_MODEL in base configuration,
  using validated full-ID regexes. Defaults admit GPT-6 Luna/Haiku/Sol and select
  the highest Luna version. Providers describe family/version metadata; core
  selection remains vendor-neutral. Reject no matches, mixed/missing comparison
  metadata, highest-version ties and excessive regex work explicitly.
- [x] Enforce admission before provider loading for explicit inference, and filter
  local/shared-backend catalogs consistently. Resolve the request integration's
  model through the same allowed catalog rather than a hardcoded Sol selection.
  Cover both consumer paths, bootstrap validation and metadata wire preservation.
- [ ] Wire this policy into the [public model-list and inference API](implementation_plan_f.md).

## GPT estimate ownership

- [x] Supply standard text list rates for exactly known direct-provider selections, with cached-input discounts and long-context premiums; do not invent rates for unknown selections or third-party routing. API-key mode never reads subscription quota or uses subscription allocation prices.
- [x] Use a bounded optional read of the selected subscription token's weekly/daily quota window, matching LLM_QUOTA_PERIOD, before/after owned integration work. Rejection, schema changes or unavailable fields fall back to list pricing; do not read another application's auth cache or let accounting change authentication/test outcomes.
- [ ] Extend third-party routing price discovery from validated catalog metadata and support later media/tool/cache-write billing metrics. Unknown routed prices stay unavailable until implemented and verified.

Pricing sources, verified 2026-10-10: [Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [Sol](https://developers.openai.com/api/docs/models/gpt-6-sol), [6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol), [4.1 nano](https://developers.openai.com/api/docs/models/gpt-4.1-nano). Rates are USD per million standard text tokens. Missing cached-input detail is estimated at the uncached input rate; reasoning is already included in output. Quota protocol evidence: [official backend client](https://github.com/openai/codex/blob/main/codex-rs/backend-client/src/client.rs). This optional endpoint's compatibility with the selected subscription token requires live verification; inaccessible usage remains unavailable.

- [ ] Supply provider-owned immutable pricing evidence for each quote: numeric rates/units/currency, revision/source/verification time, long-context/cache/service-tier factors and the applicable allocation profile. Preserve configured and provider-observed plan/bucket metadata separately. Capture actual returned model/revision where available and distinguish it from catalog inference or aliases. Keep unknowns explicit and prevent credentials from entering new metadata across IPC or Allure. Do not assume public API token prices exactly describe subscription quota weighting.


## Review repairs: completed output and cache preflight

- [x] Reject explicitly unfinished assistant/reasoning output items in the shared terminal parser for both JSON Responses and terminal-only subscription SSE. Preserve compatible omitted item status and validated token usage in the safe failure; accumulated stream events must not be required to detect contradiction.
- [x] Apply verified-family membership and ASCII calendar-valid snapshot handling consistently to official and compatible cache-control routes. Valid future snapshots of supported families remain eligible; unknown suffixes and impossible/non-ASCII dates fail before authentication or requests. Empty policy still delegates provider defaults; subscription medium still permits automatic caching.
- [x] Preserve the human-approved unique prefix for each cache-test attempt, reuse that prefix for the two calls, and assert zero first-call cache reads followed by the route's required cache reuse. Discard the older pending two-file/branching rewrite and its backup as explicitly directed by the user.
