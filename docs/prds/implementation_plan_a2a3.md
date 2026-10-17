# Passkey sign-in and direct app authorization

[Selected-login capabilities](implementation_plan_a2a2.md)

- [x] Start normal provider setup directly at application authorization, including
  selected logins with both password and passkey material. Prepare the software
  authenticator before navigation and retain the shared login workflow through
  password, offered passkey alternatives and final setup cleanup. Do not pre-login
  through ChatGPT. Other login and API-key paths stay intact.
- [x] Remove the unused preliminary sign-in helper, its cookie/page handoff
  heuristics and its extra software-authenticator navigation permission. Keep a
  real-browser direct authorization regression without preliminary sign-in.
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
- [x] Verify origin restrictions, navigation races, same-page key scope,
  direct authorization without a preliminary sign-in,
  configured-name precedence and failed/partial setup. Renew applicable unit,
  installed-wheel and public-boundary gates before delivery.
- [x] On token-exchange HTTP 400 with recognized `invalid_grant`, discard the
  rejected code and retry authorization once using the issued client ID. Preserve
  the same host, page/context, selected login, original deadline and cleanup owner;
  rotate state, nonce and PKCE. Omit the initial registration name hint on retry,
  select only a unique offered account with the exact configured email under the
  recognized app heading, bind the callback and identity to the issued client, and save only fully
  verified credentials. Do not retry other failures or loop on a second rejection.
- [x] Preserve a fixed safe OAuth failure stage, HTTP status and recognized
  protocol error classification after an accepted browser callback. Never echo
  response descriptions, unknown error strings, codes, tokens or key material.
  Keep refresh behavior and cleanup ownership intact. Verify malformed and
  oversized responses, transport failures and credential-bearing error fields.
- [ ] Human-review discovery-only direct registration with an uncached selected
  account configured with password and passkey; observe the offered post-password
  challenge. Make no inference calls to exercise authentication.

Provider setup uses direct application authorization only. Shared password,
TOTP and software-passkey support remains available throughout that flow; no
preliminary ChatGPT session or cookie heuristic is required.
