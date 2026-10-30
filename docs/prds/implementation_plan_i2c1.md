# Prepared UI-session contract and integration checkpoint

[Prepared session requirements](implementation_plan_i2c.md)

The optional UI capability uses existing version-one provider capabilities:
`operations=("launch", "prepare_session")` advertises async
`aprepare_session(*, session, user_index, credentials)`, returning `None`.
Omission means anonymous readiness. A provider requiring login advertises the
operation and owns its recognition/navigation and completion criteria. A provider
bridging an existing authenticator invokes that implementation from this hook;
the consumer never invokes both hooks.

Core launches the effective selected provider with injected runtime services,
TLS and process ownership, then navigates the managed browser to its listener.
Preparation receives the original `BrowserSession`. Its browser/context remain
identical; the provider may update its active page within that same context.
No background setup may remain when the hook completes. Required credentials
use declared UI roles and the exact selected index; no fallback is permitted.

The prepared owner exposes `session`, `browser_owner` and `provider`, retaining
provider/runtime ownership through inspection. Its full `aclose()` releases
browser, provider and optional runtime owner, with incomplete cleanup retained
for retry. All launch/preparation/cleanup runs under shared nested timing.

`requires_ui_session` expresses integration intent independently of TUI mode.
Legacy tests retain LLM prerequisites; UI-only selections do not consume LLM
setup. Combined tests explicitly declare `requires_llm`. Mixed selections union
their provider prerequisites. Ordinary discovery remains frontend-free.

Applicability predicates may declare `operation = "ui_session"` inside
`applies_when`. This internal resolver intent comes from selected-test markers,
never a setting/environment override; it cannot change defaults or requirement
predicates. Other applicability/pack constraints and all value precedence remain
enforced. Each integration attempt receives an isolated ephemeral loopback port
with the configured listener scheme/host, without changing saved configuration.
Listener readiness verifies process identity and fails if another process wins
the allocation race. Local application ports retain their separate contract.

The fixture is one-shot and owns startup tasks, helper settlement, browser,
provider and creating loop. DEBUG retains the complete tree through end-inspection.
An interrupted transfer remains retryable before host disposal. Repeated cleanup
never cancels a preparation's disposal twice. Setup failure evidence precedes
cleanup through explicit retained ownership, including failure before a test body.

The one-prompt Studio request test consumes the prepared selected UI session.
Its `ui_provider_backend` fixture supplies an async context factory receiving the
effective UI runtime values and yielding only an Agent loopback URL and ephemeral
access token. Validate that binding, register the token for credential exclusion,
and retain the backend through UI disposal/DEBUG inspection. Ordinary session
checks leave this optional backend absent and never initialize an LLM. Studio
selectors are restricted to the included provider before LLM setup. The request
uses the real Agent HTTP contract; controlled tests substitute inference only.
Success/failure capture uses the same attempt pipeline, not temporary PNG files.

- [x] Prove the selected provider lifecycle with a harmless rendered action,
  original session handoff and no inference.
- [x] Prove absent/required hooks, scoped account refusal, invalid replacement,
  cancellation and retained cleanup with focused controls and mutations.
- [x] Join explicit configuration applicability and fixture ownership; keep
  DEBUG provider/runtime lifetime until original-browser inspection ends.
- [x] Independently review session, account-binding and joined startup ownership.
- [x] Complete applicable exact coverage, installed/package/public-boundary gates.
- [x] Human review the selected-provider browser, harmless rendered action and
  explicit retained-browser cleanup without an inference request.

Run local, application E2E, private login adapters and workflow conversion remain
separate consumers and retain their own acceptance gates.

Contract controls live in `test_ui_session.py`, `test_ui_runtime.py`,
`test_role_bindings.py`, `test_ui_operation_intent.py` and
`test_ui_provider_fixture.py`. The shared integration proves original-session
handoff; the included provider contribution proves its Appearance interaction.

Original delivery validation passed 9,555 units at 32 workers plus three test-only
phase/standalone-cleanup cases against identical production bytes. The union
measures every file at exact 100% lines and branches: 22,980 statements and
8,694 branches. All 9,558 original cases are accounted for; 27 subtests pass.
The installed wheel runs its selected included provider and rendered action
from an unrelated working directory, captures original masked pixels and
disposes its process/browser. Frontend coverage passes 112 cases plus five
reply controls with exact authored line/branch coverage.

- [x] Human acceptance of the one-prompt Studio integration through the prepared selected UI session, Agent HTTP bridge and recorded evidence. Full workflow execution and application E2E remain separate.

Post-test interaction and its known deadline/client-lifetime gap are tracked in [retained Studio interaction](implementation_plan_i2c2.md). Frontend human acceptance is deferred until that capability or working Run local is available; the accepted automated request remains distinct.

Human review reconfirmed the automated Studio `Ok` request and the previously accepted reporting and authentication behavior. Full manual interaction, explicit selection and long-idle cache acceptance remain deferred to a usable retained session or Run local; they do not block implementation of that capability.

## Failure-dialog appearance

- [x] Apply the selected dark appearance to the error dialog, its close button and backdrop, preserving readable text. Capture the existing error state before changing catalog or theme-test acceptance behavior. Preserve explicit error dismissal via the close button or Escape.
