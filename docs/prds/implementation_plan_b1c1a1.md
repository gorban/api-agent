# Canonical native Allure scope projection

[Recorded execution contract](implementation_plan_b1c1a.md) · [Execution-evidence transition](implementation_plan_b1c1.md)

A recorded graph can emit both a versioned projected journal and a runner evidence packet containing the same scope lifecycle. Current native Allure projection renders both trees. The retained journals and immutable call accounting remain accurate, but the visible duplicate trees can imply extra executions or duplicated elapsed work. Native step presentation is not accepted until this follow-through closes.

- [ ] During the execution-evidence/trace-format transition, project one canonical native Allure scope tree per actual invocation, using stable invocation/scope identities rather than names, timestamps or display order. Retain both original evidence attachments and their provenance.
- [ ] Handle journal-plus-runner packet order and repeated delivery explicitly. Preserve distinct invocations, repeated node visits and retries, partial/failed/cancelled evidence, and diagnostics for conflicting identities or outcomes. Do not hide a real execution or invent success while suppressing duplicate representations.
- [ ] Preserve exact leaf-call/accounting attribution and inclusive timing/cost semantics. Native display, finalized result attachments and immutable accounting must describe the same executions without charging or counting duplicate evidence as another call.
- [ ] Reproduce the paired journal/runner witness in controlled finalized Allure output; cover both packet orders, distinct same-time invocations, partial failures and causal regression controls. Independently verify privacy, applicable exact coverage, installed artifacts and public boundaries. No paid inference is required solely for this repair.
- [ ] Human acceptance of the rendered native step hierarchy after the implementation and independent review. Static declaration/diagram acceptance does not close this separate gate.
