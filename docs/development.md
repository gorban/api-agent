# Running, debugging and testing

## Running locally

Choose **Run local** to launch the agent. `RUN_UI=True` also launches the selected
UI provider as a host process. Packs/providers declare any managed upstream API
and database prerequisites; an explicit blank API_ENDPOINT requests that local
API, while a remote URL starts no local API stack. AGENT_DJANGO_URL addresses the
agent itself. The current launcher does not yet execute local-run; the service/log controls
below describe its planned contract. See [the infrastructure plan](prds/implementation_plan_h.md).

Use **b** for browser destinations and **l** for available log sources; **\***
selects interleaved, labeled logs. `RUN_LOG_SOURCE` selects the initial source
by name. **Stop running containers** lists only eligible owned services and
requires confirmation; it excludes unrelated containers and the hosting session.

TRACE_MODE selects LangFuse or OTEL; TRACE_TARGET chooses its destination.
Default local targets acquire the corresponding managed profile. Local-run
resources stop on exit; retained test services remain available for observation
and explicit cleanup. `LOG_CONTENT=False` keeps user/LLM/tool content out of
logs and traces; credentials stay excluded regardless of that setting.

## Recorded content and token usage

`LOG_CONTENT` defaults to False. Set it True to attach exact provider-neutral
LLM request/response content to Allure, including paths and code internals;
credentials remain redacted. Failure diagnostics and token accounting remain
available with content disabled. Future tool/browser evidence uses the same gate.

Set `TOKEN_USAGE_CSV_ENABLED=True` to append usage to `token_usage.csv` in the
selected pack root, or the base root without a pack. `TOKEN_USAGE_CSV` overrides
that path; an explicit empty value disables it. Its columns are provider, selected LLM,
call count, session ID, input, output, total, cache-read, cache-creation and
reasoning tokens. Allure retains its own usage summary and CSV independently.
The default CSV, its lock and legacy backups are Git-ignored.

## Testing

Select `unit`, `integration` or `e2e` through `--tui-mode`. File/node selectors
can be repository-qualified; `tag:NAME` selects a marked group. Multiple
selectors select their union. Unknown, ambiguous or empty matches fail.
```bash
./launch.py --tui-mode "<tests_type>" "<selector>"
./launch.py --tui-mode "<tests_type>" --collect-only
```

The interactive picker starts with All tests, then tag/file groups and individual
tests. Positional selectors preselect matches without hiding the full picker.
**s** starts; **b** returns to refreshed selection after completion. New tests
under a selected group join the run after recollection. Enter opens failed or
skipped result details. Failures sort first, then skipped tests, retaining order.

Shift+arrows, Shift+Home/End and Shift+Page Up/Down extend keyboard ranges.
Enter/Space selects the range if any member is unchecked, otherwise clears it.
Copy uses selected text, a test-name range or the focused test name; Ctrl+C on
non-Mac systems, Cmd+C on Mac. Confirmed stop cancels and drains the runner.

Unit discovery combines base units and any pack units. Integration combines
shared, pack and selected-provider suites. E2E uses the selected pack's suite.
Missing declared integration/E2E directories are errors, not successful empty
runs. Collection starts no services, authentication or test-result publication.

`TEST_PARALLEL_JOBS` defaults to 8. Units use `UNIT_TEST_PARALLEL_JOBS`,
defaulting to 32; set it empty to inherit the global count, or 0 to run units
serially. Both the TUI and terminal execution honor this override.

Only one suite can execute at a time per local user, across repositories and
resource packs. Idle launchers and history browsing remain available. The suite
lock covers setup, quota capture, recording and cleanup; the OS releases it if
the owner process dies. A competing Start fails before execution.

## Terminal automation and acceptance

