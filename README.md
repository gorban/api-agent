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

Use Python 3.13 or newer. For source-checkout browser viewing, the included Studio
and unit tests, also install Node.js 24 or newer with npm. The launcher checks
the frontend dependencies and automatically runs the locked `npm ci` when they
need preparation, showing progress or guidance if Node/npm is unavailable.
The first install needs access to the packages or an npm cache; prepared assets
work offline afterward. You can also run `npm ci` from the repository root
yourself, including before opening workflow files directly in a browser.
Installed Python distributions include the viewer assets; graph execution alone
does not require npm.

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
home first, an enabled ready tracing dashboard next, the [actual LangGraph flow](flow.md.html), and the optional prepared UI last.
With both settings false, it prints the home and flow URLs without opening a
browser or authenticating an LLM. To use the same operation without the TUI:

```bash
./launch.py --tui-mode run-local --no-std-in
```

Press Ctrl+C to stop the terminal operation; use Stop or Quit in the TUI.
Shutdown waits for owned requests and resources to finish cleanup.
[Tracing configuration](docs/tracing.md) enables a ready Jaeger or LangFuse dashboard;
LangFuse dashboard login uses its separately configured admin account.
UI-process logs show safe activity messages rather than
arbitrary child output, which may contain credentials even with `LOG_CONTENT=True`.

## Guides

- [Settings and TUI controls](docs/workbench.md)
- [Credentials and LLM connections](docs/connections.md)
- [Included Bedrock provider and conditional AWS setup](src/api_agent/llm_providers/bedrock/README.md)
- [Resource packs, providers and named tools](docs/resource-packs.md#workflow-selected-named-tools)
- [Provider-owned integration test profiles](docs/integration_test_profiles.md)
- [Running, debugging and testing](docs/development.md)
- [Workflow diagrams and browser inspection](docs/workflow_viewer.md)
- [Bundled API demonstration](docs/api_demo.md)

**Developing with a coding agent?** Start with Python 3.13+ and an agent with
terminal access. For browser inspection, configure Chrome DevTools MCP (preferred)
or Playwright MCP. See [developer setup](docs/development.md#developer-environment-and-builds)
for prerequisites and connection instructions.

## Known issues

- **Managed local LangFuse has known development credentials:**
  `admin@local.dev` / `password`. Anyone who can reach that dashboard can try
  these defaults; keep it local and override them for other use. Existing
  accounts are not reset by changing initialization settings. The planned
  [safe multi-stage runtime image](docs/prds/implementation_plan_k2a.md#deployment-dashboard-credential-defaults)
  will clear both admin defaults to empty and require explicit configuration
  when LangFuse is selected; that image work remains pending.
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
and branch coverage. Run integrations under the documented qualified settings
needed to cover every applicable test. Providers can require different settings
for different tests; coverage does not require every possible setting combination.
For GPT, the valid discovery configurations are:

- `GPT_API_MODE`
  - `responses`
  - `completions`
- `GPT_AUTH_MODE`
  - `api_key`
  - `subscription`
    - Subscription not supported with `GPT_API_MODE=completions`.

When one configuration qualifies the whole suite, run it once with those settings:
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
API-specific request tests also need coverage under each applicable mode. Repeat
affected tests after changing effective provider settings. A passing configured
run does not qualify other settings. The included Bedrock provider supplies
[operation-scoped test profiles](docs/integration_test_profiles.md): ordinary
Responses calls and structured Completions workflows use separate defaults.
The application retains its own settings; configure the provider and supported
authentication once rather than switching API/model settings between tests.

## Markdown workflow PoC

The [workflow guide](docs/workflow_poc.md) covers the restricted Markdown grammar,
inference-free validation and the permanent mandatory node-coverage fixture.
Normal coverage filters fresh UUIDs before inference to exercise all ten reachable
route/readiness combinations, with real responses and retries under one test deadline.
It needs no calibration baseline; [older statistical maintenance](docs/workflow_calibration.md)
remains separate. Unsupported provider/model/settings fail without substitution.
The selected [API demonstration](docs/api_demo.md) uses its own declared flow;
the root flow remains the small one-call chat example.

Open [flow.md.html](flow.md.html) directly to view the saved declaration and SVG,
or run `./launch.py --view-flow` for checked, managed browser inspection.
[Diagram maintenance](docs/workflow_viewer.md) explains markup freshness checks,
optional generation prerequisites and staged pre-commit checks.

*YubiKey® is a registered trademark of Yubico AB.*
