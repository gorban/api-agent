# Selected-login material and managed software passkeys

[Password-manager capabilities](implementation_plan_a2a1.md) · [Login intervention](implementation_plan_f2b_login.md)

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
- [x] Renew exact applicable unit coverage, wheel, boundary and mutation gates.
- [ ] Human-review the selected-login passkey route and retained-browser cleanup.

1Password passkey retrieval remains unsupported. Its CLI login-recovery work
retains its separate deferred schedule. No new inbox integration is included.

Verification: 9,187 unit tests pass at 32 workers with exact coverage of
22,203 statements and 8,398 branches. Installed-wheel, unrelated-working-directory,
synthetic browser signatures/navigation, independent rejection mutations and
public-boundary checks pass. Live selected-account acceptance remains separate.
