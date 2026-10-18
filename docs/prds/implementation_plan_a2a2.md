# Selected-login material and managed software passkeys

[Password-manager capabilities](implementation_plan_a2a1.md) · [Login intervention](implementation_plan_f2b_login.md)

[First-party sign-in and registration follow-through](implementation_plan_a2a3.md)

The managed software-authenticator path is explicitly supported for a selected
unattended login. It signs with the exported credential; browser-simulated
presence and verification do not prove physical-key approval. Physical keys and
unsupported native challenges retain their attended path outside active software
authentication. If a physical key is needed in software mode, finish cleanup and
start attended; same-operation physical-device handoff is not certified.

- [x] Add optional structured selected-login discovery, separate from string
  username/password/TOTP reads. Distinguish unsupported capability, absent
  material, ambiguous records and failed/locked manager access. Existing adapters
  remain usable. Keeper resolves only the configured vault/login by JSON UID
  lookup and privately reads that record. Validate account, relying party,
  credential/user identifiers and consistent P-256 key parameters.
- [x] Before choosing an observed alternative to email verification, inspect
  available material. Prefer an offered passkey route with a matching passkey;
  choose password only with a confirmed nonempty password. Never automate inbox
  retrieval or guess an unobserved authentication URL. Expose a clear unsupported
  or unavailable-route reason when neither automatic route can proceed.
- [x] Install an operation-scoped software authenticator in the original managed
  Chromium page before account submission, allowing capability-sensitive sites
  to offer passkeys. Scope imported material to the selected account and relying
  party. No material travels through worker readiness/catalog IPC.
- [x] Use explicit software presence/verification and backup defaults rather
  than claiming to reconstruct hardware or original backup flags. Reject positive
  signature counters; verify counterless support with two real synthetic browser
  signatures. Do not import PRF secrets or claim preserved PRF outputs. Provider
  rejection remains an error, not a reason to alter flags or identities.
- [x] Reject unsafe protocol logging before reading/importing material. Keep
  exported records out of recovery output and diagnostics. Register sensitive
  representations with the shared scrubber; use no key-bearing command arguments,
  files, traces or result artifacts. Trusted local browser control necessarily
  carries the key in memory; it is not a persistent evidence/export channel.
- [x] Remove the authenticator before retaining a DEBUG browser, including error,
  cancellation and callback completion. Failed cleanup retains ownership and a
  retry receipt. Preserve callback priority, nested deadlines and pinned-document
  credential entry. Verify no sibling page or later operation inherits the key.
- [x] Exercise supported/absent/unsupported/mismatched/malformed records, manager
  recovery, route preference, no-password routes, navigation races, timeout,
  cancellation, repeated assertions and cleanup through independent controls.
- [x] Allow ordinary supporting iframe documents while keeping every main-frame
  navigation/redirect restricted to the trusted origin or exact owned callback.
  Before document execution, enforce browser WebAuthn get/create permissions for
  self only; refuse ambiguous existing policies and preserve unrelated headers.
  Prove same-RP foreign-frame denial against a successful unrestricted control.
  Supply a standard status phrase when HTTP/2 omits it, preserving status and
  response headers/body; include real empty-phrase navigation controls.
- [x] Renew exact applicable unit coverage, wheel, boundary and mutation gates
  after the supporting-frame compatibility repair.
- [x] While a challenge is paused, do not repeatedly allocate/dispose document
  handles for a known absent password. Remember confirmed absence within the
  current workflow, not manager-access failures. Repeated polls keep the active
  component clock stable without suppressing real progress or cleanup events.
- [x] Human-review the selected-login passkey route and retained-browser cleanup.

1Password passkey retrieval remains unsupported. Its CLI login-recovery work
retains its separate deferred schedule. No new inbox integration is included.

Verification: 9,187 unit tests pass at 32 workers with exact coverage of
22,203 statements and 8,398 branches. Installed-wheel, unrelated-working-directory,
synthetic browser signatures/navigation, independent rejection mutations and
public-boundary checks pass. Live selected-account acceptance remains separate.

Inspection note: keep one observer connection alive while software authentication
is active. Disconnecting an additional CDP observer can disable Chromium's shared
virtual-authenticator environment. Wait for provider-owned authenticator removal
before disconnecting the observer. Do not inspect credential-bearing CDP payloads.

Supporting-frame/HTTP2 renewal: 9,241 units pass at 32 workers with exact
coverage of 22,265 statements and 8,424 branches. Installed browser and
independent policy/navigation mutations pass. A live selected-account attempt
reached email verification but offered only password despite available browser
passkey support; this does not establish live passkey acceptance.

Paused-route timing renewal: 9,245 units pass at 32 workers with exact
coverage of 22,270 statements and 8,426 branches. Real offline pages reproduce
the previous repeated cleanup clock; installed-package and independent mutation
controls verify stable polling, resumed routes and unchanged real cleanup.

Human acceptance of the delivered authentication/DEBUG behavior is recorded. This does not claim additional challenge replay, native-dialog support or completion of separately deferred capabilities; new defects remain independent work.
