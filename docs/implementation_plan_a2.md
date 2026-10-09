# Interactive unattended-authentication setup and credential helpers

## Checklist

- [x] Add "Setup unattended authentication" as the last normal TUI mode. The wizard is interactive-only; reject its execution with `NO_STD_IN=True`. It configures credential retrieval through an already-authenticated password-manager CLI; it does not log into the password manager itself.
- [x] On interactive launches without an explicit startup TUI_MODE, before normal mode selection and execution-specific configuration prompts, check whether unattended-auth configuration is absent/empty or its entries are incomplete. An empty role array is valid attended fallback but still incomplete unattended setup. Skip the suggestion when the user has remembered Skip, or when every active role has at least one valid account and manager/path are configured; unequal nonempty role counts are allowed. Suggest entering this wizard directly with an `s` option to skip and continue normal launch. Honor an explicit request for the wizard without offering it twice. Explicit startup TUI_MODE overrides this suggestion and default acknowledgement; help and configured-mode collect-only retain their noninteractive short-circuits. The explicitly selected unattended-auth mode opens the wizard directly.
  - [x] Place an unchecked "Remember my selection" checkbox directly under Skip. Only choosing Skip with it checked saves `SKIP_UNATTENDED_SETUP=True` to the current .env target. Declare False by default with `acknowledge_default=false`; suppress only the startup suggestion, preserving required unattended credentials and manual wizard access. Unchecked Skip, Setup and closing do not persist the choice. Clear .env configuration exposes the saved assignment for deletion, restoring the suggestion.
- [x] Declare optional `UNATTENDED_AUTH` as a typed object with one shared `password_manager` object and `roles` mapping from role labels to account arrays, stored in `.env` as compact single-line JSON with identified fields. Use interoperable escaped dotenv quoting and the Python JSON parser; generated escape comments distinguish newly encoded assignments from untouched legacy literal quotes/backslashes without executing or expanding values; do not invent a delimiter parser. Store credential-source descriptors, not retrieved passwords or TOTP codes.
  - [x] The selected LLM provider declares its credential roles through the generic provider interface. Selected resource packs and API providers may register additional roles and configuration through their declarations/adapters; neither shared schema nor demo declares pack-specific secondary roles. The credential wizard consumes those declarations. Selected API-provider extensions own upstream signing and identity validation.
  - [x] The shared `password_manager` object requires `manager` (`1password` or `keeper`, chosen through an easy picker) and `cli_path` (password-manager executable path). Both defaults are blank and display as `-`; choosing 1Password or Keeper conditionally defaults the CLI path to `op` or `keeper`. Allow incomplete draft configuration to be stored while requiring configured manager/path for unattended retrieval. Each entry in a named role array requires only `vault` and `user` (non-empty strings); `user` is the exact login/item name, initially suggested from the role label. Allow different vault/item names per account and add/edit/remove accounts individually. Combine the shared manager with account descriptors only when retrieving credentials.
  - [x] Confirming the password-manager dropdown focuses CLI executable path, including when reselecting the current manager. Enter in the path field or on **Save value (Enter)** saves; Enter on Cancel cancels without saving. Initial picker values and dismissed choices do not advance focus.
  - [x] Enter on a selected account-role or password-manager row opens Edit, including an empty role row for its first account; existing account values are prefilled.
  - [x] Size the password-manager row from its current header/value text before rendering, so a newly saved CLI path is fully displayed immediately while the table remains focused. Preserve horizontal scrolling for paths wider than the viewport.
  - [x] Show a **Source** column for every account-role row and the shared manager, with component-specific provenance and the same launcher-relative `../` path formatting as Review configuration. Switching the write target does not move stored values or change their Source. Saving an edit updates the affected component's Source; environment/CLI overrides retain their effective values and sources. Explicit empty role arrays still show their saved source. Size both tables before drawing so new source paths are displayed correctly immediately.
  - [x] Permit `.env` target switching inside account/manager editors as well as before opening them. With multiple valid targets, expose `s Switch .env` and a switch button; preserve unsaved input and update the selected target paths. Saving moves only the edited role array or manager from its former `.env` to the chosen one, retaining unrelated components. Cancel writes nothing. Pack-only roles retain their single forced pack target.
  - [x] Parse, validate, and save the compact object through the shared `.env` editor. Retain explicit value precedence and show if environment/CLI configuration overrides the saved file. Validate only the selected pack's named role arrays for setup. At operation time, filter to the consuming roles before strict parsing; explicit attended operation bypasses unattended retrieval validation, and test LLM-account selection ignores unused secondary arrays. Don't demand a secondary account for the default demo.
  - [x] Restrict save targets for pack-defined roles to the active pack's `.env`, including add/edit forms. Restore the user's previous shared-role target on returning to LLM User. Save and move only the edited role, preserve other roles/unrelated settings, and merge distinct role arrays across agent/pack `.env` files (pack overrides duplicate role arrays; ENV/CLI still override the whole mapping). Read/remove legacy pack roles saved in the agent file. Guard pack-only targets in the storage backend and preserve original files/symlinks on failure.
  - [x] Keep the account list as the configured-account display; omit the redundant Effective source sentence. Place the right-aligned stored-entry pointer note beside the title with a leading asterisk. Parenthesize workflow shortcut keys and color only those keys to match the footer. Put account add/vault/login-item/save instructions before shared-manager editing instructions.
