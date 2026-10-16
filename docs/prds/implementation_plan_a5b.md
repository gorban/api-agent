# Local startup browser and authentication policy

[Local-run contracts](implementation_plan_a5.md) · [Prepared UI sessions](implementation_plan_i2c.md) · [Dashboard tabs](implementation_plan_k2.md)

The prepared-session policy supersedes earlier pack-hook and eager-login wording.
RUN_UI opts into the frontend; DEBUG changes visibility and retention without
enabling UI or authentication by itself. The separately owned startup viewer/
dashboard browser in K2 remains independent of frontend/authentication sessions.

- [ ] With RUN_UI=False, start no frontend or UI authentication session, and do
  not eagerly initialize the LLM provider or retrieve login credentials/quota.
  Genuine later authenticated API calls retain lazy provider setup. Separately
  configured reporting/dashboard tabs do not authorize login or a UI hook.
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
- [ ] Apply the shared LLM login visibility policy: attended is headed;
  unattended is headless unless RUN_UI or DEBUG requests visibility. Reuse or
  refresh valid cached state first. Bounded visible challenge handoff remains
  available; it does not turn DEBUG into authentication authorization.
- [ ] Reuse shared original-session inspection and cleanup for local UI and
  separate login browsers. DEBUG retains created sessions; ordinary shutdown
  closes them. Expose original endpoints/identities without substituting a
  browser, and preserve pending cleanup/retry ownership until disposal confirms.
- [ ] Verify UI-off with DEBUG both ways, UI-on attended/unattended/no-op login,
  matching-index LLM setup, failure/cancel/timeout and readiness ordering. Use
  the same consumer in explicit UI-provider integrations and E2E; do not duplicate
  login or browser visibility code in each runner. Keep startup dashboard tabs
  and the later selected workflow viewer under their existing owning requirements.
