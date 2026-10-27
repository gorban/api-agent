# Snapshot-only selected-profile reuse

[Bedrock requirements](implementation_plan_f4.md) · [Session persistence](implementation_plan_f4g.md)

The user approved retaining both native refresh state and an independently usable exported credentials snapshot. Ordinary cached reuse has human acceptance; snapshot-only reuse needs the qualification below.

- [x] Reuse a fingerprint-matched provider-owned selected-profile snapshot while its recorded expiry remains safely in the future. Verify the exact tuple with STS against the stored identity and any explicit assertions. Native cache deletion alone must not require browser login.
- [x] Preserve the real expiry and identity across setup/restart. Near-expiry or expired snapshots must use the selected native refresh path, then attended recovery only when credentials are specifically unavailable. The builtin adapter never launches recovery. Never change identity on denial, mismatch, malformed state, missing dependencies or network failure.
- [x] Preserve deliberately edited tuples, unrelated profiles and existing bounded cleanup/publication guarantees. Distinguish missing cache from malformed cache without accepting arbitrary errors as permission to log in.
- [x] Reconcile source-of-truth documentation: the verified, valid exported tuple serves requests; native profile/cache state supplies renewal. Both builtin and native adapters use the common snapshot-readiness rules.
- [x] Qualify isolated restart, cache-only removal, expiry, mutation and failure neighbors with focused causal tests and exact coverage for new code. Require separate adversarial and external review, package/resource and public-boundary checks.
- [x] Demonstrate real selected-profile snapshot-only STS/model listing without inference, with recoverable private backup before authorized cache removal. Stop for attended login when needed.

- [ ] Human acceptance through the ordinary launcher remains open; root auth/catalog checks do not establish new live inference or visible pricing acceptance.

## Qualification

Three Sol low lanes and distinct Sol high reviewers completed repairs, preserving
SDK profile/method/region precedence. The combined related run passed2,369 tests;
the final region repair passed1,372 provider tests with exact100% line/branch
coverage across2,319 statements and792 branches. All changed shared pricing/history
code is covered. Installed/relocated wheel/sdist and public-boundary checks passed.
External PR-style review approved the frozen candidate and independently reran59
guarded offline cases. Root removed only the selected native cache after private
backup: both adapters verified the default snapshot and Mantle listed60 models,
credentials stayed unchanged, and cache bytes were restored exactly. No inference
was invoked.
