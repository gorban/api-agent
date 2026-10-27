# Session factory selection and setup regression

[Bedrock requirements](implementation_plan_f4.md) · [Session reuse follow-up](implementation_plan_f4g.md)

The user reports that the ordinary LLM integration test fails during session acquisition with no unattended index, for both API formats. The reported stack hides the original acquisition failure behind a generic contract error. The user separately reports an API-key Completions pass; this does not qualify session acquisition.

- [x] Select direct AWS and IAM Identity Center recovery using separate BEDROCK_SESSION_PROVIDER factories. Retain aws_login.py:factory for direct login and add aws_sso.py:factory for Identity Center. Share acquisition/persistence internals without a second BEDROCK_LOGIN_METHOD setting. Explicit factory selection owns recovery; never silently switch adapters or identities.
- [x] Remove BEDROCK_LOGIN_METHOD from the contributed schema/runtime selection and update settings/help/documentation. Retain optional session-level SSO routing fields and request missing routing only when the SSO adapter needs recovery; arbitrary relative, absolute and installed factory paths must work without selector-string heuristics. Document migration from the earlier PoC method setting.
- [x] Reproduce and repair the session-setup failure with synthetic credentials/profiles and the ordinary setup boundary where possible. Keep credential/profile identity, persistence, bounded-worker and cleanup contracts intact. Expose only useful fixed diagnostic classifications or stack locations; never disclose credentials, native transcripts, endpoint response text or arbitrary exception messages.
- [x] Run focused regression and causal checks, exact line/branch coverage for all new code, distinct implementation review, current package/boundary checks and external PR-style review before a human retry. Preserve the successful API-key Completions report without treating it as a session pass.
- [ ] Human acceptance: ordinary launch.py session setup and existing integration test, with both API selections as separately authorized. Previously approved coordinator inference scope is exhausted; obtain new explicit approval before further real inference. Live authentication and persistence mutation require their own user checkpoint when needed.

No full-main acceptance is implied by focused PoC checks. The earlier follow-up remains historical evidence; its human acceptance does not close this newly reported regression.

## Reproduced startup defect

The native worker gained a top-level persistence import from api_agent but was
launched as a script without the parent source import path. With PYTHONPATH
unset in a normal source checkout it exits before emitting its protocol or
checking credentials. A matching before-prompt failure was reproduced without
any AWS call. Existing test processes inherited the source PYTHONPATH, masking
this boundary. The regression must exercise worker startup without that source
environment dependency, including installed/relocated resources.

The worker now anchors direct script execution to its own package root before
importing persistence. Source and installed-layout regressions fail without the
bootstrap and pass with it; distinct review also checks a poisoned unrelated
working directory. Both selector factories pass distinct loading/lifecycle
review. An authentication-only readiness check after the repair reaches normal
interaction-required behavior: cached credentials are unavailable and a fresh
attended recovery is needed. It performed no inference or browser handoff; this
is not a live session-reuse acceptance result.

## Focused qualification

The combined guarded provider run passed 1,235 focused tests with exact 100%
line/branch coverage across all 17 executable provider modules: 2,246 statements
and 764 branches. Distinct lane reviews and causal controls passed. Final external
PR-style review approved this bounded repair and selector migration. Boundary
checks and rebuilt wheel/sdist installed/relocated probes passed, including actual
packaged worker startup without inherited PYTHONPATH. An initial lane run hit an
existing process-disappearance race in a /proc cleanup test; its isolated rerun
and the final combined run passed. This does not qualify new live inference,
attended recovery/reuse or main adoption; human acceptance above remains open.
