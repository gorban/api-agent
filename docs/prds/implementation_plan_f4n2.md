# Coalesce cold native sign-in before clearing session state

[Terminal session handoff](implementation_plan_f4n1.md)

- [x] Serialize native session acquisition across separate owners/processes for
  the same saved credential file and AWS profile. Keep differing test/provider
  configurations isolated; do not share raw session objects or credential readers.
- [x] After waiting, recheck persisted credentials before admitting a login
  channel or native sign-in. A successful peer login should eliminate the second
  prompt. Preserve identity assertions and selected-index rejection.
- [x] Bound lock waiting by the existing acquisition deadline and cancellation.
  Release ownership after failure, cancellation and process exit. Never block a
  background thread indefinitely on a native file lock.
- [x] Verify concurrent owner reuse, distinct profiles, cross-process exclusion,
  failure/cancellation release and meaningful causal controls; independently review.
- [x] Only after the repair, clear the user-authorized credential file and exact
  selected-profile native cache into a private recoverable backup. Preserve
  unrelated configuration and caches. Do not copy secrets into repository files,
  test evidence, reports or ordinary diagnostic output.
- [ ] Human cold-start multi-test acceptance after cleanup.

The existing persistence lock serializes readiness and publication separately,
not the intervening interactive acquisition. Test-profile owners can therefore
both observe missing credentials and start separate sign-ins. A fresh normal
catalog-only launch reused the saved session without a prompt and cost $0.000;
the duplicate cold-start prompts do not establish a persistence failure.

## Verification

449 focused units pass with 100% line/branch coverage for native admission and
the acquirer. Bypassing admission in a process-local mutation causes the cold
parallel-owner regression to fail with two prompts. A real child process proves
cross-process exclusion and release on exit; cancellation and deadline controls
prove waiters do not retain ownership. Independent review found no actionable
issue. These checks do not claim a new full-project unit or publication gate.

After technical closure, the authorized local credential file and exact selected
native login cache were moved to a private recoverable backup outside the
repository. Their absence and unchanged AWS configuration were verified. Other
caches and coordination files were preserved. No live sign-in or inference was
performed after reset; the state is left cold for the user's multi-test check.
