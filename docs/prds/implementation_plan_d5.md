# E2E confirmation attempts and Allure retries

[Testing and reporting](implementation_plan_d.md) · [Test interface](implementation_plan_a.md)

`E2E_RERUNS` exists; the application E2E runner remains planned. The base defaults to two additional confirmation attempts; the shared declaration defaults to zero. Integration and unit tests never use this allowance. Empty values are currently rejected by the integer declarations.

- [ ] Accept empty/None and zero as disabled. Preserve normal precedence and the configured nonnegative number of additional confirmations; do not interpret explicit empty as the default allowance.
- [ ] Run each selected test once. Initial pass is final. Following initial failure with allowance N > 0, require all N subsequent attempts to pass. Stop immediately on the first unsuccessful confirmation. For N=2: P -> pass; F/P/P -> pass; F/F -> fail after two attempts; F/P/F -> fail after three. Skips, setup errors and interruption cannot count as successful confirmations.
- [ ] Report first-attempt and final pass rates separately over unique test identities, not attempts. A recovered F/P/P contributes positively only to the final rate. Preserve explicit skipped/not-run/interrupted counts, authoritative group outcome, UI and exit status.
- [ ] Record every attempt with a distinct UUID and stable test/history identity, excluding the attempt counter from history discrimination. Verify every rerun appears in native Allure 2 Retries; another launch belongs in History. Keep parameterized cases distinct. Verify final Allure status agrees with the complete confirmation protocol, including aborted confirmation and intermediate success.
- [ ] Include every executed attempt's tokens and charges in accounting, retaining test/turn/call ownership. Preserve acceptance, suite exclusion and cleanup through final publication.
- [ ] Prove fail-fast ordering, both rates, cancellation/cleanup and actual generated Allure presentation with deterministic controlled tests before E2E acceptance. No paid calls solely for retry verification. Renew applicable exact coverage, installed/native artifacts and independent review when implementing.

The current integration recorder rejects completed finalization if any recorded attempt failed. Extend this through an explicitly validated E2E aggregate outcome when implementing confirmations; keep the integration failure guard intact. Native retry grouping alone does not implement E2E recovery or final-rate semantics.
