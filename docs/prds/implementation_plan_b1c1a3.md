# Native Allure workflow retry and error colors

[Recorded execution](implementation_plan_b1c1a.md) · [Complete payload follow-through](implementation_plan_b1c1a2.md)

Use native Allure status colors for workflow attention. An optimal successful execution is green throughout; an internal retry is a Broken/yellow step even when recovery succeeds and the test passes. This is an explicit presentation policy, separate from authoritative execution outcomes.

- [x] Derive each scope's native presentation status from observed execution: terminal failure is Failed/red; otherwise an actual internal retry is Broken/yellow; otherwise successful execution is Passed/green. Preserve existing cancelled/unknown distinctions. A configured retry allowance or a first attempt alone is not an actual retry.
- [x] Propagate terminal failure before retry attention to component and graph ancestors. Preserve each child occurrence's own state: an unaffected sibling remains green inside a yellow or red parent. Repeated node occurrences inside a component retry must not color an otherwise successful individual node visit merely because its enclosing component is retrying.
- [x] Keep original scope outcomes inspectable and unchanged in the canonical journal, with explicit native-step explanation for retry attention. Preserve final test status, immutable accounting, token/cost totals, visit/call identities, intervals and privacy. Do not infer unseen transport retries; successful tool-call continuations must not be classified as errors solely because the response asks to run a tool.
- [x] Keep paired journal/legacy projection merging consistent when attention evidence is available in only one projection or arrives in either order. Do not reject a legitimate projection solely because its derived attention is richer; continue rejecting conflicting execution identities, ownership or terminal outcomes.
- [x] Verify clean success, observed retry, terminal error, red-over-yellow precedence, nested ancestors, green siblings, cancellation, timed and untimed calls, paired projection order and finalized passing-test status with deterministic tests and causal controls. Complete applicable exact unit coverage, artifact/boundary and independent/external review before delivery. No new inference or statistical qualification is required for presentation-only changes.
- [x] Human acceptance of native workflow colors in the generated Allure report (2026-10-25).

Canonical Allure payload storage has its own [delivered contract](implementation_plan_b1c1a2a.md). Complete durable test logs and historical overlay UI remain separate incomplete requirements.

Human acceptance was recorded on 2026-10-25. Payload consolidation and historical overlays retain their separate acceptance gates.
