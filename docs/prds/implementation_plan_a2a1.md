# Optional passkey authenticator support

[Password-manager adapters](implementation_plan_a2a.md) · [Login intervention](implementation_plan_f2b_login.md)

Scheduling: selected-login Keeper automation is pulled forward under the
[managed software-passkey requirements](implementation_plan_a2a2.md). Remaining
optional authenticator work follows application delivery and profiling. Existing password/TOTP automation and manual browser approval remain
available. Keeper is the planned passkey implementation; no 1Password CLI
passkey implementation is scheduled without a verified supported interface.
Contributions establishing that interface are welcome.

A passkey performs a WebAuthn challenge/response; it is not a password or TOTP
string. Physical security keys remain attended. Browser-visible challenges
can pause automation and resume supported login steps after human completion.
Native browser/OS prompts are not reliably exposed through page DOM selectors.

- [ ] Investigate supported observation of a pending native WebAuthn request
  without changing browser authentication behavior. DOM heading detection alone
  cannot establish that a native key prompt is open. Keep unknown/native cases
  explicit and require visible setup where reliable observation is unavailable;
  do not enable a virtual authenticator just to detect a real-key request.
- [ ] Establish each bundled manager’s supported scoped interface for finding
  and using a passkey on the already selected role/index login. Verify current
  CLI/SDK/browser integration capabilities separately; vault storage or export
  alone does not prove that a CLI can authenticate. Do not export entire vaults
  or assume that arbitrary password fields are passkeys. Preserve unsupported
  adapters without making this capability mandatory.
- [ ] Define an optional structured passkey-retrieval method on the manager
  interface, separate from username/password/TOTP strings. Distinguish unsupported
  capability, absent credential and a validated matching record; reject ambiguity.
  Keeper resolves the exact selected vault/login to a record UID, preferring
  machine-readable listing output, then captures only that record's passkey fields
  privately. Preserve shared deadlines, cancellation and login recovery. The
  1Password CLI adapter remains unsupported until an appropriate interface is
  established; browser passkey support does not establish CLI access.
- [ ] If a supported interface exists, define an optional authenticator
  capability carrying the relying party, exact trusted origin, challenge and
  selected account through the existing operation/deadline/cleanup boundary.
  Preserve the requested user-presence and user-verification policy; absence
  of support requires attended intervention, never a fabricated assertion.
- [ ] Retrieve or invoke only the selected login’s credential through its owning
  adapter. Where a manager exposes a passkey record, validate its relying party,
  credential ID, account user ID, signature counter and supported key parameters
  before signing. Capture CLI output privately inside the adapter; never display
  an unmasked record or treat the key as a password/TOTP return value.
  Never persist/export private key material, challenges, assertions or
  recovery input in configuration, IPC diagnostics, logs or Allure. Bind any
  browser integration to the original managed context and exact authenticated
  operation; disposal must revoke it. Managed software authentication follows the separately documented explicit
  software-presence policy; it does not establish physical user verification.
- [ ] Verify both bundled adapters and an unsupported synthetic adapter; test
  mismatched account/origin/relying-party, absent/ambiguous credentials, user
  cancellation, timeouts, cleanup and credential exclusion with independent
  rejection mutations. Require applicable exact unit coverage and independent
  review before enabling it.
- [x] Human acceptance of the currently delivered manager authentication. Future physical-key handoff enhancements retain their own implementation and native-review gates.

String helpers continue to support username/password/TOTP. The optional
[structured selected-login capability](implementation_plan_a2a2.md) is separate.
[Keeper’s passkey guide](https://help.keeper.io/article/268-what-is-a-passkey-and-how-does-keeper-support-them)
and [1Password’s browser guide](https://support.1password.com/save-use-passkeys/)
describe browser use; [1Password CLI fields](https://developer.1password.com/docs/cli/item-fields/)
do not establish a passkey-signing API. Keep implementation conditional on a
verified supported interface rather than adding an unusable credential kind.
