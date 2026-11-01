# Runtime model display identity and Studio labels

[Runtime model support](implementation_plan_f4o.md) · [Future capabilities](implementation_plan_f1a2.md)

- [x] Parse recognizable discovered Claude family/version IDs, including current family-first IDs, dated revisions and older version-first IDs. Parse numeric GPT IDs without a named variant. Preserve exact model/profile IDs, routing scope and the existing authority for capabilities, limits and prices; display recognition grants none of these.
- [x] Keep full executable IDs and optional display metadata available through the generic catalog. A replaceable UI owns presentation; core must not replace IDs with shortened labels or merge routing targets.
- [x] In the included Studio, shorten Claude family labels to names such as `haiku`. Show routing suffixes only when needed to distinguish multiple visible targets for the same family; a sole US family appears as `haiku`, while multiple direct/US/global families appear as `haiku`, `haiku-us`, `haiku-global`. Versions remain simple numeric values such as `4.5` and `5.5`; duplicate exact versions remain disambiguated by full IDs. Existing opaque provider names retain their behavior.
- [x] Document that Haiku 4.5/5.5 use Runtime Converse in the current adapter. Responses and Chat Completions are not supported by these models, and Mantle Messages remains unimplemented. Changing labels must not introduce automatic route retry or hide AWS failures.
- [x] Verify discovery/filter/default selection, UI group labels, concrete selection roundtrips, source/installed/relocated resources, exact applicable unit/frontend coverage, causal controls, public boundary and external review.
- [ ] Human acceptance: filter to one Haiku route, then multiple routes; inspect simple versions, distinct labels and full selected identity. Complete a Haiku turn using Runtime Converse with an authorized account.

Public basis: [Haiku 4.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-4-5.html), [Haiku 5.5](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html), and [Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).
