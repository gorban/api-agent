# Interactive unattended-authentication setup and credential helpers

## Checklist

- [ ] Add "Setup unattended authentication" as the last normal TUI mode. The wizard is interactive-only; reject its execution with `NO_STD_IN=True`. It configures credential retrieval through an already-authenticated password-manager CLI; it does not log into the password manager itself.
- [ ] On every interactive launch, before normal mode selection and execution-specific configuration prompts, check whether the required unattended-auth profiles are missing, empty, or incomplete. Suggest entering this wizard directly with an `s` option to skip and continue normal launch. Honor an explicit request for the wizard without offering it twice. Help and configured-mode collect-only retain their noninteractive short-circuits and never trigger the suggestion.
- [ ] Declare optional `UNATTENDED_AUTH` as a typed mapping stored in `.env` as compact single-line JSON with named profile keys and identified fields. Use proper `.env` quoting/escaping and the Python JSON parser; do not invent a delimiter parser. Store credential-source descriptors, not retrieved passwords or TOTP codes.
  - [ ] Always include the role "LLM User". Declare optional non-empty `UNATTENDED_SECONDARY_USER`; its value names an additional required profile when configured. Shared/demo default has no secondary role; selected packs may declare a secondary-role default. Allow explicit configuration such as `UNATTENDED_SECONDARY_USER="API User"` even when starting api-agent directly. Use the configured role label in the wizard without hardcoding provider-specific identities into shared code.
  - [ ] Each named profile requires `manager` (`keeper` or `1password`, chosen through an easy picker), `cli_path` (password-manager executable path), `vault` (non-empty string entry), and `user` (non-empty item/user name used by retrieval commands). The profile key is the role label; `user` is the actual password-manager item name, initially suggested from that label. Allow independent manager/path/vault choices per profile.
  - [ ] Parse, validate, and save the compact object through the shared `.env` editor. Retain explicit value precedence and show if environment/CLI configuration overrides the saved file. Require only the selected pack's named profiles; don't demand a secondary account for the default demo.
- [ ] Implement shared helpers for password and current TOTP retrieval using the exact commands below. Execute argument arrays without a shell, preserving vault/item names and executable paths containing spaces. Helpers rely on an existing CLI session and never run interactive login, unlock, or acceptance flows.
  - [ ] Close child stdin, allocate no interactive terminal, and supervise stdout/stderr concurrently. Detect login/unlock/input prompts or input-required failures promptly; stop the command and fail the TUI/helper invocation with a nonzero exit code, showing the command's captured console output and explaining that the user must log into the CLI first. Use a bounded noninteractive timeout for tools that stall instead of emitting a detectable prompt; never hang waiting for stdin or retry interactively. Apply this to each password and TOTP command independently.
  - [ ] Treat successful Keeper password/TOTP and 1Password TOTP output as raw values. Parse the 1Password password output with Python's CSV processor as exactly one cell, so embedded commas/quotes are handled correctly. Reject unsuccessful/invalid output rather than returning it as a credential.
  - [ ] Keep successful retrieved credentials in memory for the authentication consumer; do not print or persist them to `.env`, logs, or test artifacts. Captured failure/prompt output supplies the requested diagnostics.
- [ ] Make helpers available to LLM provider authentication and the selected pack's additional authentication adapter. Keep the password-manager retrieval layer separate from browser/API login logic and from TUI screens.
- [ ] Add deterministic tests for JSON round trips, role/default selection, independent profiles, exact command argument forwarding, single-cell CSV parsing, and stdin/prompt/timeout/nonzero failure handling. Use simulated CLI processes; do not require real vault access or credentials to validate this layer.

## Single-line .env example

```dotenv
UNATTENDED_AUTH='{"LLM User":{"manager":"keeper","cli_path":"/path/to/keeper","vault":"my-vault","user":"LLM User"}}'
```

When a secondary role is enabled, include a second profile under that role's label using the same four fields.

## Exact retrieval commands

Keeper password (raw output):

```text
<path_to_keeper_cli> find-password '<vault>/<name>'
```

Keeper TOTP (raw output):

```text
<path_to_keeper_cli> clipboard-copy '<vault>/<name>' --totp --output stdout -t
```

1Password password (CSV-formatted single cell):

```text
<path_to_op_cli> item get '<name>' --vault '<vault>' --fields password --reveal
```

1Password TOTP (raw output):

```text
<path_to_op_cli> item get '<name>' --vault '<vault>' --otp
```
