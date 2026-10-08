# Personal subscription connections

This is the first part of the [GPT provider](implementation_plan_f2.md). Connection setup does not execute inference and is not a completed LLM factory. Keep subscription registrations distinct from password-manager API-key entries.

## Checklist

- [x] Generate fresh state, nonce and PKCE for each loopback sign-in attempt. Validate the callback and issued client before exchanging a code; verify signed identity and granted plan permission before saving a connection.
- [x] Store a stable per-installation host ID and separate protected account/client records. Preserve the previous connection on failed reconnect. Replace rotating credentials atomically and serialize refresh across processes.
- [x] Provide a connection command that opens the system browser after starting its callback listener. Offer list and explicit reconnect without exposing tokens. Close listener/client resources on completion, failure, timeout and cancellation.
- [x] Verify these boundaries with synthetic signed credentials and fake network/browser dependencies, including focused rejection mutations and exact unit coverage. Independent adversarial review precedes human sign-in acceptance.
- [ ] Review and complete one actual sign-in with the user. Confirm the account and plan permission without making an inference call. Record actual eligibility and callback behavior separately from deterministic evidence.
- [x] Integrate minimal account identification into provider setup: a unique saved OAuth connection is reused, an optional subscription login username binds unattended reuse, and attended OAuth identifies an account when needed. Keep cached profile IDs internal and subscription descriptors optional.
- [ ] Add optional vault password/TOTP assistance only for an observed supported login page; it cannot replace consent or make a connection complete by itself. Add an explicit cached-account picker/reconnect only when observed interactions require it.
- [ ] Integrate the connection getter/refresh owner with the eventual LLM factory, preserving explicit account selection and disposal. Add explicit sign-out/revocation and returning-account recovery before claiming the full session lifecycle.

## Setup contract

The supported flow is Continue with ChatGPT, followed by user sign-in and permission approval. A valid signed identity alone is insufficient: the token grant must allow ChatGPT plan usage. Subsequent runs reuse the provider's own protected credential record and refresh it when necessary. Reauthorization preserves the registered client and host; changing accounts must not overwrite another registration.

The initial command uses the operating system browser. No additional executable is required for this connection foundation: HTTP and signed-token libraries already belong to the managed Python environment. Automated login-page assistance and the complete TUI connection manager remain separate tasks.

The provider-specific code lives in `src/api_agent/llm_providers/gpt/`; shared core carries opaque sessions and does not interpret this provider's tokens. The auth-only factory and its lazy launch path are covered by [the next checklist](implementation_plan_f2b.md). Inference transports and API-key vault acquisition remain in the parent checklist.

## Internal connection diagnostic

Normal user setup occurs lazily from launch.py when a selected integration requests provider setup. The module commands below are optional development diagnostics, not user prerequisites.

From the repository root with its Python environment prepared:

```bash
source .venv/bin/activate
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect --help
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect list
PYTHONPATH=src python -m api_agent.llm_providers.gpt.connect connect
```

`connect` opens the system browser for sign-in and permission consent. It saves only a verified connection and makes no inference request. `list` prints saved profile IDs and account emails without making an authentication request. To reconnect one existing account, run `connect --profile PROFILE_ID` with its listed ID.

Records default to `~/.config/api-agent/gpt`; `--state-dir PATH` selects a different protected directory. The directory must belong to the current user with mode 0700, and credential files use 0600. The default sign-in timeout is 180 seconds; `--timeout SECONDS` accepts 1 through 600. Expired or revoked grants, account eligibility and live browser behavior still require human acceptance; passing synthetic tests does not establish those outcomes.
