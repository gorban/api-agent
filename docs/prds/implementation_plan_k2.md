# Startup dashboard tabs and LangFuse login

[Tracing milestone](implementation_plan_k.md)

These requirements join the tracing backend to local-run browser ownership. Exporters and synthetic backend diagnostics do not establish this interactive integration.

## Requirements

- [ ] Support both interactive Run local and agent-driven `launch.py --tui-mode run-local --no-std-in`, with `--debug` for the same user-visible browser and original-session inspection. Preparation and shutdown use the ordinary launcher. Viewer/dashboard tabs alone never trigger LLM login; an enabled frontend uses [prepared-session readiness](implementation_plan_i2c.md).
- [ ] Every Run local opens one new headed browser through the shared managed Playwright factory, independently of TRACE_MODE and RUN_UI. RUN_UI controls the frontend service and its tab, not whether the startup browser launches. Keep provider authentication sessions separate.
- [ ] Open tabs in this order after each applicable readiness check: Agent app, enabled configured UI provider for interacting with the agent's API, configured tracing viewers, then the selected workflow as the final tab. Open the workflow after its resource host is ready, independent of TRACE_MODE and RUN_UI. Its static declaration/diagram view must represent the graph compiled for this invocation, with resolved pack overrides. Reuse the root-only `--view-flow` resource/viewer contract and preserve owned shutdown; [B1a](implementation_plan_b1a.md) also owns the matching README Get started text and `actual LangGraph flow` link to `flow.md.html`.
- [ ] Open dashboard/service tabs only after this invocation has started or acquired a verified managed component. Include the selected UI and agent when started; include enabled LangFuse after its integration establishes readiness. A configured upstream API endpoint stays visible in status but receives no startup tab solely because it is configured. Add its tab if this invocation actually manages and starts that API.
- [ ] When TRACE_MODE=LangFuse, automatically open the resolved dashboard address and sign in through the observed login UI using shared Playwright helpers and an explicitly configured dashboard email/username and password. Verify authenticated project/dashboard state before claiming login success. LangFuse disabled starts no stack, tab or login attempt. OTEL viewer and ingestion URLs remain distinct.
- [ ] Declare dashboard credentials through normal TOML discovery, applicability, review, persistence and base/pack/provider precedence. Local headless provisioning and browser sign-in use the same resolved credentials; remote dashboards require their own explicit credentials and start no local containers. Project public/secret ingestion keys are distinct from dashboard login credentials. Reused installations must validate the account rather than assume new initialization values replace existing users.
- [ ] Keep the dashboard username and password in the persistent interactive local status after startup tail removal, as explicitly requested. Render them as literal text; do not put them in startup/process logs, files, test artifacts, exports, traces, URLs or browser inspection descriptors. This display requirement does not relax the shared credential emission policy for those channels.
- [ ] Bound login/navigation with shared timing, detect browser closure promptly and preserve operation/browser/component ownership through failure, cancellation and Stop/Exit. Failed or absent dashboard login remains actionable and must not be labelled authenticated. Closing component logs returns to the same browser and latest status.
- [ ] Use origin-bound observed selectors and verify TLS; do not send dashboard credentials to arbitrary redirects or reuse LLM provider cookies/session state. Validate against the selected pinned LangFuse release when implemented.

## Acceptance

- [ ] Independently verify enabled/disabled UI and tracing, local/reused/remote endpoints, actual-started versus configured tabs, authenticated state, missing/incorrect credentials, redirect rejection, persistent literal credential display and exclusion from other evidence channels. Confirm tab ordering and that the final flow matches the selected compiled graph, including explicit pack overrides; tracing tabs use viewer addresses rather than ingestion endpoints.
- [ ] Verify browser closure, cancellation, partial startup and repeated cleanup without disposing services owned by another invocation. Run focused mutations and applicable coverage/public-boundary checks before code commits.
- [ ] Human reviews ordinary startup, automatic dashboard login, persistent status and Stop/Exit cleanup.

Official documentation: [Langfuse headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization) documents distinct user email/password and project API-key provisioning. The live documentation currently describes v4; recheck the pinned deployed version rather than silently upgrading this application.

## Later workflow inspector

- [ ] After the static final flow tab and application lifecycle pass, reuse this same owned server/browser tab for [live workflow inspection](implementation_plan_b1c.md). Preserve tab order/readiness and static fallback; playback controls never pause execution or allocate another browser. Earlier application work reserves the evidence identities without pulling the full inspector into initial frontend integration.
