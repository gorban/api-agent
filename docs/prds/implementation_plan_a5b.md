# Local startup browser and authentication policy

[Local-run contracts](implementation_plan_a5.md) · [Prepared UI sessions](implementation_plan_i2c.md) · [Dashboard tabs](implementation_plan_k2.md)

RUN_UI opts into the frontend service and tab. RUN_UI or DEBUG opens the shared
startup browser; DEBUG alone opens home/status, enabled tracing and the final
flow without enabling frontend login or eager LLM setup. Both False starts no
automatic browser. Every browser used by Run local is headed, including attended
and unattended authentication; headless browsing is a test policy only.

- [ ] With RUN_UI=False, start no frontend or UI authentication session, and do
  not eagerly initialize the LLM provider or retrieve UI/LLM credentials/quota.
  Genuine later authenticated API calls retain lazy provider setup. Separately
  configured tracing dashboards may still use their own login credentials; they
  do not authorize UI/LLM login or a UI hook.
- [ ] With RUN_UI=True, use the selected UI provider's normal launch lifecycle,
  verify owned frontend readiness, create the managed headed UI session and
  await its optional provider-owned login/navigation component. Bridge existing
  pack preparation through one owner. Hand the same context and current page
  back only after required preparation completes; anonymous/no-op providers
  remain supported without claiming verified authentication.
- [ ] Resolve the selected unattended index consistently for the consumed UI
  and LLM roles. Finish UI login, then ensure selected LLM readiness through its
  shared owner before declaring the interactive agent ready. If UI preparation
  needs that LLM session earlier, coalesce setup without a second login. Preserve
  separate cookie/token ownership and fail selected-credential errors rather
  than silently choosing attended mode or another cached account.
- [ ] Local LLM authentication always uses a headed browser if a browser is
  required, including after a later API request with both startup flags False.
  Reuse/refresh valid cached state first; no browser is needed for a cache hit
  or static API key. Test-mode unattended authentication may be headless under
  its own visibility policy. DEBUG does not authorize eager login by itself.
- [ ] Reuse shared original-session inspection and cleanup for local UI and
  separate login browsers. DEBUG retains created sessions; ordinary shutdown
  closes them. Expose original endpoints/identities without substituting a
  browser, and preserve pending cleanup/retry ownership until disposal confirms.
- [ ] Verify all four RUN_UI/DEBUG combinations, headed-only local login,
  authenticated tracing without frontend login, UI-on attended/unattended/no-op login,
  matching-index LLM setup, failure/cancel/timeout and readiness ordering. Use
  the same consumer in explicit UI-provider integrations and E2E; do not duplicate
  login or browser visibility code in each runner. Keep startup dashboard tabs
  and selected workflow viewer under [the four-tab plan](implementation_plan_k2.md).
