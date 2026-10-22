# Startup dashboard tabs and LangFuse login

[Tracing milestone](implementation_plan_k.md)

These requirements join the tracing backend to local-run browser ownership. Exporters and synthetic backend diagnostics do not establish this interactive integration.

## Requirements

- [ ] Support both interactive Run local and agent-driven `launch.py --tui-mode run-local --no-std-in`, with `--debug` for the same user-visible browser and original-session inspection. Preparation and shutdown use the ordinary launcher. Viewer/dashboard tabs alone never trigger LLM login; an enabled frontend uses [prepared-session readiness](implementation_plan_i2c.md).
- [ ] Open one new headed startup browser through the shared managed Playwright factory when RUN_UI or DEBUG is True, independently of TRACE_MODE. With both False, do not automatically open a browser. RUN_UI separately enables the frontend service and its tab; DEBUG alone does not. Run local never creates headless browsers, including any later authentication browser. Keep provider authentication state separate.
- [ ] When the startup browser opens, use this tab order, omitting disabled optional destinations without blank placeholder tabs:
  1. **Agent home/status:** always first. Provide a friendly unauthenticated page identifying the running application and showing health/readiness statuses, including disabled, starting, ready, degraded or unavailable components. Link to available UI/tracing/flow destinations conditionally. Loading or polling this page never retrieves UI/LLM credentials, starts login, discovers remote models or sends inference. Expose only safe status information, not configuration secrets, admin credentials, callback parameters or raw diagnostics.
  2. **UI provider:** only when RUN_UI enabled and this invocation launched its verified frontend. Complete its required provider-owned session preparation before user handoff; retain the same browser/context/current page.
  3. **Tracing viewer:** only for configured, ready tracing. Use its viewer address, not the ingestion/collector address, and perform supported automatic dashboard login, such as LangFuse with its configured admin account. Dashboard login is independent of UI/LLM account selection.
  4. **Actual LangGraph flow:** always final when the startup browser opens, even without UI or tracing. Reuse the `--view-flow` resource host/rendering contract inside this operation; do not invoke a second launcher or create a substitute browser. Display the graph compiled for this invocation with resolved overrides and validated diagram/source freshness. This read-only view performs no graph execution, login or inference.
- [ ] Keep the home/status and workflow viewer addresses available in local status when browser startup is disabled. Opening those read-only pages manually must not trigger UI/LLM authentication. Wait for the applicable hosts before opening tabs; a missing/failed flow host is reported rather than showing a stale or unrelated graph. [B1a](implementation_plan_b1a.md) owns matching README Get started wording and the `actual LangGraph flow` link to `flow.md.html`.
- [ ] Open dashboard/service tabs only after this invocation has started or acquired a verified managed component. Include the selected UI and agent when started; include enabled LangFuse after its integration establishes readiness. A configured upstream API endpoint stays visible in status but receives no startup tab solely because it is configured. Add its tab if this invocation actually manages and starts that API.
- [ ] When TRACE_MODE=LangFuse, automatically open the resolved dashboard address and sign in through the observed login UI using shared Playwright helpers and the explicitly configured dashboard admin email/username and password. Verify authenticated project/dashboard state before claiming login success. LangFuse disabled starts no stack, tab or login attempt. OTEL viewer and ingestion URLs remain distinct.
- [ ] Declare dashboard credentials through normal TOML discovery, applicability, review, persistence and base/pack/provider precedence. Local headless provisioning and browser sign-in use the same resolved credentials; remote dashboards require their own explicit credentials and start no local containers. Project public/secret ingestion keys are distinct from dashboard login credentials. Reused installations must validate the account rather than assume new initialization values replace existing users.
- [ ] Keep the dashboard username and password in the persistent interactive local status after startup tail removal, as explicitly requested. Render them as literal text; do not put them in startup/process logs, files, test artifacts, exports, traces, URLs or browser inspection descriptors. This display requirement does not relax the shared credential emission policy for those channels.
- [ ] Bound login/navigation with shared timing, detect browser closure promptly and preserve operation/browser/component ownership through failure, cancellation and Stop/Exit. Failed or absent dashboard login remains actionable and must not be labelled authenticated. Closing component logs returns to the same browser and latest status.
- [ ] Use origin-bound observed selectors and verify TLS; do not send dashboard credentials to arbitrary redirects or reuse LLM provider cookies/session state. Validate against the selected pinned LangFuse release when implemented.

## Acceptance

- [ ] Independently verify enabled/disabled UI and tracing, local/reused/remote endpoints, actual-started versus configured tabs, authenticated state, missing/incorrect credentials, redirect rejection, persistent literal credential display and exclusion from other evidence channels. Confirm RUN_UI/DEBUG startup combinations, headed-only local browsers, unauthenticated home/status with no credential/login side effects, conditional links, tab ordering and that the final flow matches the selected compiled graph, including explicit pack overrides; tracing tabs use viewer addresses rather than ingestion endpoints.
- [ ] Verify browser closure, cancellation, partial startup and repeated cleanup without disposing services owned by another invocation. Run focused mutations and applicable coverage/public-boundary checks before code commits.
- [ ] Human reviews ordinary startup, automatic dashboard login, persistent status and Stop/Exit cleanup.

Official documentation: [Langfuse headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization) documents distinct user email/password and project API-key provisioning. The live documentation currently describes v4; recheck the pinned deployed version rather than silently upgrading this application.

## Later workflow inspector

- [ ] Apply [development graph refresh](implementation_plan_b1d.md) to the existing Run local flow tab and attached DEBUG browser. Display the definition actually used; document Python restart and incompatible-state handling. Refreshing a diagram never invokes the graph or reauthenticates.
- [ ] After the static final flow tab and application lifecycle pass, reuse this same owned server/browser tab for [live workflow inspection](implementation_plan_b1c.md). Preserve tab order/readiness and static fallback; playback controls never pause execution or allocate another browser. Earlier application work reserves the evidence identities without pulling the full inspector into initial frontend integration.

## Core runtime checkpoint

The [core operation join](implementation_plan_h1a1.md) delivers local ownership,
unauthenticated home/status, optional prepared UI and the final actual-flow tab
with tracing disabled. Its technical checks and independent review are complete;
human acceptance remains in that child. The tracing/dashboard requirements and
combined acceptance above remain open for their following integration batch.
