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

## External trace-ingestion credentials

- [x] Explain LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY as the paired project API credentials used for trace ingestion, obtained from the selected project settings and distinct from dashboard login. Preserve managed-local development defaults. External targets have no key default: an empty string counts as provided under the generic resolver and would bypass its early missing-setting error, so do not add empty defaults solely for presentation.

## Reviewable trace-ingestion keys

- [x] Show LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY in Review configuration so missing external keys use the ordinary required-setting review and editing path, including their project-key help. Preserve managed-local defaults, explicit precedence, noninteractive missing-setting validation and startup blocking until required keys are supplied. Keep unrelated hidden settings hidden.
- [ ] Human confirm that an external or mismatched LangFuse target exposes both missing key rows in Review configuration and their help when edited. Existing tracing/dashboard approvals remain unchanged.

- [x] Keep the newly reviewable secret key confidential in terminal presentation: mask its value input and render only Configured for nonempty values in Review, default descriptions and Clear .env configuration, including refresh/reopen. Use generic strict Boolean sensitive metadata for text settings without enum choices; preserve exact resolution, persistence and execution values. Resource-pack overrides must inherit sensitivity, cannot remove it, and must reject incompatible effective enum choices. Verify default, entered and reopened synthetic secrets and rejection controls before independent/external recheck.
- [x] Before accepting input in a sensitive configuration editor, activate the existing private-input diagnostic guard. Protect existing/future native parsers and typed/pasted/changed/submitted events before debug files or devtools can retain them; keep ordinary diagnostics available and queued events safe after closing. Verify the real editor-to-guard join in a fresh process with synthetic keys.