- [ ] Resolve the user for each authentication role when its consuming operation requests unattended login, not globally for unused roles. Declare optional zero-based integer `UNATTENDED_USER_INDEX` shared by credential arrays for the roles needed by the operation; it has no implicit first-user default.
  - [ ] Resolve accounts only for roles consumed by the operation. If a required role has no entries, use the provider or resource pack attended fallback. Offer only indexes present in every required role array; automatically select the sole complete pairing, otherwise use an explicit valid index or ask. Unequal array lengths are permitted. Validate integer type and bounds without switching users. The LLM provider and resource pack own their separate authentication strategies.
  - [ ] With multiple entries and no configured index, pause to ask which user to use, displaying each entry's `<vault>/<user>` alongside its zero-based index and role label. Choose one shared index present in every needed role array, showing the paired accounts and validating bounds in each array; resource-pack API authentication adapters use only their declared credential profiles. Route request-time choices to the running TUI instead of blocking a server worker on stdin.
  - [ ] If `NO_STD_IN=True`, ambiguity is an immediate nonzero operation failure identifying the missing index setting; do not select the first user, prompt, or read stdin. Zero-entry attended fallback remains an auth/UI challenge and does not read terminal stdin. Help/collect-only/unit paths do not resolve login choices.
  - [ ] Exclude the shared index field from startup default acknowledgement and automatic default-to-`.env` persistence, like the debug exception. The test picker explicitly saves account choices to `.env`; explicit ENV/CLI indices still override them. Do not auto-save or default an index at startup. Index selection must not rewrite or reorder the stored credential arrays. Keep the chosen identity stable across retries/refreshes for its session.
