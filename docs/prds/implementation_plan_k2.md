# Startup dashboard tabs and LangFuse login

[Tracing milestone](implementation_plan_k.md)

These requirements join the tracing backend to local-run browser ownership. Exporters and synthetic backend diagnostics do not establish this interactive integration.

## Requirements

- [x] Support both interactive Run local and agent-driven `launch.py --tui-mode run-local --no-std-in`, with `--debug` for the same user-visible browser and original-session inspection. Preparation and shutdown use the ordinary launcher. Viewer/dashboard tabs alone never trigger LLM login; an enabled frontend uses [prepared-session readiness](implementation_plan_i2c.md).
- [x] Open one new headed startup browser through the shared managed Playwright factory when RUN_UI or DEBUG is True, independently of TRACE_MODE. With both False, do not automatically open a browser. RUN_UI separately enables the frontend service and its tab; DEBUG alone does not. Run local never creates headless browsers, including any later authentication browser. Keep LLM authentication in its provider-owned session; dashboard account selection remains independent, with startup-cookie sharing governed by the policy below.
- [x] When the startup browser opens, use this tab order, omitting disabled optional destinations without blank placeholder tabs:
  1. **Agent home/status:** always first. Provide a friendly unauthenticated page identifying the running application and showing health/readiness statuses, including disabled, starting, ready, degraded or unavailable components. Link to available UI/tracing/flow destinations conditionally. Loading or polling this page never retrieves UI/LLM credentials, starts login, discovers remote models or sends inference. Expose only safe status information, not configuration secrets, admin credentials, callback parameters or raw diagnostics.
  2. **Tracing viewer:** only for configured, ready tracing. Use its viewer address, not the ingestion/collector address, and perform supported automatic dashboard login, such as LangFuse with its configured admin account. Dashboard login is independent of UI/LLM account selection.
  3. **Actual LangGraph flow:** after any tracing viewer and before the enabled UI; final when UI is disabled. Reuse the `--view-flow` resource host/rendering contract inside this operation; do not invoke a second launcher or create a substitute browser. Display the graph compiled for this invocation with resolved overrides and validated diagram/source freshness. This read-only view performs no graph execution, login or inference.
  4. **UI provider:** always last and foregrounded, only when RUN_UI enabled and this invocation launched its verified frontend. Complete its required provider-owned session preparation before user handoff; retain the same browser/context/current page.
- [x] Keep the home/status and workflow viewer addresses available in local status when browser startup is disabled. Opening those read-only pages manually must not trigger UI/LLM authentication. Wait for the applicable hosts before opening tabs; a missing/failed flow host is reported rather than showing a stale or unrelated graph. [B1a](implementation_plan_b1a.md) owns matching README Get started wording and the `actual LangGraph flow` link to `flow.md.html`.
- [ ] Open dashboard/service tabs only after this invocation has started or acquired a verified managed component. Include the selected UI and agent when started; include enabled LangFuse after its integration establishes readiness. A configured upstream API endpoint stays visible in status but receives no startup tab solely because it is configured. Add its tab if this invocation actually manages and starts that API.
- [x] When TRACE_MODE=LangFuse, automatically open the resolved dashboard address and sign in through the observed login UI using shared Playwright helpers and the explicitly configured dashboard admin email/username and password. Verify authenticated project/dashboard state before claiming login success. LangFuse disabled starts no stack, tab or login attempt. OTEL viewer and ingestion URLs remain distinct.
- [x] Declare dashboard credentials through normal TOML discovery, applicability, review, persistence and base/pack/provider precedence. Local headless provisioning and browser sign-in use the same resolved credentials; remote dashboards require their own explicit credentials and start no local containers. Project public/secret ingestion keys are distinct from dashboard login credentials. Reused installations must validate the account rather than assume new initialization values replace existing users.
- [x] The original interactive credential display was delivered. The revised [terminal-only display requirement](implementation_plan_k2b.md) supersedes that presentation: omit it from the TUI and show it only in NO_STD_IN terminal status. Structured diagnostic/event privacy remains separate.
- [x] Bound login/navigation with shared timing, detect browser closure promptly and preserve operation/browser/component ownership through failure, cancellation and Stop/Exit. Failed or absent dashboard login remains actionable and must not be labelled authenticated. Closing component logs returns to the same browser and latest status.
- [x] Use origin-bound observed selectors and verify TLS; do not send dashboard credentials to arbitrary redirects or reuse LLM provider cookies/session state. Validate against the selected pinned LangFuse release when implemented.

