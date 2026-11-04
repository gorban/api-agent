# Unit platform applicability

[Retained child exit and final release](implementation_plan_a9h3.md)

- [x] Tests may declare one `unit_platform(*platforms)` pytest marker using supported host names `linux`, `darwin`, or `win32`. Reject empty, duplicate, malformed, unknown or repeated declarations. Preserve validated platform metadata on each collected identity; retain all tests in discovery and selection.
- [x] When a selected unit excludes the current platform, do not run its body. Report it as `not_applicable`, with a fixed platform explanation, only after completed setup/teardown reporting and parent validation of the selected declaration and host. Failed teardown, timeout, cancellation, missing or conflicting evidence cannot become not applicable. Changed declarations between collection and execution reject qualification.
- [x] Ordinary skips, including missing dependencies or resources on an otherwise applicable platform, remain nonpassing for unit suites. Keep integration/E2E skip behavior unchanged. Do not classify applicability from a free-text skip reason.
- [x] Terminal and interactive results show a neutral not-applicable label, explanation and count, separate from passing, skipped and failing tests. Applicable pass ratios exclude not-applicable tests. A wholly not-applicable partial selection reports zero applicable tests rather than claiming passed tests; exact coverage remains an independent gate.
- [x] Migrate platform-specific fixtures without removing their real platform/resource guards. Preserve their real execution and rejection checks on supported hosts.
- [x] Verify serial and parallel child-report transport, declaration drift/forgery rejection, teardown failure precedence, ordinary skips, all-not-applicable selection, terminal/TUI presentation and unchanged coverage gates. Complete independent/external review, full applicable coverage and public-boundary checks before publication; actual native acceptance stays separate.

Verification: 26,814 unit tests pass on Linux with exact 100% line and branch coverage. Independent and external reviews pass, including targeted metadata and owned-path rejection mutations.

- [ ] Human acceptance on macOS using the normal unit launcher.

[Concurrency qualification and fixture ownership](implementation_plan_a9h5.md).