`--no-std-in` (or `NO_STD_IN=True`) uses console output instead of the interactive
TUI. Supply the mode and applicable required settings. Missing prerequisites skip
dependent integrations with a reason; malformed values and execution failures
fail. An explicitly detected human-only login challenge can reopen Chromium
visibly once, then resume supported steps after intervention. This also applies
with `RUN_UI=False` and `NO_STD_IN=True`; a graphical session is required for the
visible browser. Credential lookup errors stop the selected unattended login.
Password-manager recovery retains its dedicated
[terminal interaction](architecture/password_managers.md#interactive-recovery).
Authentication and recovery remain bounded by their enclosing deadlines.

For terminal integration execution, combine `--no-std-in` with `--start`
(or `--start=E2E`). `--debug` additionally retains login browsers for inspection.
E2E requires its acceptance screen unless explicitly accepted with `--start=E2E`,
which also permits terminal execution. Normal startup configuration checks and
confirmed-newer-worktree approval still apply. The [automation task](prds/implementation_plan_a.md#terminal-integration-automation)
tracks remaining terminal result presentation.

## Results and configuration coverage

All units must pass. All tests selection additionally requires exactly 100%
lines and branches in every measured production file, including unimported code.
Partial selections show coverage informationally. Units use no live LLM calls
and are excluded from Allure. Integration has no reruns; E2E has its configured
consecutive-pass confirmation protocol after an initial failure.

Integration/E2E results go to configured Allure destinations. Optional local
results/secondary directories can be disabled with explicit blank values.
Publication failure is an operation failure even if individual tests passed.
A run with skipped tests has not verified those scenarios.

### Configuration coverage

Run the full suite once with a valid effective configuration. Repeat only the
configuration-dependent setup/conformance tests for other valid combinations;
GPT needs two additional setup runs. Track the initial auth/API values so none
is repeated or omitted. A focused setup pass does not cover mode-specific
request/stream behavior: run the corresponding transport tests under every applicable mode.

Provider-specific tests override their inputs after launch resolution without
editing .env. Negative tests assert rejection using isolated invalid inputs,
regardless of the valid launch configuration. CI scenarios should have named
report identities and independently owned sessions. Consult the
[coverage procedure](../README.md#for-full-coverage), and never treat a skip as
an implemented-mode pass.

## Browser debugging

To inspect provider login, use DEBUG with selected integration tests:
```bash
./launch.py --tui-mode integration --no-std-in --debug \
  api-agent::tests/integration/test_llm_discovery.py
```

A created browser is retained after success/failure; a cached session may need
no browser. Current debug teardown waits for retained windows to close.
Closing a login browser before callback ends authentication promptly.
E2E_DEBUG selects retained headed E2E browsers for focused tests.

For the included Studio request test, keep DEBUG inspection in the pytest
controller with a serial run:
```bash
./launch.py --debug --tui-mode integration --test-parallel-jobs=0 \
  api-agent::tests/integration/test_studio_request.py
```
This test makes one inference request using the configured provider. Parallel
worker-owned UI inspection is pending: its browser currently closes when the
worker exits. Shared provider-login inspection is independent of that limitation.

Prefer Chrome DevTools MCP for browser inspection; Playwright MCP is an
alternative. Configure the agent's MCP server to attach to the printed loopback
CDP endpoint, replacing `<port>` below with its port:
```bash
npx -y chrome-devtools-mcp@latest --browser-url=http://127.0.0.1:<port>
npx -y @playwright/mcp@latest --cdp-endpoint=http://127.0.0.1:<port>
```
Choose one server and select the existing context/page. The MCP extension
connection page is a separate browser. Attaching does not change the browser's
headless/headed mode or retention policy; launch.py owns cleanup. Keep callback
parameters, credentials, cookies and storage out of diagnostic output. See the
[Chrome DevTools MCP options](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/configuration.md),
[Playwright MCP options](https://github.com/microsoft/playwright-mcp#configuration)
and [inspection contracts](architecture/provider_architecture.md).

## Developer environment and builds

Use Python 3.13+ and a coding agent with terminal access. The launcher prepares
its local environment and Python dependencies; `uv` is optional for launcher
setup. Browser inspection additionally needs:

- Node.js LTS and npm when using the `npx` MCP commands above.
- Chrome DevTools MCP (preferred) or Playwright MCP configured in the agent.
- A compatible browser: attaching to an existing managed browser needs no
  separate Chrome installation. The launcher prepares Playwright Chromium when
  browser work first needs it; required host libraries must also be available.
  Compatibility with the chosen MCP/browser versions still needs verification.

If the MCP launches its own browser for independent UI development, configure
its `--executable-path` when its default Chrome executable is unavailable.
Use the CDP attachment options above when inspecting a launch.py browser.
Docker and other provider prerequisites are needed only for operations that
declare them; ordinary unit tests need no live provider credentials or inference.

Activate `.venv` in each shell. Development installation is separate from the
managed user launch path; this example uses `uv`:
```bash
uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/python -m build --wheel
```

Use the launcher for the applicable combined unit/coverage gate before code
commits. Narrow pytest runs help iteration but do not replace that full gate.
Environments and build artifacts are ignored. Another worktree can share local
certificates through intentional symlinks. A .env symlink permits shared reads,
but configuration editors refuse to write through it; use an independent copy
when editing settings through the TUI.

### Included frontend checks

The Studio host uses Node's standard library. For browser development checks,
install the pinned development tools and their Chromium once:
```bash
npm install
npx playwright install chromium
npm run test:frontend
npm run test:frontend:coverage
.venv/bin/python -m pytest tests/frontend/test_studio_connection.py
```
The first two test commands exercise controlled responses without inference.
The Python connection checks exercise real local HTTP listeners and disposal
with a controlled provider. The coverage command requires exact authored
line/branch coverage; reports stay under `artifacts/frontend-coverage/`.
`test_studio_request.py` is a separate integration test: it sends one small
prompt through the actual agent endpoints using the configured LLM connection.

Large unit selections are transferred to pytest through an operation-owned
temporary selection file, so parameterized test IDs do not exhaust OS command
argument limits. Positional selection and per-owner execution remain unchanged.
