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

Use Python 3.13 or newer, then run from this repository directory:
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

## Guides

- [Settings and TUI controls](docs/workbench.md)
- [Credentials and LLM connections](docs/connections.md)
- [Resource packs and providers](docs/resource-packs.md)
- [Running, debugging and testing](docs/development.md)

**Developing with a coding agent?** Start with Python 3.13+ and an agent with
terminal access. For browser inspection, configure Chrome DevTools MCP (preferred)
or Playwright MCP. See [developer setup](docs/development.md#developer-environment-and-builds)
for prerequisites and connection instructions.

## Testing

Choose `unit`, `integration`, or `e2e` as `<tests_type>`:
```bash
./launch.py --tui-mode "<tests_type>"
```

All tests start selected. Add positional selectors to preselect tests, or adjust
selection in the TUI. Append `--no-std-in` for terminal automation, or export
`NO_STD_IN=True` for multiple commands. Integration requires `--start=True` or `--debug`;
E2E requires `--start=E2E` as human acceptance. These tests may spend tokens.
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
