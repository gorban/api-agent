# Selected ranges and whole-suite Allure viewing

[Allure viewers](implementation_plan_d1.md) · [Existing single-test contract](implementation_plan_d1a.md)

- [x] Allow the existing View in Allure action for an arbitrary selected range
  or all selected terminal integration/E2E results. Details remain scoped to
  their individual attempt. Exclude groups, units and text-entry focus.
- [x] Require every selected row to have a retained exact result mapping. Do not
  silently omit running, pending, missing or over-budget evidence. Never substitute
  a different attempt, latest run or the full suite for an unavailable subset.
- [x] Preserve single-result deep links. Multiple results open an immutable report
  containing exactly those selected result files, with deterministic set identity
  independent of row order. Native history and attachment bytes remain intact.
- [x] Build live selections only from verified immutable previews of the same
  repository, mode, run and start identity. Validate each manifest/file digest,
  reject conflicting artifact bytes and bound any combined files/count/bytes.
  Keep the derived preview explicitly pending; never invent finalized accounting
  or mix finalized and live copies. A common finalized snapshot is authoritative.
- [x] Keep archive.html as the separate complete finalized-run index. The primary
  notification URL targets the selected report. Preserve progress, configured
  port, browser ownership, test execution and existing cleanup/retry behavior.
- [x] Verify live and finalized subsets, all-results selection, immutable route
  identity, native history, exact attachments and exclusion of unselected tests
  using the actual Allure CLI. Complete applicable exact unit coverage, installed
  artifacts, boundary/license and independent/external reviews.
- [ ] Human review a selected subset and all completed results, including a
  completed subset while other tests continue.

**Deferred human review**

Implementation and required verification/reviews are complete. Only explicit
human acceptance above remains unchecked.
