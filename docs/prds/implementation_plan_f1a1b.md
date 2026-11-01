# Useful demo revision feedback

[Declarative responses](implementation_plan_f1a1.md)

- [x] A rejected useful-demo reviewer must return the previous validated candidate and names of its failed checks through the existing declared feedback object. The next planner call must receive both, along with the unchanged original conversation, so it can revise the actual candidate rather than regenerate without context.
- [x] Preserve the four Boolean review controls, original-schema validation, correctness/grounding/relevance gates, final-pass preference rule, selected model and configured pass/deadline budgets. Do not introduce puzzle-specific instructions, additional automatic requests or an acceptance bypass. Successful review has no revision feedback; invalid tool preparation retains its existing safe failure path.
- [x] Snapshot the validated candidate before any tool-provider callback, so a provider that mutates arguments and rejects cannot rewrite revision feedback. Treat candidate feedback as ordinary workflow content: retain existing content controls, credential authority, capture and request ownership. Never execute generated code as part of review. Regenerate source-derived workflow views when native implementation provenance changes.
- [x] Prove the actual second planner request contains the prior candidate and failed checks, recovery can succeed after correction, rejection still stops before tool execution, and accepted turns omit revision feedback. Complete applicable unit/frontend coverage, causal controls, installed/relocated resources, public-boundary and external review.
- [ ] Human acceptance: inspect a rejected first attempt and the next planner request in Run local/Allure. A difficult puzzle may still exhaust its bounded passes; this change supplies revision context without guaranteeing model correctness.

## Verification

The final runtime revision passes 22,797 unit tests with exact 100% line and
branch coverage across all 334 measured files. The frontend passes 276 tests
and five coverage checks at exact authored line/branch coverage. Actual
second-request tests verify revision context, unchanged conversation, no tool
execution on rejection and recovery after correction. A mutating, rejecting tool
owner cannot rewrite the candidate snapshot. Process-local causal controls
detect missing context, a missing snapshot and stripped Claude branding.
Source and relocated distributions, installed native resources, no-Git workflow
checks and public-boundary review pass. Independent external source review
closes the argument-mutation finding. No additional live inference was made.

## Specific rejection details

- [x] Require a reviewer-generated `details` string alongside the four Boolean
  controls. Ask for concrete evidence and corrective guidance for every failed
  check. Forward this string unchanged through feedback into the next planner
  request, including line breaks and Unicode, subject to existing credential
  controls. Do not replace it with a generic summary.
- [x] Enforce a 4,096-character node limit without truncating explanations;
  missing, non-string, oversized or blank rejected-verdict details fail
  validation. Keep the native-compatible response schema, declared fallback,
  original gates, budgets and accepted empty feedback. A tool failure before
  review must not invent a model explanation or expose raw exception internals.
- [x] Verify actual retry requests on both direct-answer and rejected-tool
  paths, accepted turns, native/prompted schema validation, credential controls,
  and source/installed/relocated resources. Complete exact unit coverage,
  causal controls and independent external review.
- [ ] Human acceptance: inspect the exact reviewer explanation in the next
  planner input and its canonical GRAPH request evidence.

Ordered histories are owned by [shared parsed-flow inference history](implementation_plan_f1a1c.md).

## Detailed-feedback verification

Exact next-request tests preserve multiline and Unicode reviewer explanations,
reject malformed or oversized details, retain the candidate snapshot and keep
accepted feedback empty. Both direct-answer and tool-rejection paths are
covered, with native and permitted fallback schema validation unchanged.
Shared-history qualification and remaining human acceptance are recorded in
[ordered inference history](implementation_plan_f1a1c.md).
