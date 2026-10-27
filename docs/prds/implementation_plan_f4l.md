# Provider-selected reasoning and integration qualification

[Bedrock requirements](implementation_plan_f4.md) · [Workflow qualification](implementation_plan_f4b.md)

Deliver provider-selected reasoning, Studio and workflow choices without changing
application settings, selected authentication or explicit test overrides. Keep
workflow provenance strict and qualify the actual model/API/source fingerprint.
A supported reasoning control, observable token counters and a measured contrast
remain separate claims; partial or cap-reaching replies do not qualify a comparison.

- [x] Complete bounded model/control probes, typed provider-contributed selections
  and initial provider-specific calibration. Preserve exact statistics, complete
  observations and independent review; never reuse another provider's bounds.
- [x] Complete [reasoning deadline and concurrency repair](implementation_plan_f4l1.md).
  The earlier OSS120B Responses low/medium three-pair pass used a 16000-token cap
  and unchanged guards; it establishes bounded smoke behavior, not stochastic
  stability. Retain every failed comparison and its original scope.
- [x] Complete the earlier explicit-matrix checkpoint in
  [integration readiness](implementation_plan_f4l2.md). Its 14 Responses cases,
  one Completions workflow case and 20-graph calibration remain historical
  configuration-bound results, not acceptance of the current defaults catalog.
- [x] Complete [operation-scoped defaults and current qualification](implementation_plan_f4l3.md):
  Responses text/reasoning/Studio, native GLM5 Completions tools/workflow, isolated
  controls and exact source-bound calibration. Preserve strict tool/schema/truth
  assertions, usage, reporting and cleanup. The historical native-adapter catalog had
  17 core cases plus one provider UI case.
- [x] Complete the historical native-adapter workflow qualification and normal
  defaults catalog: 17 core cases plus one provider UI case. Retain failed runs.
- [x] Complete final package/boundary reconciliation and independent/
  external review; source checks and live passes do not close these gates.
- [x] Record conditional human acceptance of the current launcher follow-up
  after technical closure; retain earlier manual checkpoints in their details.

The historical native-adapter checkpoint passed its then-current calibration,
18-case catalog, exact applicable units and final artifact/external review.
The [current SDK-profile follow-up](implementation_plan_f4l3d.md) passed fresh
workflow and reasoning cases, exact units and final technical gates, and records
conditional acceptance. Its passes do not establish a new full 18-case run. Broader
authentication, platforms, capabilities and actual-charge accounting remain with their owning Bedrock requirements. Bounded inference
requires its own authorization; discovery or prior success does not extend it.

Earlier requirements, settings, failures, measurements and checkpoint statuses are
preserved verbatim in [historical reasoning/integration evidence](implementation_plan_f4l4.md).

**Deferred human review**

Substantive work is complete in the linked children; their explicit human acceptance remains unchecked and does not block subsequent work.

- [Integration readiness](implementation_plan_f4l2.md)
- [Operation-scoped profiles](implementation_plan_f4l3.md)
