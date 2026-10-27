# Mantle list-price quotes and compatible predictions

[Bedrock cost requirements](implementation_plan_f4c.md) · [Bedrock requirements](implementation_plan_f4.md)

The user approved published AWS list-price estimates and compatible history/picker use. These estimates are not invoice charges. Billing-service reconciliation remains a separate proposal and is not authorized by this phase.

- [x] Supply a bounded provider-owned USD/token rate snapshot derived from official regional AmazonBedrock public price files. Match exact model, Mantle route, region and Standard tier; preserve publication/source/revision/verification provenance. Unsupported or ambiguous dimensions remain unavailable. Never borrow direct-vendor rates.
- [x] Quote complete observed input/output usage without inventing missing cache/reasoning counts or counting inclusive reasoning twice. Unknown subdivisions may still permit a total only where qualified rates make every possible subdivision equivalent. Retain unknown component visibility.
- [x] Freeze the rate card and non-secret compatibility with per-call accounting. Treat session, access-key and bearer-key authentication as metered AWS access independent of login mechanism; do not fabricate reported charges.
- [x] Extend shared validation, current-card selection and compatible history/picker repricing through generic contracts. Preserve original historical evidence, provider/region/route/tier separation, and explicit unavailable values. Warm help/configuration/prediction display must remain free of authentication, network and billing calls.
- [x] Cover distinct synthetic provider rates, incomplete/mismatched usage/cards, history compatibility, packaged resources and installed/relocated behavior. Require focused causal checks, exact new-code line/branch coverage, separate adversarial review and external PR-style review.
- [ ] Obtain human acceptance through the ordinary launcher results/predictions. Any new real inference needs explicit spending approval; auth/catalog readiness alone is insufficient.

Public price files require no account billing permission. Cost Explorer/CUR totals are delayed aggregates and need separate access/configuration and cost approval; they cannot establish an exact synchronous per-test invoice amount.

## Bounded implementation evidence

The bundled card qualifies OSS20B and OSS120B in us-east-1 through either supported
Mantle API, with the documented omitted/default/standard tier semantics. Observed
model/tier/revision survives decoding and stream reconstruction; contradictory
stream metadata fails, and unsupported tiers remain unpriced. Current provider
rates take precedence over stale or wrong-provider estimator overrides. Separate
Sol high review passed the provider and shared-history repairs. Focused aggregate
and package/external qualification are coordinated with the snapshot phase; no new
real inference or billing call establishes live pricing acceptance.

Final qualification:2,369 related focused tests passed, followed by1,372 final
provider tests at exact100% line/branch coverage. Changed shared code, causal
controls, installed/relocated resources and boundary checks passed. External
PR-style review approved the frozen candidate after59 independent offline cases.
Human live-price acceptance and broader cost requirements remain open.

The OSS120B extension is tracked with [reasoning qualification](implementation_plan_f4l1.md).
It uses the same public regional offer and inclusive-token interpretation;
34 focused pricing cases, exact line/branch coverage and distinct Sol high review
passed. The newer external acceptance is recorded in that follow-up. Earlier
external approval above applies to the earlier OSS20B scope.