- [x] Keep `UNATTENDED_AUTH` out of the general configuration cleanup list; the dedicated mode owns its editing and deletion. Remove bulk `x` Clear and `t` attended actions. `r` removes just the selected account, preserving other accounts and roles; removing the last account saves an explicit empty role array for attended login. Environment/CLI overrides remain unchanged and are explained in the view.
- [x] Display accounts in a minimum-five-row table (including its header) that grows with added users and scrolls internally when constrained. Place a one-row shared-manager table after one spacer line; reserve horizontal-scrollbar space and keep the manager visible without an outer scrollbar when the terminal can fit both minimums. Recompute sizing after resizing and form close.
  - [x] On the manager row, permit `e` Edit and `r` Remove, which deletes saved manager/path assignments while preserving accounts and effective ENV/CLI precedence. With no higher-precedence manager, blank defaults become effective. Disable account Add on this row. Manager forms contain only manager/path; account forms contain only vault/login-item. Open Add/Edit on a separate page with a bold operation/role title, one blank line before the first label and before the second field label, visible Save/Cancel controls and readonly target paths. Preserve list geometry, scroll position and row selection while editing; cancel without writing, keep errors and typed values in the editor, and return to the correct selected table after saving. Shared-manager targets use the previous shared-role target; pack account forms remain pack-only. Keep step 1 `(s) Choose .env target` visible regardless of target count or selected role. Outside edit forms, the shortcut remains available with a single target and switches it to itself without changing the remembered shared target.
  - [x] Allow `e` Edit on an unconfigured role row to create its first account, alongside `a` Add; Add creates an additional account when that role already has entries. Editing existing accounts starts with the last saved vault/login-item values. Connect down/j from the last account row to the manager and up/k back to the last account row. Keep the account table first and the manager table last in Tab order; exclude the outer scroll container from focus. Use "Enter vault name" and "Enter login/item name" in the guide.
  - [x] Show `[` Move up and `]` Move down only when the selected role has multiple accounts, keeping both hints visible at first/last indexes. Persist the reordered role array, preserve the same selected account after reloading the effective array (including ENV/CLI overrides), stop at role boundaries and preserve other roles and targets. Hide these actions for the manager, singleton/blank roles and active edit forms.
  - [x] Read uniform legacy per-account manager/path objects without rewriting on launch. An explicit manager choice migrates them to the shared schema, preserving role order and account descriptors. Conflicting legacy manager choices require selecting one shared manager before saving accounts; never silently discard a choice or overwrite account data.
- [x] Implement shared helpers for password and current TOTP retrieval using the exact commands below. Execute argument arrays without a shell, preserving vault/item names and executable paths containing spaces. Helpers rely on an existing CLI session and never run interactive login, unlock, or acceptance flows.
  - [x] Close child stdin, allocate no interactive terminal, and supervise stdout/stderr concurrently. Detect login/unlock/input prompts or input-required failures promptly; stop the command and fail the TUI/helper invocation with a nonzero exit code, showing the command's captured console output and explaining that the user must log into the CLI first. Use a bounded noninteractive timeout for tools that stall instead of emitting a detectable prompt; never hang waiting for stdin or retry interactively. Apply this to each password and TOTP command independently.
  - [x] Treat successful Keeper password/TOTP and 1Password TOTP output as raw values. Parse the 1Password password output with Python's CSV processor as exactly one cell, so embedded commas/quotes are handled correctly. Reject unsuccessful/invalid output rather than returning it as a credential.
  - [x] Keep successful retrieved credentials in memory for the authentication consumer; do not print or persist them to `.env`, logs, or test artifacts. Captured failure/prompt output supplies the requested diagnostics.
- [x] Make helpers available through scoped LLM credentials and use them in the bundled authentication-only provider, separate from TUI/browser login logic.
- [ ] Join the selected pack's additional local authenticated-UI adapter without copying retrieval helpers.
- [x] Add `get_username` alongside password/TOTP retrieval. The saved descriptor's `user` identifies the vault login/item; it is not necessarily the actual login username. Retrieve the username on demand using the exact commands below without adding a redundant stored username or changing existing descriptor files.
  - [x] Parse 1Password username stdout as exactly one CSV cell through Python's CSV processor; keep Keeper username stdout raw, removing only the CLI output terminator. Reject missing/empty or malformed results. Preserve names/paths and significant returned content using argv execution, never a shell.
  - [x] Reuse closed-stdin, prompt detection, bounded timeout, process-group supervision and nonzero-exit handling for both username commands. Keep actual usernames in memory and scrub them from diagnostics, traces and report attachments; entry labels already used by the setup UI do not authorize logging fetched identity data.
  - [x] Extend role-bound and LLM-bound in-process readers to allow username explicitly alongside password and TOTP.
  - [ ] Extend versioned credential IPC to allow `username` explicitly alongside `password` and `totp`. Reject all unsupported kinds before invoking helpers; preserve role/index, deadline, revocation and closed-owner guards. Do not let a new kind fall through to TOTP retrieval.
  - [x] Add deterministic argv/CSV/raw-output/error tests, including entry name differing from retrieved username, spaces/quotes/commas, no empty credential, no cross-role retrieval and no real vault access. Helper tests pass and the observed GPT browser-login consumer is implemented; other consumers and live-operation credential revocation remain separate tasks.
