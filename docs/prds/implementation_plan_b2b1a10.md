# Retry and conditional-route coverage qualification

[Workflow calibration](implementation_plan_b2b1.md)

Node presence alone does not prove retry or branch execution. The mandatory workflow coverage test must observe all six declared nodes, each root route (`skip`, `next_a`, `next_b`) and at least one actual internal component retry before reporting complete coverage.

- [x] Expand trial measurements with validated actual retry observations. Root routes and retry evidence must agree with executed structural events; never synthesize a retry from a configured maximum or repeated independent trials.
- [x] Stop only after every expanded target is observed; fail immediately on execution defects and fail with missing-target diagnostics at the reviewed cap. Preserve independent trial/turn identities and all recorded GRAPH executions.
- [x] Version the expanded calibration policy. Compute simultaneous exact one-sided 95% lower bounds across all required targets, a 99.9% conditional coverage target, and the existing fixed four-trial margin within the maximum ceiling of 60. Earlier node-only reports remain immutable historical evidence and cannot qualify the larger target set.
- [x] Preserve the existing prompt/model/version/reasoning distribution unless a separate change is agreed. A fresh bounded pilot and independent evidence review must justify the new cap; insufficient observations must fail qualification rather than weaken targets or relabel old measurements.
- [x] Deterministic tests reject all-node/green-only premature success, missing routes, fabricated retry evidence and tampered calibration; prove complete early stop, cap exhaustion and meaningful causal controls.
- [x] Complete source review, authorized live qualification and one ordinary workflow-coverage run, exact unit/package/boundary verification and independent/external review.
- [ ] Human acceptance confirms multiple trial GRAPH entries, the actual internal retry and all required conditional outcomes.

The reviewed GPT-6-Luna entry contains 60 successful independent graphs and a cap of 53 (minimum 49 plus the fixed four-trial margin). The normal coverage check reached all ten targets in four trials, including the observed retry. Bounds remain specific to the packaged fingerprint; other profiles require their own qualification.

**Deferred human review**

Implementation and required automated, independent and external reviews are complete. Only the explicit human acceptance above remains unchecked.
