# Diagnose unavailable dashboards before irrelevant login requirements

[Provider-owned tracing configuration](implementation_plan_k1a2c.md)

- [x] Resolve selected dashboard capability before requiring fields that are irrelevant when no dashboard is available. A stale ingestion endpoint after a profile change must produce the existing safe provider/profile/actual/managed-default diagnostic even if managed login fields have no default for that endpoint.
- [x] Keep genuinely admitted dashboard requirements strict. Preserve explicit remote dashboards, explicitly empty dashboard disabling, custom declarations and credential-safe diagnostics, with no backend-specific branches in core display logic.
- [x] Read raw diagnostic secrets only through valid nonempty declaration field names; optional absent password metadata must not become an invalid environment lookup. Exercise real environment-mapping key rules without changing process credentials.
- [x] Resolve and validate controller errors before side effects. Do not instantiate providers, allocate infrastructure, start browsers or change saved configuration to produce a rejection diagnostic.
- [x] Reproduce the bundled profile mismatch with only the two route settings, verify red-to-green diagnostics and meaningful rejection controls, complete independent and external source review, exact full unit coverage and public index/metadata checks.
- [ ] Human acceptance of the corrected unavailable-dashboard message remains required.

Verification: 26,915 full unit tests pass at 16 workers with exact 100% line and branch coverage. Independent adversarial and external source reviews pass after the custom named-endpoint regression was repaired. The actual launcher produces the actionable profile mismatch diagnostic without changing saved settings. Human acceptance of that message remains open.
