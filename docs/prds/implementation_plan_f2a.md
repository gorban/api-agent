# Personal subscription connections

This is the first part of the [GPT provider](implementation_plan_f2.md). The authentication-only factory and connection setup are implemented; they do not execute inference or complete the LLM capabilities. Keep subscription registrations distinct from password-manager API-key entries.

## Checklist

- [x] Generate fresh state, nonce and PKCE for each loopback sign-in attempt. Validate the callback and issued client before exchanging a code; verify signed identity and granted plan permission before saving a connection.
- [x] Store a stable per-installation host ID and separate protected account/client records. Preserve the previous connection on failed reconnect. Replace rotating credentials atomically and serialize refresh and reconnect writes across processes with the same profile lock; browser waiting stays outside that lock, and reconnect lock acquisition remains asynchronous and bounded. Treat scopes as a permission set while preserving identity/host/expiry validation.
- [x] Provide a connection command that opens managed Playwright Chromium after starting its callback listener. Offer list and explicit reconnect without exposing tokens. Close listener/browser/client resources on completion, failure, timeout and cancellation; retain cleanup ownership until disposal is confirmed.
- [x] Verify these boundaries with synthetic signed credentials and fake network/browser dependencies, including focused rejection mutations and exact unit coverage. Independent adversarial review precedes human sign-in acceptance.
- [x] Complete actual sign-in with the user: fresh-cache DEBUG and non-DEBUG unattended setup verified signed account/plan permission, accepted callback and protected persistence without inference. See [observed completion](implementation_plan_f2b.md#observed-unattended-login-completion--october-8-2026). Configured history publication and the full human checkpoint remain separate.
- [x] Integrate minimal account identification into provider setup: a unique saved OAuth connection is reused, the required unattended subscription login username binds cache reuse, and attended OAuth identifies an account when needed. Keep cached profile IDs internal. Plain startup suggests setup for missing account/manager configuration; Skip allows attended login, and explicit TUI_MODE retains its bypass.
- [x] Declare provider-owned GPT_APP_NAME, default api-agent independently of resource-pack naming, and pass it through lazy setup to initial registration's display-name hint. Allow ordinary pack/config overrides; require nonblank text for initial registration. Reauthorization reuses the issued client ID without resending the display hint, and changing the setting does not rename an existing grant.
- [x] Add optional vault username/password/TOTP assistance for observed supported login pages, plus both observed consent actions, without treating a submitted page as verified completion. Preserve current-page/origin/account guards and cancellation. See [workflow evidence](implementation_plan_f2b.md).
- [ ] Add an explicit cached-account picker/reconnect only when observed interactions require it.
- [x] Integrate the connection getter/refresh owner with the authentication-only factory, preserving selected account binding and awaited disposal.
- [ ] Join the getter to discovery/inference and add explicit sign-out/revocation and returning-account recovery before claiming the full session lifecycle.

## Setup contract

The supported flow is Continue with ChatGPT, followed by user sign-in and permission approval. A valid signed identity alone is insufficient: the token grant must allow ChatGPT plan usage. Subsequent runs reuse the provider's own protected credential record and refresh it when necessary. Reauthorization preserves the registered client and host; changing accounts must not overwrite another registration.

GPT_APP_NAME supplies the initial application display-name hint and defaults to api-agent, including when a resource pack selects this provider. It is not an account/client identifier or an automatic copy of the pack name. Keep the chosen application name consistent across installations; an existing registration retains its issued client ID and is not renamed by a configuration change.

Attended consent uses managed Playwright Chromium in a visible window, independently of RUN_UI. The launcher prepares the Python browser dependency through its development group; immediately before browser work, prepare the version-matched Chromium binary if missing. Run the browser alongside the loopback listener rather than relying on the system-default browser's loopback permissions. Cached-account reuse does not allocate a browser. Observed automated login-page assistance is complete in the next checklist; the full TUI connection manager and unobserved login variants remain separate tasks.

The provider-specific code lives in `src/api_agent/llm_providers/gpt/`; shared core carries opaque sessions and does not interpret this provider's tokens. The auth-only factory and its lazy launch path are covered by [the next checklist](implementation_plan_f2b.md). Inference transports and API-key vault acquisition remain in the parent checklist.

## Internal connection diagnostic

Normal user setup occurs lazily from launch.sh when a selected integration requests provider setup. The module commands below are optional development diagnostics, not user prerequisites.

From the repository root with its Python environment prepared:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect --help
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect list
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect connect
```

`connect` opens managed, visible Chromium for sign-in and permission consent; the diagnostic environment must include the browser dependency. It saves only a verified connection and makes no inference request. `list` prints saved profile IDs and account emails without making an authentication request. To reconnect one existing account, run `connect --profile PROFILE_ID` with its listed ID.

Records default to `~/.config/api-agent/gpt`; `--state-dir PATH` selects a different protected directory. The directory must belong to the current user with mode 0700, and credential files use 0600. The default sign-in timeout is 180 seconds; `--timeout SECONDS` accepts 1 through 600. Expired or revoked grants, account eligibility and live browser behavior still require human acceptance; passing synthetic tests does not establish those outcomes.
