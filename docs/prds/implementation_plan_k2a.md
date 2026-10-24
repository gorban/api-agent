# Dashboard configuration and viewer follow-through

[Startup dashboards](implementation_plan_k2.md)

- [x] Supply public, known development admin defaults for managed local LangFuse only. Preserve explicit remote credentials and explicit overrides; a reused account is not reset. Document the exposure in README and link the production image requirement.
- [x] Review configuration may show settings across modes, but conditional required labels must follow the effective enablement predicates. LangFuse admin settings must not be required for OTEL or disabled tracing; verify defaults, explicit empty values and actual execution separately.
- [x] Treat omitted and explicitly empty TRACE_VIEWER_URL identically: derive the managed local Jaeger URL from JAEGER_VIEWER_PORT. A custom collector never implies a viewer or starts local infrastructure. Explain a missing viewer with an actionable, non-content diagnostic; preserve explicit collector targets.
- [x] Verify managed Jaeger appears in startup before the final UI and in browser destinations, including a nondefault viewer port. Test actual HTTP readiness and existing origin restrictions, not only mocks.
- [x] Close causal regressions, applicable full coverage, installed checks and independent/external review. Keep deployment schema parity separate.
- [x] Human accepted Jaeger startup and visible traces, and conditional LangFuse required settings with the requested effective values/defaults, on October 23, 2026.
- [ ] Human retest: automatic LangFuse credential filling/login. Empty viewer default acceptance follows the new [local-output corrections](implementation_plan_k2b.md); earlier approval does not accept those corrections.

## Deployment dashboard credential defaults

- [ ] Under the [safe multi-stage runtime image contract](implementation_plan_h4.md), clear both development LANGFUSE_ADMIN_EMAIL and LANGFUSE_ADMIN_PASSWORD defaults to empty. Verify that selecting LangFuse requires explicitly supplied values under its supported runtime contract; do not ship or silently restore the known local development credentials in any runtime layer. Build/runtime image verification remains required.

## Retained backend coordination

- [x] Integration, E2E and Run local must resolve identical managed-local admin provisioning values for the same configured LangFuse backend, including explicit overrides. Switching modes must not reject reuse solely because dashboard settings were filtered out. Keep remote dashboard-login requirements scoped to actual Run local login.

## Conventional local target boundary

- [x] Keep managed LangFuse startup and development-default eligibility aligned: recognize exactly http://localhost:3000 and http://127.0.0.1:3000, each with no trailing slash or one trailing slash. Omission uses the normal mode default. Other valid URL spellings remain custom endpoints, require explicit credentials and start no managed stack. Preserve explicit overrides and OTEL behavior; test the actual resolver, declared defaults and profile selection together.
