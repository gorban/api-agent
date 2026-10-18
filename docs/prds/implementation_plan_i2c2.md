# Retained Studio interaction after test completion

[Prepared sessions](implementation_plan_i2c.md) · [Integration checkpoint](implementation_plan_i2c1.md) · [Studio frontend](implementation_plan_i2a.md)

[Ownership, admission and controlled evidence](implementation_plan_i2c2a.md) specifies the retained bridge and its remaining joined verification gates.

The integration's successful prompt and finalized evidence do not establish a usable post-test chat session. The previous DEBUG bridge retained its UI/Agent listener while the pytest LLM client and shared execution were finalized. Inherited UI readiness deadlines could also immediately reject later requests. The retained-owner implementation below repairs those separate lifetime boundaries.

- [x] Give long-lived HTTP serving its own explicit lifetime, independent of completed startup deadlines. Every incoming request receives a fresh bounded operation context; preserve genuinely enclosing request deadlines, cancellation and retained disposal. Do not extend an expired startup scope or remove request timeouts globally.
- [x] Keep an interactive retained Studio session backed by a valid selected provider owner until inspection ends. If execution finalization requires revoking the test client, create a distinct interactive owner through normal authentication/account selection; never resurrect a closed client or revoked worker lease. Before it is usable, expose a clear waiting/unavailable state rather than an expired-deadline error.
- [x] Freeze the original test outcome, usage, cost, quota and Allure artifacts. Treat later manual chat as separately owned activity and accounting. Prevent it from contaminating still-running tests' suite quota attribution: wait for finalized suite accounting or prove an explicitly isolated accounting boundary. Opening a retained browser alone must never initiate inference.
- [x] Preserve the selected account, model/version/reasoning controls, lazy readiness, credential exclusion and shared suite/inspection ownership. Keep the actual original browser available; end-inspection, back/menu and TUI exit must drain requests and release UI/backend/LLM owners with retryable incomplete cleanup.
- [ ] Prove pass/failure, elapsed startup deadlines, worker/session teardown, shared setup publication, parallel workers, delayed requests and interrupted cleanup with controlled HTTP/IPC/browser providers. Verify no mutation of finalized reports, no unowned provider tasks and no late suite accounting events. Renew applicable exact coverage, installed/package and public-boundary gates, plus independent review.
- [ ] Defer Studio frontend human acceptance until either working Run local or this retained interactive session provides a usable backend. Then review themes, composer/history, Stop/retry, clarifications and selection/message metadata. Prior automated Studio-request and Allure acceptance remains valid.

No additional live authentication or paid inference is authorized by this requirements task. Use controlled verification; any live review follows the user's existing spending instructions.

[Interactive cache acceptance](implementation_plan_f2g.md) additionally requires long idle pauses before another manually submitted request; cache retention is provider-dependent rather than a universal 31-minute guarantee.
