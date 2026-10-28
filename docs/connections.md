# Credentials and LLM connections

## Choose a provider

`LLM_PROVIDER` selects a replaceable factory (`file.py:factory`). api-agent
includes the [GPT provider](../src/api_agent/llm_providers/gpt/cfg/config.toml).
A resource pack can select another provider; explicit .env/ENV/CLI choices win.
The provider contributes its own settings and authentication roles to the TUI.

GPT offers subscription authentication with Responses, or API-key authentication
with a supporting configured endpoint. Subscription plus Chat Completions is
unsupported. API-key credentials are stored as a password-manager entry's
password, without a separate secret-bearing API-key configuration field.
API-key setup/transports are tracked in [the GPT plan](prds/implementation_plan_f2.md).

For OpenRouter, use an OpenRouter key in the selected password-manager entry,
set `GPT_AUTH_MODE=api_key`, `GPT_BASE_URL=https://openrouter.ai/api/v1` and
`GPT_API_MODE=completions`, and select `openai/gpt-6-luna`. This route supports
the structured response policy with compatible schemas; see the
[GPT response modes](../src/api_agent/llm_providers/gpt/RESPONSE_MODES.md)
for its restrictions and fallback behavior.

Authentication starts when an operation needs it. Help, collection and choosing
tests do not sign in or send inference requests. The shared `test_llm_discovery`
authenticates the selected provider and verifies remote catalog access without
inference. GPT probes the catalog anonymously first: a protected catalog is
retried with the selected credential; a public API-key catalog requires a
successful credentialed key-verification response. Subscription uses only its
fixed official endpoint and rejects a public catalog. No other user, cache or
authentication mode is tried on rejection.

A wrong password, revoked key or missing catalog permission fails this test with
the safe upstream explanation. Catalog access does not prove permission for
Responses or Chat Completions; their request tests check that separately. An
explicitly selected GPT must not acquire a universal catalog prerequisite.
Acquisition-only readiness remains an internal unit-tested checkpoint, not the
integration verdict. API-key acquisition itself makes no HTTP request.

## Attended and unattended login

Attended sign-in opens managed Chromium when needed. Unattended sign-in uses
configured password-manager entries; UI providers and LLM providers implement
their own login workflows. Setup is available from the main menu.

In **Setup unattended authentication**:

1. Choose the shared password manager: 1Password or Keeper, and its CLI path.
2. Add accounts under the displayed roles using exact vault and login/item names.
3. For roles that must be paired, place matching users at the same index.
4. Sign into the manager's CLI before requesting unattended authentication.

The saved `UNATTENDED_AUTH` object contains manager configuration and account
descriptors, not retrieved passwords or codes. Use the editor rather than
hand-writing JSON. **e** edits, **a** adds another account, **r** removes the
selected entry, and **[ / ]** reorder duplicate role entries.

Startup offers Setup or Skip when enabled roles/manager settings are incomplete.
**Remember my selection** under Skip saves `SKIP_UNATTENDED_SETUP=True`; delete
it in configuration cleanup to restore the suggestion. An explicit TUI_MODE
bypasses the startup suggestion. Attended login remains available.

## Select an account for a run

The integration picker displays its selected account and any paired labels.
**[ / ]** cycles eligible accounts and manual login; the choice saves to the
shown `.env` target. **s** starts with the effective selection.

`--unattended-user-index=` selects attended login for one run without changing
a saved index. ENV/CLI overrides take precedence over choices saved by the TUI.
Only roles needed by the operation participate; local authenticated UI may need
a full pair, while provider-only tests need only the LLM account.

Provider login is headless for unattended operations unless RUN_UI or DEBUG
requests visibility. Attended login is visible. Credential retrieval failure stops an explicitly
selected unattended operation, including interactive and DEBUG runs. Repair
password-manager access or choose manual login with **[ / ]** or
`--unattended-user-index=`. Browser security challenges may separately require
human intervention when interaction is permitted.

## Saved GPT subscription connections

`GPT_STATE_DIR` defaults to the expanded `~/.config/api-agent/gpt`, independently
of `GPT_APP_NAME`. It is reviewable and overrideable; an explicit blank retains
the same fallback. State is protected with owner-only permissions.

Attended setup can reuse a unique valid connection without a browser or vault
lookup. Unattended reuse first retrieves the selected entry's username to bind
the cached account; password/TOTP are fetched only when a login page needs them.
The workflow rejects a different verified account rather than overwrite it.
Expired access credentials use the provider's owned refresh mechanism.

`GPT_APP_NAME` supplies a display-name hint for initial registration. Changing
it does not rename an existing issued registration. No manual GPT_PROFILE is
required. Unrecognized pages/security challenges may still require intervention.

See [browser debugging](development.md#browser-debugging) for retained sessions
and agent inspection, and [session contracts](architecture/provider_architecture.md) for
ownership and cleanup.
