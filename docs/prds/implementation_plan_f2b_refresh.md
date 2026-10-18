# Cached subscription renewal and complete setup reports

[Lazy authentication](implementation_plan_f2b.md) · [Login workflow](implementation_plan_f2b_login.md)

- [x] Distinguish rejected refresh credentials (HTTP 400/401 from the refresh
  exchange) from transient transport/server failures. Retain fixed safe failure
  classifications; never forward refresh tokens, response bodies or arbitrary
  authentication exception messages.
- [x] Reuse valid cached access without a browser. After refresh rejection,
  renew through the existing provider-owned login workflow once per shared
  authentication attempt. Preserve the registered client/profile and selected
  account, scoped credentials, headed/headless policy, DEBUG inspection, bounded
  login components and cleanup receipts. Concurrent waiters share one renewal.
- [x] Attended renewal may request browser interaction; selected unattended
  renewal retains the same indexed credentials. With neither interaction nor a
  selected credential source, fail actionably without opening a browser. Do not
  launch login for transient server/transport failures or silently change users.
- [x] Cancellation, browser failure and failed renewal retain the existing owned
  disposal and retry boundaries. A second refresh failure after renewal is a
  named safe error, never an unbounded login loop or an unknown setup failure.
- [x] Treat setup failure/skip plus teardown as a complete pytest report without
  requiring a call phase. Share that predicate between streaming completion and
  final worker settlement. Missing setup/teardown or missing call after successful
  setup remains genuinely incomplete. Preserve the original failure/skip and
  Allure evidence for serial and parallel execution.
- [x] Verify controlled refresh rejection/recovery, identity and visibility,
  concurrency/cancellation and real pytest setup failures/skips. Complete exact
  applicable unit coverage, wheel/licenses and source/index/metadata checks.
- [x] Human-review the next expired-cache DEBUG sign-in and successful selected
  test. Controlled verification requires no paid inference.


## Verification

The isolated 32-worker gate passes 9,032 tests at exact measured line and branch
coverage. Controlled tests exercise rejected refresh through the real shared
session owner, four simultaneous waiters, serialized persistence of the same
profile, cancellation and explicit failure paths. Actual serial and parallel
pytest runs preserve setup failures/skips without incomplete-report diagnostics.
Seven independent mutations fail their intended assertions, including retaining
the refresh lock and dropping profile binding or report completeness handling.
The installed-wheel replay verifies HTTP400 renewal, serialized cache save and
disposal outside the checkout. Package, required licenses and public boundary
checks pass. No paid inference is needed for these proofs; live renewal and the
selected browser request remain human acceptance.

Human acceptance of the delivered authentication/DEBUG behavior is recorded. This does not claim additional challenge replay, native-dialog support or completion of separately deferred capabilities; new defects remain independent work.
