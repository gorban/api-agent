# E2E confirmation attempts and Allure retries

[Testing and reporting](implementation_plan_d.md) · [Test interface](implementation_plan_a.md)

`E2E_RERUNS` defaults to two additional confirmations. Explicit empty/None and zero disable confirmations; ordinary configuration precedence applies. Integration and unit tests never repeat. Existing parallel worker counts apply independently to each confirmation wave.

- [x] Run each selected test once. Initial pass is final. Following an initial failure with allowance N > 0, require all N subsequent attempts to pass; stop that test immediately on an unsuccessful confirmation. For N=2: P passes; F/P/P recovers; F/F and F/P/F fail. Initial skips and interruption do not start confirmations; initial setup or execution errors receive confirmations when suite infrastructure remains healthy. Unsuccessful confirmations, including skips and setup errors, cannot count as passes.
- [x] Preserve logical first/final outcomes independently of physical attempt results. Return physical attempt ownership separately, retain first-attempt status on logical results, and report first/final rates over unique selected identities. Initial skips retain their explicit skip outcome. Infrastructure/callback failures cannot be repaired by functional confirmation.
- [x] Persist distinct Allure UUIDs, stable history identity and excluded attempt parameters. Latest recovered results are yellow/broken; first passes are green/passed; final failures are red/failed. Order physical results strictly even when clocks share a millisecond. Native retry grouping retains every earlier attempt; another launch remains History.
- [x] Persist effective `manifest.metadata.e2e_reruns` and authoritative `e2e_outcomes` with ordered raw statuses, physical result filenames, first/final outcomes, attempt count and completion state. Validate the protocol from actual recorded attempts before allowing completed finalization. Integration's any-failure guard remains intact. Legacy retry archives without authoritative policy remain explicitly uncertain.
- [x] Keep physical evidence/accounting ownership distinct from logical UI identity. Independent worker waves isolate sidecars and journals; every physical result has separate evidence, payload stores, workflow bindings, call accounting and cost attachments. Frozen artifacts retain all charges and tokens. Returned UI usage aggregates physical ownership to each logical test without modifying immutable accounting.
- [x] Complete applicable exact line/branch coverage, cancellation/cleanup regressions and independent review before E2E acceptance. Controlled tests must prove fail-fast ordering, initial/final rates, retained evidence/charges and native generated Allure presentation, without paid calls solely for verification.
- [x] Human approval of confirmation behavior and Allure outcome/retry presentation. Approval does not imply additional live observations.
- [ ] Separate selected-suite/parallel operation and archive navigation acceptance remains required.

Implementation evidence: `testing/confirmations.py` owns the deterministic protocol and presentation usage projection; `testing/integration.py` owns confirmation waves and aggregate lifecycle; `testing/recorded.py` retains physical evidence; `testing/recording.py` validates and projects native outcomes. Focused synthetic tests cover P, F/P/P, F/F, F/P/F, skipped confirmations, count validation and immutable attempt usage. Native Allure 2.46.1 generation was independently inspected for green P, yellow F/P/P with two Retries, and red F/F and F/P/F with one and two Retries. This evidence does not establish live browser acceptance or waive coverage/review gates.

Completed E2E selected-test reports include all verified physical attempts for
each selected logical test, preserving native Retries and the exact selected
attempt deep link. Selected subsets exclude unrelated test groups.
