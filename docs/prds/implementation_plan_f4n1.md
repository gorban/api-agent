# Terminal authentication context and attended session handoff

[Authentication prompts](implementation_plan_f4n.md)

[Cold-start sign-in coalescing](implementation_plan_f4n2.md) addresses the
subsequently reported duplicate prompts from parallel test-profile owners.

- [x] Show the selected interaction title before terminal prompts, matching the
  TUI's existing channel title. Preserve the helper's account-specific text;
  do not copy user identities or secrets into fixtures, docs or reports.
- [x] Allow attended session acquisition in terminal integration when an
  operation-owned visible interaction channel is explicitly supplied, even with
  NO_STD_IN. Continue to reject absent interaction permission/ownership; do not
  introduce implicit raw stdin, credential fallback or identity substitution.
- [x] Forward the permission through the shared integration owner, Bedrock
  session request and native direct/SSO adapter. Preserve SDK reuse, same-identity
  refresh, cancellation, EOF and cleanup behavior.
- [x] Keep indexed authentication authoritative. Bundled session login requires
  an empty unattended-user selection; a saved API-key index is not a request to
  sign in as another account. Custom indexed session adapters retain their own
  qualification contract. Manual key-entry policy is not changed here.
- [x] Cover title context, serialized queued prompts, terminal native URL/code
  handoff through shared setup, missing ownership and indexed rejection with
  deterministic tests and independent review.
- [x] Reconcile generated CLI help, configuration help and guides: NO_STD_IN is
  not a promise that explicitly owned authentication cannot request input.
- [ ] Human acceptance of attended session login. The user reports 19/19 Bedrock
  API-key integrations passing with NO_STD_IN; this does not qualify session mode.

This explicitly expands the earlier presentation-only scope. NO_STD_IN continues
to select non-TUI execution; visible owned authentication is permitted where the
operation supplies it. A detached execution without an interaction owner cannot
start a hidden sign-in. Existing application deadline/retry policies remain.

## Verification

509 focused units passed, with terminal interaction at 100% line/branch coverage.
The final 71-test recheck includes the generated-help correction and strengthened
indexed rejection. Native direct and SSO flows join the real local workflow;
shared integration acquisition joins an actual TCP interaction relay and terminal
pipe. The AWS/SDK boundaries are controlled: no live sign-in or paid inference
was performed. EOF remains terminal and no indexed identity is substituted.

Mutations removing title output or shared-owner interaction permission fail the
new causal regressions. Independent review closed its generated-help finding;
no remaining scoped findings were reported. Full-project unit/coverage and
publication gates were not rerun or claimed, and no files were committed/pushed.
The saved selected index and other user configuration were left unchanged.

## Deferred human review

Implementation and independent review are complete. The cold-start follow-up
also completed its technical checks and authorized reset; actual attended
multi-test acceptance remains in the child.

- [Cold native sign-in coalescing](implementation_plan_f4n2.md)
