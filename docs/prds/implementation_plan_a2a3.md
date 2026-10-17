# First-party passkey sign-in and app authorization

[Selected-login capabilities](implementation_plan_a2a2.md)

- [x] For a selected login with supported software passkey material, sign in
  through ChatGPT's offered first-party controls, then start ordinary application
  authorization in the same original managed page/context. Keep the authenticator
  installed until final setup cleanup. Other login and API-key paths stay intact.
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
  navigation races, same-page key scope, ordered first-party/OAuth handoff,
  configured-name precedence and failed/partial setup. Renew applicable unit,
  installed-wheel and public-boundary gates before delivery.
- [ ] Human-review discovery-only login through registration with an uncached
  selected account; make no inference calls to exercise authentication.

Verification: 9,289 units pass at 32 workers with exact measured line/branch
coverage. Installed-wheel controls, same-page browser/callback checks and focused
mutation controls pass. Live registration remains the unchecked acceptance above.
