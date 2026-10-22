# api-agent

Build and run API-connected agents from one guided terminal interface.

- Review settings and save reusable configuration.
- Adapt applications through resource packs and replaceable providers.
- Run locally with connected UI/API services, logs and optional tracing.
- Select tests, inspect failures and browse recorded results.
- Keep DEBUG browsers open for human or agent inspection with Chrome DevTools MCP
  or Playwright MCP.
- Even allows hooking up a password manager for automated login to UI and LLM
  provider.

## Get started

Use Python 3.13 or newer. Source-checkout browser viewing and the included Studio
also need one explicit preparation with Node.js 24 or newer: run `npm ci` from
the repository root. They work offline afterward; installed Python distributions
include those assets, and graph execution alone does not require npm.

Then run from this repository directory:
```bash
./launch.py
```
The launcher prepares its local environment and dependencies. Follow the prompts
and select **Run local** in the main menu to start the app.

In **Review configuration**, set `RUN_UI=True` to launch the connected UI too.
For packs offering a managed upstream API, set `API_ENDPOINT` to a remote URL,
or save an explicit blank to launch that API locally alongside the agent.
`AGENT_DJANGO_URL` is the agent's own address, not the upstream API setting.
See [setup and configuration](docs/workbench.md#getting-started).

Run local opens one headed browser when `RUN_UI=True` or `DEBUG=True`: agent
home first, the optional prepared UI second, and the [actual LangGraph flow](flow.md.html) last.
With both settings false, it prints the home and flow URLs without opening a
browser or authenticating an LLM. To use the same operation without the TUI:

```bash
./launch.py --tui-mode run-local --no-std-in
```

Press Ctrl+C to stop the terminal operation; use Stop or Quit in the TUI.
Shutdown waits for owned requests and resources to finish cleanup. This initial
Run local release requires an empty `TRACE_MODE`; tracing dashboard startup and
login follow separately. UI-process logs show safe activity messages rather than
arbitrary child output, which may contain credentials even with `LOG_CONTENT=True`.

## Guides

- [Settings and TUI controls](docs/workbench.md)
- [Credentials and LLM connections](docs/connections.md)
- [Resource packs and providers](docs/resource-packs.md)
- [Running, debugging and testing](docs/development.md)
- [Workflow diagrams and browser inspection](docs/workflow_viewer.md)

**Developing with a coding agent?** Start with Python 3.13+ and an agent with
terminal access. For browser inspection, configure Chrome DevTools MCP (preferred)
or Playwright MCP. See [developer setup](docs/development.md#developer-environment-and-builds)
for prerequisites and connection instructions.

## Known issues

- Automatic [Sign in with ChatGPT OAuth client registrations](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
  support recognized username, password and authenticator-app TOTP steps through
  the bundled Keeper and 1Password adapters. Hardware-key approval
  ([YubiKey®](https://www.yubico.com/), for example) stays manual: a detected
  challenge in a headless login triggers one visible-browser retry, then
  supported steps resume after your approval. Other security checks may also
  need attention; native OS prompts cannot reliably be detected.
  - **Workaround:** choose attended login (no unattended user selected, or
    `--unattended-user-index=`). No unattended subscription-user entry or
    password manager is required. Managed Chromium opens when registration is needed; the saved
    connection is reused and refreshed automatically. OpenAI documents
    [one-hour access tokens and renewable 30-day refresh tokens](https://developers.openai.com/siwc/token-sharing-open-source/token-reference#token-lifetimes).
    Successful refresh renews that 30-day window; expiry or revocation may
    require signing in again.
- **Keeper passkeys use a managed software authenticator.** The selected login's
  matching P-256 credential is loaded only for sign-in and removed before DEBUG
  browser retention. Presence/verification are software signals, not hardware-key
  approval; positive signature counters and PRF-dependent authentication are not
  supported. If a physical key is needed during software authentication, end
  that attempt and retry attended, after its software authenticator is removed.
  The site must offer a passkey route; email-only verification stays manual.
  See [passkey support](docs/prds/implementation_plan_a2a2.md).
  The bundled 1Password CLI adapter has no verified passkey interface; contributions
  using a supported interface are welcome. Manual passkey approval remains available.
- **1Password CLI must already be signed in.** Credential retrieval is included;
  interactive CLI login recovery is a [later task](docs/prds/implementation_plan_a2a.md#deferred-login-recovery).
  Keeper already supports interactive recovery. Native Windows recovery is
  not implemented; the recovery transport currently requires POSIX.
- **API-key mode needs a configured password-manager entry.** Manual key entry
  is [not implemented](docs/prds/implementation_plan_f2.md); the attended-login
  workaround above applies to subscription authentication.

## Testing

Choose `unit`, `integration`, or `e2e` as `<tests_type>`:
```bash
./launch.py --tui-mode "<tests_type>"
```

All tests start selected. Add positional selectors to preselect tests, or adjust
selection in the TUI. Append `--no-std-in` for terminal automation, or export
`NO_STD_IN=True` for multiple commands. Integration requires `--start=True` or `--debug`;
E2E requires `--start=E2E` as human acceptance. These tests may spend tokens.
With `DEBUG=True` and more than one distinct integration or E2E test selected,
execution asks for literal `y` before opening browsers, including with `--no-std-in`.
Set `DEBUG=False` and omit `--debug` to avoid the prompt.
See [test controls and automation](docs/development.md#testing).

### For full coverage

Run all unit tests without selectors; require every test to pass and 100% line
and branch coverage. Some integrations also need every valid configuration:

- `GPT_API_MODE`
  - `responses`
  - `completions`
- `GPT_AUTH_MODE`
  - `api_key`
  - `subscription`
    - Subscription not supported with `GPT_API_MODE=completions`.

Run the full integration suite once with your valid effective settings:
```bash
./launch.py --tui-mode integration --no-std-in --start
```

Then repeat only the LLM discovery tests for the other valid combinations—two more
runs for GPT. For example, when this differs from your first run:
```bash
./launch.py --tui-mode integration --no-std-in --start \
  --gpt-auth-mode=subscription --gpt-api-mode=responses \
  api-agent::tests/integration/test_llm_discovery.py
```

Discovery checks remote credential acceptance and catalog access without inference.
API-specific request tests also need coverage under each applicable mode.

## Markdown workflow PoC

The [workflow guide](docs/workflow_poc.md) covers the restricted Markdown grammar,
inference-free validation and the permanent mandatory node-coverage fixture.
Model-specific bounds are packaged with the fixture; [calibration refresh](docs/workflow_calibration.md)
is explicit maintenance outside normal test collection. Normal coverage needs no
local pilot file and rejects unsupported provider/model/settings without substitution. The useful GraphQL demonstration remains
the next phase.

Open [flow.md.html](flow.md.html) directly to view the saved declaration and SVG,
or run `./launch.py --view-flow` for checked, managed browser inspection.
[Diagram maintenance](docs/workflow_viewer.md) explains markup freshness checks,
optional generation prerequisites and staged pre-commit checks.

*YubiKey® is a registered trademark of Yubico AB.*
