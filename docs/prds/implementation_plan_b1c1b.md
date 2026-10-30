# Execution-time workflow setting values

[Execution evidence](implementation_plan_b1c1.md) · [Recorded execution contract](implementation_plan_b1c1a.md) · [Historical overlays](implementation_plan_b1c2.md)

Extend the planned Allure execution-evidence format before historical overlay consumers depend on it. Existing timeout events and effective component pass limits are useful partial observations, not a general setting snapshot or an implemented historical settings overlay.

## Capture and identity

- [ ] Capture the effective, validated values actually consumed by an instrumented integration/E2E flow, including graph/component/node timeout settings, component pass limits and other explicitly approved graph settings. Read from the resolved execution configuration, not current TOML defaults or a later environment read at report publication. Capture the same values used for execution without introducing another resolution path.
- [ ] Bind each captured setting to its logical declaration key/owner, stable graph/definition and invocation identity, and applicable component/node scope. Retain type, unit and value state: omitted, explicit null, false and zero remain distinct where admitted by the setting's contract. Distinguish literal declarations, declared defaults and effective overrides; include safe source-kind provenance when available, never whole environment files, machine paths or secret override text.
- [ ] Separate configured scope timeout from the actual inherited/effective deadline budget and, where needed, remaining budget at scope entry. Associate per-visit/attempt budget observations with their actual scope rather than implying the configured limit restarted for a retry. Preserve unlimited/omitted and unavailable states distinctly. Link effective max passes back to its setting when configurable, rather than treating the declared default as the executed limit.
- [ ] Freeze invocation-level resolved settings with the invocation evidence. If a supported runtime legitimately consumes changed values later, capture each consumed revision at the affected visit/attempt; do not overwrite earlier observations or pretend one end-of-test snapshot covers all occurrences. Record only actually available resolution provenance and disclose gaps.

## Privacy and storage

- [ ] Use a bounded, owner-approved setting capture policy, not a dump of configuration or a rule that all numeric values are safe. Structural execution limits may be approved metadata independent of LOG_CONTENT; content-bearing application settings follow LOG_CONTENT and pre-emission scrubbing. Credentials, authentication/session material and unsafe setting names/values remain excluded even when LOG_CONTENT=True. Mark withheld/unavailable values explicitly; freeze schema versions, size/type/depth limits and policy before implementation.
- [ ] Persist the sanitized setting snapshot and scope observations in the same manifest-hashed Allure execution artifact as the resolved graph and events. Join by stable identities, reuse existing observations and preserve native-step/accounting consistency. Native Allure steps may expose approved setting parameters; saved static declarations and diagrams never acquire runtime values. Live diagnostics and historical viewers consume the same admitted evidence.

## Historical presentation and verification

- [ ] Overlay settings beside the declaration and the selected occurrence's captured inputs/outputs. For example, show a timeout key with the executed seconds and any parent-limited budget, or a max-pass key with the actual limit. Distinguish executed value from a source default. Older artifacts lacking a value display unavailable; never fill a historical gap from current configuration, environment, source defaults or a different invocation.
- [ ] Test identical declaration/commit hashes executed with different overrides, changed defaults after execution, nested tighter deadlines, omitted values, retries, multiple turns and repeated visits. Verify finalized artifact, native Allure parameter and overlay joins agree. Include legacy/missing data, capture revision, tampered identity and sensitive-setting cases; causal controls must detect default substitution, cross-invocation mixing and privacy bypass.
- [ ] Complete independent/privacy review and applicable exact unit, package/relocation and public-boundary gates, then human-review historical examples with differing timeout/pass limits. Controlled tests require no paid inference. Implementation and acceptance remain pending.