- [ ] Add deterministic tests for JSON round trips, individual removal, manager reset and empty-role attended transition, role/default selection, zero/one/many entries, configured and explicitly persisted picker index selection, noninteractive ambiguity failures, shared manager and per-account vault/item descriptors, legacy migration, exact command argument forwarding, single-cell CSV parsing, and stdin/prompt/timeout/nonzero failure handling. Use simulated CLI processes; do not require real vault access or credentials to validate this layer.

## Single-line .env example

```dotenv
UNATTENDED_AUTH='{"password_manager":{"manager":"keeper","cli_path":"/path/to/keeper"},"roles":{"LLM User":[{"vault":"my-vault","user":"LLM User"}]}}'
```

When a secondary role is enabled, include another array within `roles` under that role's label, using only `vault` and `user` per entry. The shared manager serves all accounts. An empty array (`"LLM User":[]`) selects attended login. Multiple complete selectable indexes require an explicit zero-based index or interactive choice at authentication time. Local-run pairing stops at the shortest active role array; integration/E2E require only the primary LLM account for unattended eligibility and retrieve its credentials through the LLM provider callback; resource-pack authentication owns any additional test identity setup.

## Exact retrieval commands

Keeper username (raw output):

```text
<path_to_keeper_cli> clipboard-copy '<vault>/<name>' --field login --output stdout
```

Keeper password (raw output):

```text
<path_to_keeper_cli> find-password '<vault>/<name>'
```

Keeper TOTP (raw output):

```text
<path_to_keeper_cli> clipboard-copy '<vault>/<name>' --totp --output stdout -t
```

1Password username (CSV-formatted single cell):

```text
<path_to_op_cli> item get '<name>' --vault '<vault>' --fields username
```

1Password password (CSV-formatted single cell):

```text
<path_to_op_cli> item get '<name>' --vault '<vault>' --fields password --reveal
```

1Password TOTP (raw output):

```text
<path_to_op_cli> item get '<name>' --vault '<vault>' --otp
```

- [ ] Keep selection of needed credential roles synchronized through `UNATTENDED_USER_INDEX` only. Preserve paired array ordering; never independently pick a secondary index. Incomplete requested pairings retain attended fallback; validate a selected shared index against every array consumed by the operation, including single-entry arrays, and fail invalid pairings rather than silently using another account. Test selection explicitly persists one shared index for complete enabled-role pairs; attended Start overrides its use for that invocation.

- [ ] Complete the contributed-index contract: providers or packs may declare optional UNATTENDED_USER_INDEX, including LLM-only multiple accounts. Bare core/demo does not declare it; active help/schema includes it only when contributed. The earlier secondary-pack-only restriction is superseded by [provider architecture](provider_architecture.md).

## B2 helper validation

- [x] Verify configurable Keeper and 1Password executables through bounded version checks (Keeper 18.1.6; 1Password 2.40.0), without accessing real vault credentials. Deterministic fixtures verify retrieval commands and output parsing. Never classify successful raw password stdout as a login prompt; stderr prompts stop promptly, while stdout-only stalls rely on closed stdin and the bounded timeout. Kill the helper's owned process group on failure/timeout.
- [x] Implement the credential configuration wizard and deterministic profile/index selection backend above.
- [ ] Wire operation-time account picking and browser integration; wizard/helper implementation alone does not complete authentication.

## Next implementation slice — credential configuration

