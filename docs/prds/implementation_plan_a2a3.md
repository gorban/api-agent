# Passkey sign-in and direct app authorization

[Selected-login capabilities](implementation_plan_a2a2.md)

- [x] Start normal provider setup directly at application authorization, including
  selected logins with both password and passkey material. Prepare the software
  authenticator before navigation and retain the shared login workflow through
  password, offered passkey alternatives and final setup cleanup. Do not pre-login
  through ChatGPT. Other login and API-key paths stay intact.
- [x] Retain the independently tested first-party navigation helper as an isolated
  alternative, outside normal provider setup; it does not add a configuration
  setting or change the direct authorization entry point.
- [x] Advance on either a recognized nonempty, locally unexpired session cookie
  or a return to the exact ChatGPT origin without a visible Log in button after
  account submission. Do not treat initial hydration, CSRF/consent cookies or a
  similarly named host as success. These signals only advance browser navigation.
- [x] Pin username controls to their original document, use only offered passkey
  alternatives and preserve password/TOTP fallback when material supports it.
  Never retrieve email inbox codes or guess a passkey route.
- [x] Require the ordinary accepted callback, state/PKCE and signed token checks,
  scopes and selected-account binding before saving a connection. A ChatGPT
  session alone does not grant application access; renewed challenges remain
  subject to normal automation and intervention policy.
- [x] Read the application name from effective configuration through the normal
  provider snapshot and use that exact value for the new-registration hint and
  consent controls. Existing client registrations retain their server-side name.
- [x] Preserve nested deadlines, callback priority, cancellation and pending
  document/key/browser cleanup ownership. Keep credentials, cookies and complete
  authorization URLs out of diagnostics and recordings.
- [x] Verify session-signal alternatives, hydration/expiry/origin negatives,
  navigation races, same-page key scope, direct authorization without a preliminary sign-in,
  configured-name precedence and failed/partial setup. Renew applicable unit,
  installed-wheel and public-boundary gates before delivery.
- [ ] Human-review discovery-only direct registration with an uncached selected
  account configured with password and passkey; observe the offered post-password
  challenge. Make no inference calls to exercise authentication.

The first-party helper and its cookie/page handoff controls were delivered and
verified separately. Direct authorization is restored by user direction; live
post-password challenge behavior remains subject to the acceptance above.
