# Prepared UI sessions for local runs and tests

[Frontend foundation](implementation_plan_i2.md) · [Local authentication](implementation_plan_a5.md) · [E2E evidence](implementation_plan_a3.md)

These requirements govern the operation consumer, beyond frontend/process
readiness. They do not claim Run local, authenticated UI preparation or E2E is
implemented. Use existing RUN_UI, DEBUG, E2E_DEBUG and UNATTENDED_USER_INDEX;
do not add a second UI-enable or authentication-mode switch.

The bounded [prepared integration checkpoint](implementation_plan_i2c1.md)
owns the first selected-provider consumer, isolated test listeners and optional
preparation. Run local and application E2E remain separate consumers below.

The [retained interactive-session follow-up](implementation_plan_i2c2.md) owns post-test provider lifetime, fresh request deadlines and separate accounting. Studio frontend acceptance waits for that capability or working Run local.

## Activation and visibility

[Shared browser visibility policy](implementation_plan_i2a2.md) limits RUN_UI to optional Run local frontend selection.

| Operation | UI provider and browser | Authentication at startup |
| --- | --- | --- |
| Run local, RUN_UI=False, DEBUG=False | No automatic startup browser or frontend | No eager UI or LLM authentication; actual later authenticated requests remain lazy |
| Run local, RUN_UI=False, DEBUG=True | Headed startup browser with home/status, enabled tracing and final flow; no frontend | No eager UI or LLM authentication; tracing login is separately configured |
| Run local, RUN_UI=True | Launch selected frontend and one managed headed UI session | Finish optional UI preparation, then prepare the selected LLM session when needed for the interactive agent |
| Ordinary integration/discovery | No frontend; only a provider-owned authentication browser if needed | Only capabilities requested by the test; catalog discovery never requires a UI session |
| Explicit UI-provider integration | Launch selected UI through the same consumer as local/E2E; headed with DEBUG, otherwise headless; RUN_UI does not affect test visibility | Optional UI preparation; LLM authentication only when the test explicitly needs it |
| E2E | Frontend always required; browser headed with DEBUG or E2E_DEBUG, otherwise headless; RUN_UI does not affect startup or visibility | Complete scenario-required UI/LLM setup before handing the prepared session to its test |

For Run local, RUN_UI or DEBUG opens one headed startup browser; DEBUG alone
does not enable the optional frontend or eager UI/LLM login. Run local never
uses headless browsing, including separate login browsers. Test consumers retain
their headless/visible policy. [Startup tabs](implementation_plan_k2.md) are the
unauthenticated home/status page, optional UI, optional tracing and final flow.
Tracing login uses its configured dashboard credentials independently of UI/LLM
login; a dashboard tab does not authorize frontend authentication. Attended login requiring human action must
be visible; a headless attempt that cannot finish reports interaction required
or uses the approved bounded visible handoff, never a silent account fallback.

## Provider-owned preparation and handoff

- [x] Freeze and validate an optional versioned UI-session preparation capability
  before consumers depend on it. The selected UI provider owns its Playwright
  login/navigation component, page recognition and success criteria. Core owns
  browser allocation, supervision, credentials, timing and cleanup. Bridge an
  existing resource-pack authenticator through this single preparation owner;
  do not invoke both hooks or move private selectors/protocols into core.
- [ ] Resolve the configured index once, across only consumed UI and LLM roles.
  Pass scoped credential readers and the original managed session to preparation;
  username is retrieved separately from the saved item name. Missing or failed
  selected unattended credentials fail actionably without switching to attended
  or another cached account. An absent/no-op hook is valid for anonymous UIs and
  means ready, not verified login. Providers requiring login must prove it.
- [ ] Prepare dependencies/TLS/listeners and verify the launched instance first,
  then await UI login/navigation to completion. Return the same browser/context
  and updated active page, with no background login/navigation left running.
  Process-ready, UI-authenticated and agent-ready are distinct states; publish
  agent-ready only after required setup succeeds. Failures retain diagnostics
  and cleanup ownership and cannot yield a successful user/test session.
- [ ] For enabled local UI with a selected unattended index, complete UI login
  before the normal selected-LLM readiness step. Reuse/refresh valid LLM state
  through its existing shared session owner and the same selected index. A UI
  login that needs LLM authentication may request that owner earlier; coalesce
  it rather than deadlock or open a second login. UI cookies and LLM credentials
  remain separate, with any identity relationship validated by their owner.
- [ ] No-UI local startup must not request UI/LLM credentials, start their login
  browsers, fetch LLM quota or force LLM setup. A configured tracing dashboard
  may still perform its separately owned login in the headed startup browser. Later genuine API/inference requests still
  authenticate normally. Do not apply this startup omission to discovery or
  inference integrations merely because RUN_UI=False.
- [ ] Give every E2E attempt its own prepared UI session; shared authenticated
  LLM readiness remains coalesced for the selected operation/account. Never
  share mutable UI contexts across tests or leak an attempt's cookies to another.
  Keep existing confirmation/subset acceptance policies and attempt identities.
- [ ] Use shared nested timeout/progress, original-session inspection and retained
  ownership. DEBUG retains created browsers on success or failure; ordinary
  completion closes them. Keep the creating worker/loop alive or transfer a
  verified ownership contract, including failed setup. Complete the known
  parallel UI retention repair before claiming application E2E.

## Early UI-provider integration proof

- [ ] Add a collected shared `tests/integration/test_ui_provider_session.py`
  through an explicit UI-session fixture. It must load the effective UI_PROVIDER
  through the normal loader, prepare/launch it through shared runtime services,
  await optional session preparation and exercise the returned original page.
  Do not reconstruct a Node host inline, force a different pack/provider or
  mutate TUI_MODE to trick applicability. Resolve the UI-specific settings and
  prerequisites for this explicit test capability without affecting discovery.
- [ ] The generic test proves instance-owned readiness, original session/page
  handoff, successful required preparation, and actual rendered frontend access.
  Provider-specific assertions/actions belong to provider-contributed suites.
  The included provider's test can exercise a harmless local UI action, such as
  a theme change, without provider login or inference. Private providers own
  their application routes, credentials and visible-login assertions.
- [ ] Keep `test_llm_discovery.py` scoped to selected LLM authentication/catalog
  access. Rename or refactor the existing Studio-request fixture to consume the
  same provider lifecycle/session instead of bypassing it. Keep its one real
  inference request a separate explicitly selected spending test.
- [ ] Verify no-hook, controlled login, selected-index pairing, credential failure,
  bounded setup, browser closure/cancellation, missing prerequisites and retained
  original-page inspection. Missing required configuration skips dependent tests
  with a reason; runtime login/launch failures fail with preserved safe details.
  No false success from an injected ready flag or an unrelated HTTP 200.
- [ ] Independent rejection mutations must detect skipped UI preparation,
  replacement page/context, premature handoff, wrong account, unexpected login
  with UI disabled, bypassed provider launch and lost DEBUG ownership. Require
  applicable exact unit coverage, installed/cwd independence, package/licenses
  and public-boundary review. Controlled proofs precede separately authorized
  live private login/inference and human review.

The first human checkpoint is an actual provider-launched integration browser
retained for inspection, followed by local startup with its browser/frontend gates and then the
first application E2E. Do not advertise commands as working before those consumers
are implemented and verified. Earlier direct Studio transport proof remains valid
for its limited scope.
