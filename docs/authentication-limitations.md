# Authentication limitations

[Connections and credentials](connections.md) · [Bedrock setup](../src/api_agent/llm_providers/bedrock/README.md)

Automatic [Sign in with ChatGPT OAuth client registrations](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
support recognized username, password and authenticator-app TOTP steps through
the bundled Keeper and 1Password adapters. Hardware-key approval
([YubiKey®](https://www.yubico.com/), for example) stays manual: a detected
challenge in a headless login triggers one visible-browser retry, then
supported steps resume after your approval. Other security checks may also
need attention; native OS prompts cannot reliably be detected.

Choose attended login (no unattended user selected, or
`--unattended-user-index=`) when needed. No unattended subscription-user entry or
password manager is required. Managed Chromium opens when registration is needed;
the saved connection is reused and refreshed automatically. OpenAI documents
[one-hour access tokens and renewable 30-day refresh tokens](https://developers.openai.com/siwc/token-sharing-open-source/token-reference#token-lifetimes).
Successful refresh renews that 30-day window; expiry or revocation may
require signing in again.

## Passkeys and CLI recovery

**Keeper passkeys use a managed software authenticator.** The selected login's
matching P-256 credential is loaded only for sign-in and removed before DEBUG
browser retention. Presence/verification are software signals, not hardware-key
approval; positive signature counters and PRF-dependent authentication are not
supported. If a physical key is needed during software authentication, end
that attempt and retry attended, after its software authenticator is removed.
The site must offer a passkey route; email-only verification stays manual.
See [passkey support](prds/implementation_plan_a2a2.md).
The bundled 1Password CLI adapter has no verified passkey interface; contributions
using a supported interface are welcome. Manual passkey approval remains available.

**1Password CLI must already be signed in.** Credential retrieval is included;
interactive CLI login recovery is a [later task](prds/implementation_plan_a2a.md#deferred-login-recovery).
Keeper already supports interactive recovery. Native Windows recovery is
not implemented; the recovery transport currently requires POSIX.

**GPT API-key mode needs a configured password-manager entry.** Manual key entry
is [not implemented](prds/implementation_plan_f2.md); the attended-login
workaround above applies to subscription authentication.

## Bedrock and terminal authentication

Bedrock session mode first reuses valid selected SDK credentials. When attended
recovery is allowed, the default direct AWS adapter displays a browser URL and
accepts the returned authorization code; the optional SSO adapter instead uses
a device-code browser handoff. See [Bedrock sign-in](../src/api_agent/llm_providers/bedrock/SESSION_LOGIN.md).
Terminal integration can relay this session sign-in even with `NO_STD_IN` when
the launcher supplies a visible authentication channel. A missing/disallowed
channel cannot start hidden login. Bundled session login requires no unattended
user selected: pass `--unattended-user-index=` to override a saved key-account
index. Indexed API keys do not use session login or fall back to another identity.

Password-manager recovery is separate: unlocking the selected manager can still
require terminal input during `--no-std-in` integration execution. While an
authentication channel is open, ordinary progress, usage and failure output is
deferred until it closes, keeping passwords and provider URL/code prompts visible.
The terminal displays the same interaction title as the TUI, such as
`Authentication: Keeper` or `Authentication: AWS sign-in`, before the helper's
account-specific prompt. Authentication transcripts and replies are not put in
the ordinary output queue. EOF/cancellation fails explicitly without a fallback.

*YubiKey® is a registered trademark of Yubico AB.*
