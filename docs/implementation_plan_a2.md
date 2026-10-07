# Interactive unattended-authentication setup and credential helpers

## Checklist

- [ ] Add "Setup unattended authentication" as the last normal TUI mode. The wizard is interactive-only; reject its execution with `NO_STD_IN=True`. It configures credential retrieval through an already-authenticated password-manager CLI; it does not log into the password manager itself.
- [ ] On every interactive launch, before normal mode selection and execution-specific configuration prompts, check whether unattended-auth configuration is absent/empty or its entries are incomplete. An explicitly configured empty array for a role is a valid attended-login choice, not a malformed profile. Suggest entering this wizard directly with an `s` option to skip and continue normal launch. Honor an explicit request for the wizard without offering it twice. Help and configured-mode collect-only retain their noninteractive short-circuits and never trigger the suggestion.
- [ ] Declare optional `UNATTENDED_AUTH` as a typed mapping from role labels to arrays of credential-source entries, stored in `.env` as compact single-line JSON with identified fields. Use proper `.env` quoting/escaping and the Python JSON parser; do not invent a delimiter parser. Store credential-source descriptors, not retrieved passwords or TOTP codes.
  - [ ] The selected LLM provider declares its credential roles through the generic provider interface. Selected resource packs and API providers may register additional roles and configuration through their declarations/adapters; neither the shared schema nor demo declares pack-specific secondary roles. The generic wizard consumes these declarations without knowing provider-specific field names or login behavior. Private API provider extensions own upstream signing credentials and identity validation.
  - [ ] Each entry in a named role array requires `manager` (`keeper` or `1password`, chosen through an easy picker), `cli_path` (password-manager executable path), `vault` (non-empty string entry), and `user` (non-empty item/user name used by retrieval commands). The array key is the role label; `user` is the actual password-manager item name, initially suggested from that label. Allow independent manager/path/vault choices per entry, and let the wizard add/edit/remove multiple entries for each role.
  - [ ] Parse, validate, and save the compact object through the shared `.env` editor. Retain explicit value precedence and show if environment/CLI configuration overrides the saved file. Validate only the selected pack's named role arrays; don't demand a secondary account for the default demo.
- [ ] Resolve the user for each authentication role when its consuming operation requests unattended login, not globally for unused roles. Declare optional zero-based integer `UNATTENDED_USER_INDEX` shared by credential arrays for the roles needed by the operation; it has no implicit first-user default.
  - [ ] With zero entries, skip automatic login and use the selected provider's attended authentication flow. With one entry, select it automatically. With multiple entries and an explicit valid index, select that entry. Validate integer type and bounds instead of guessing or silently switching users.
  - [ ] With multiple entries and no configured index, pause to ask which user to use, displaying each entry's `<vault>/<user>` alongside its zero-based index and role label. Choose one shared index for all needed nonempty role arrays, showing the paired accounts and validating bounds in each array; resource-pack API authentication adapters use only their declared credential profiles. Route request-time choices to the running TUI instead of blocking a server worker on stdin.
  - [ ] If `NO_STD_IN=True`, ambiguity is an immediate nonzero operation failure identifying the missing index setting; do not select the first user, prompt, or read stdin. Zero-entry attended fallback remains an auth/UI challenge and does not read terminal stdin. Help/collect-only/unit paths do not resolve login choices.
  - [ ] Exclude the shared index field from startup default acknowledgement and automatic default-to-`.env` persistence, like the debug exception. Interactive account choices are session-only and must not be saved into `.env`; explicit ENV/CLI indices are supported. Index selection must not rewrite or reorder the stored credential arrays. Keep the chosen identity stable across retries/refreshes for its session.
- [ ] Keep `UNATTENDED_AUTH` out of the general configuration cleanup list; this mode owns its display, editing, and deletion. Add `x` to clear unattended-auth configuration with a `y/n` confirmation; only `y` confirms, and any other response leaves it unchanged.
  - [ ] On confirmation, remove saved `UNATTENDED_AUTH` assignments from the active `.env` file through the shared editor, clear current unattended credential/profile and selected-index state, and return to regular attended authentication for this invocation without immediately re-suggesting setup. Preserve unrelated configuration and the pack's secondary-role declaration.
  - [ ] Show the configuration source when externally supplied environment/CLI profiles exist. Do not attempt to modify the parent environment or original CLI arguments; use attended authentication for the current invocation after clearing, and explain that those external overrides must be removed to keep subsequent launches attended.
- [ ] Implement shared helpers for password and current TOTP retrieval using the exact commands below. Execute argument arrays without a shell, preserving vault/item names and executable paths containing spaces. Helpers rely on an existing CLI session and never run interactive login, unlock, or acceptance flows.
  - [ ] Close child stdin, allocate no interactive terminal, and supervise stdout/stderr concurrently. Detect login/unlock/input prompts or input-required failures promptly; stop the command and fail the TUI/helper invocation with a nonzero exit code, showing the command's captured console output and explaining that the user must log into the CLI first. Use a bounded noninteractive timeout for tools that stall instead of emitting a detectable prompt; never hang waiting for stdin or retry interactively. Apply this to each password and TOTP command independently.
  - [ ] Treat successful Keeper password/TOTP and 1Password TOTP output as raw values. Parse the 1Password password output with Python's CSV processor as exactly one cell, so embedded commas/quotes are handled correctly. Reject unsuccessful/invalid output rather than returning it as a credential.
  - [ ] Keep successful retrieved credentials in memory for the authentication consumer; do not print or persist them to `.env`, logs, or test artifacts. Captured failure/prompt output supplies the requested diagnostics.
- [ ] Make helpers available to LLM provider authentication and the selected pack's additional local authenticated-UI adapter. Keep the password-manager retrieval layer separate from browser/API login logic and from TUI screens.
- [ ] Add deterministic tests for JSON round trips, confirmed/cancelled clearing and attended-mode transition, role/default selection, zero/one/many entries, configured/session-only index selection, noninteractive ambiguity failures, independent profiles, exact command argument forwarding, single-cell CSV parsing, and stdin/prompt/timeout/nonzero failure handling. Use simulated CLI processes; do not require real vault access or credentials to validate this layer.

## Single-line .env example

```dotenv
UNATTENDED_AUTH='{"LLM User":[{"manager":"keeper","cli_path":"/path/to/keeper","vault":"my-vault","user":"LLM User"}]}'
```

When a secondary role is enabled, include another array under that role's label using the same four fields per entry. An empty array (`"LLM User":[]`) selects attended login. Multiple entries require an explicit zero-based index or an interactive choice at authentication time.

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

- [ ] Keep selection of needed credential roles synchronized through `UNATTENDED_USER_INDEX` only. Preserve paired array ordering; never independently pick a secondary index. Zero-entry arrays retain attended fallback; validate a selected shared index against every nonempty array consumed by the operation, including single-entry arrays, and fail invalid pairings rather than silently using another account. Interactive selection chooses one session-only index for the needed arrays.