- [x] Implement and validate a reusable credential-profile schema before browser automation: role-to-array structure, shared-manager and account field types, supported managers, nonempty descriptor values, zero-entry attended selection and synchronized account-index bounds. Use the existing `CredentialSource` and retrieval helpers; keep stored values to descriptors only. Preserve generic pack role registration.
- [x] Replace the unattended-auth operation-unavailable screen with the actual account add/edit/remove wizard with a separate shared-manager table in `src/api_agent/tui/auth_setup.py`, using the shared target .env editor, source precedence, navigation and literal table rendering.
- [ ] Human-review saved compact JSON, per-role empty arrays, multiple entries, target selection, shared-manager editing/reset and individual account removal as part of the remaining full human authentication checkpoint; the observed GPT login sequence is already implemented. Duplicate role/account buttons were removed in favor of footer shortcuts and a read-only numbered guide (1–5 left, remaining steps right; narrow terminals stack the steps). Short read-only guidance covers Keeper/1Password login creation, password/optional TOTP, exact vault/item names and matching account indexes. Account Save/Cancel remain visible. Rendered-screen and resize regressions pass; human walkthrough remains pending.
- [x] Implement deterministic zero/one/many and synchronized-index selection in `src/api_agent/auth/profiles.py`.
- [ ] Finish request-time, local-run and E2E consumers of the selection backend. Bare core/demo does not declare an index; selected providers may contribute it. Integration selection and observed GPT login are already implemented.

Deletion is one selected account or the shared manager at a time; there is no duplicate bulk Clear action. Deleting saved configuration cannot override explicit ENV/CLI descriptors. Restore and clamp both account-table scroll axes after edits, rather than retaining only the outer viewport. On Linux/macOS, successful raw CLI retrieval removes exactly the tool's output terminator while preserving password content. Windows Keeper translation of embedded password newlines remains unverified.

## Review regression maintenance

- [x] Keep account selection attached to its effective descriptor through save/reorder when ENV/CLI overrides the saved array; do not select another effective account by a persisted index.
- [x] Preserve and clamp both inner-table scroll axes through account/manager rebuilds.
- [x] Preserve raw password trailing CR/LF on Linux/macOS by removing one CLI output terminator; retain single-cell CSV parsing for 1Password.
- [x] Delete only the selected saved account or shared manager, preserving ENV/CLI effective descriptors and provenance. There is no bulk Clear action.

- [x] Follow the requested reorder destination for effective saved arrays, including adjacent identical account descriptors; preserve descriptor occurrence when ENV/CLI leaves the effective array unchanged. Verify both directions and subsequent removal.
- [x] Escape accepted Unicode line separators in generated single-line dotenv values, including U+0085, U+2028 and U+2029. Preserve ordinary readable Unicode, legacy literal backslashes and unrelated assignments through shared/source-only reload and later edits.

- [ ] Wire the pair picker to the active provider/pack-contributed index with no default and exclusion from startup acknowledgement. Explicit test-picker selection may save it to .env; attended Start overrides it for that invocation. Generic ambiguity choices may remain session-only. Validate only roles consumed by the selected operation; do not require a secondary role for an LLM-only test.

## Completed integration account selection

- [x] Bind the integration picker to the selected-provider index declaration, expose [ / ] account/manual cycling and labels, and save explicit choices to the displayed .env while honoring ENV/CLI overrides. Empty --unattended-user-index= selects attended for one invocation. Use only the primary LLM role for integration; unused secondary roles do not block it.
- [x] Verify zero/one/many profile selection, index/type/kind guards, JSON/legacy migration, individual account/manager edits, source/target moves and supervised helper failures with deterministic tests. Verify the selected username binds cache reuse and observed fresh GPT login through [lazy setup](implementation_plan_f2b.md).

The broad operation-selection entries above remain open for request-time choices, local full-pair UI handoff and E2E wiring. Integration DEBUG is the only currently implemented terminal-free integration execution exception.