## Acceptance

- [ ] Independently verify enabled/disabled UI and tracing, local/reused/remote endpoints, actual-started versus configured tabs, authenticated state, missing/incorrect credentials, redirect rejection, persistent literal credential display and exclusion from other evidence channels. Confirm RUN_UI/DEBUG startup combinations, headed-only local browsers, unauthenticated home/status with no credential/login side effects, conditional links, tab ordering and that the displayed flow matches the selected compiled graph, including explicit pack overrides; tracing tabs use viewer addresses rather than ingestion endpoints.
- [x] Verify browser closure, cancellation, partial startup and repeated cleanup without disposing services owned by another invocation. Run focused mutations and applicable coverage/public-boundary checks before code commits.
- [ ] Human reviews ordinary startup, automatic dashboard login, persistent status and Stop/Exit cleanup.

Official documentation: [Langfuse headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization) documents distinct user email/password and project API-key provisioning. The live documentation currently describes v4; recheck the pinned deployed version rather than silently upgrading this application.

## Later workflow inspector

- [ ] Apply [development graph refresh](implementation_plan_b1d.md) to the existing Run local flow tab and attached DEBUG browser. Display the definition actually used; document Python restart and incompatible-state handling. Refreshing a diagram never invokes the graph or reauthenticates.
- [ ] After the static flow tab and application lifecycle pass, reuse this same owned server/browser tab for [live workflow inspection](implementation_plan_b1c.md). Preserve tab order/readiness and static fallback; playback controls never pause execution or allocate another browser. Earlier application work reserves the evidence identities without pulling the full inspector into initial frontend integration.

## Core runtime checkpoint

The [core operation join](implementation_plan_h1a1.md) delivers local ownership,
unauthenticated home/status, the actual-flow tab and final optional prepared UI
with tracing disabled. Its earlier technical checkpoint is recorded in that child;
navigation/catalog repairs and core human acceptance are recorded there. The bounded tracing/dashboard join completes technical verification below;
broader combined requirements and human acceptance remain open.

## Dashboard join checkpoint

The local operation now has a bounded dashboard owner, distinct viewer addresses,
configured admin provisioning, origin-bound login and the ordered optional tracing
tab. Dashboard credentials stay outside status snapshots/events; literal terminal
status follows the [noninteractive-only policy](implementation_plan_k2b.md). Local stacks retain prerequisite lease
ownership; a dashboard owner never stops containers. Missing remote OTEL viewer
configuration does not invent a browser address. No-browser startup leaves dashboard
authentication unattempted.

The pinned LangFuse 3.163.0 image was checked using an isolated loopback stack:
its observed sign-in has Email, Password and Sign in; the helper also supports
the pinned source's optional Continue step. Successful login requires the selected
account from the session endpoint and a visible project dashboard destination.
This synthetic check proves dashboard integration, not graph trace or human
acceptance. The bounded runtime, content-policy and consumer joins have completed technical
verification and external review. Managed API startup tabs, the later inspector
and combined human acceptance retain their open gates above.

## Startup cookie policy

- [x] Retain the shared startup browser context; separate contexts or cookie hostnames are not required merely to prevent cookie sharing across local ports. Use distinct names for application-owned cookies where names could collide with dashboard cookies. Prefer HttpOnly for session cookies and Secure on HTTPS where the owning backend supports it; do not rewrite third-party cookies or force Secure onto supported HTTP-only development endpoints in a way that breaks login. No application-owned HTTP cookie is introduced by this tracing join. LLM provider authentication remains in its existing separate provider-owned session.
- [x] Verify that shared-cookie behavior cannot bypass the credential exclusions for logs, traces, Allure, status snapshots or inspection descriptors. Literal dashboard credential presentation follows the explicit terminal-status exception in [local-output follow-through](implementation_plan_k2b.md). This policy permits browser cookie sharing, not exporting authentication state.

## Startup and configuration follow-through

[Dashboard defaults, applicability and viewer recovery](implementation_plan_k2a.md) records the technically verified corrections and partial human acceptance. Automatic dashboard login retains its human gate; production-image clearing and broader deployed tracing conformance remain separate.

[Local startup, terminal output and shutdown follow-through](implementation_plan_k2b.md) owns the new viewer-default acceptance, terminal presentation, progress and reported request/cleanup repairs. Its technical and independent/external review gates are complete; the explicit human retest remains unchecked. Broader managed-API tabs, combined dashboard acceptance and deployment gates remain separate.
