# Running, debugging and testing

## Running locally

Choose **Run local** to launch the agent. `RUN_UI=True` also launches the selected
UI provider as a host process. Packs/providers declare any managed upstream API
and database prerequisites; an explicit blank API_ENDPOINT requests that local
API, while a remote URL starts no local API stack. AGENT_DJANGO_URL addresses the
agent itself. Only enabled declared prerequisites are acquired; concrete upstream
APIs and databases remain the selected pack's responsibility.

Use `./launch.sh --tui-mode run-local --no-std-in` for the terminal consumer.
`RUN_UI=True` or `DEBUG=True` opens one headed browser with agent home first,
the actual selected graph next and optional prepared UI last. With both false,
home and flow URLs remain available without a browser, credentials or catalog
access. Required later LLM authentication is always headed in Run local.

Use **b** for browser destinations and **l** for available log sources; **\***
selects interleaved, labeled logs. In either menu, use arrow keys or **h/j/k/l**
to select a button, then **Enter** to activate it. **1–9**, then **0**, activate
the first ten choices directly. **Ctrl+C** copies the selected destination URL
or log-source name. `RUN_LOG_SOURCE` selects the initial source
by name. **Stop running containers** lists only eligible owned services and
requires confirmation; it excludes unrelated containers and the hosting session.

Model discovery caches validated successful metadata for
`LLM_CATALOG_CACHE_TTL_SECONDS` (default 3600 seconds). Zero disables completed
cache reuse. Failed discovery can be requested again; authentication/setup
failures still require a new operation. This does not cache prompts or replies.

[Tracing configuration](tracing.md) optionally enables local or remote tracing
and ready dashboard tabs. Local-run resources stop on exit after active requests
and cleanup duties acknowledge completion; failed cleanup retains a retry receipt.
Retained test services remain available for observation and explicit cleanup.
UI-process output produces content-free activity messages, even when content
logging is enabled; arbitrary streaming output has a separate privacy gate.
`LOG_CONTENT=False` keeps user/LLM/tool content out of logs and Allure.
`TRACELOOP_TRACE_CONTENT=False` independently keeps payload content out of both
OTEL and LangFuse spans. Credentials stay excluded with either opt-in enabled.
Run local emits shared workflow and safe provider lifecycle events.
`RUN_LOG_FULL_CONTENT=True` removes the default 512-character preview limit from
permitted, supported payload fields; it does not enable content capture or remove
source admission bounds. Use `./launch.sh --tui-mode run-local --no-std-in | tee run.log`
to retain stdout; the TUI keeps its own smaller display buffer. Slow terminal
consumers can cause dropped diagnostic events, explicitly marked when output
resumes. Stopping the application does not wait indefinitely for a blocked pipe.

## Recorded content and token usage

`LOG_CONTENT` defaults to False. Set `LOG_CONTENT=True` to attach scrubbed
provider request/response content to Allure, including provider-returned plaintext
reasoning and summaries when available. They stay separate from the final answer;
credentials, encrypted reasoning and internal state remain excluded.
`TRACELOOP_TRACE_CONTENT=True` independently permits trace content.
Failure diagnostics and token accounting remain
available with content disabled. Future tool/browser evidence uses the same gate.

Allure's **LLM request/response diagnostics** attachments retain bounded SDK
observations even without DEBUG or content opt-in: effective effort/output limit,
output item counts, visible/reasoning text lengths, completion/stop categories,
and available HTTP status, request/response IDs and SDK versions. They correlate
with the existing call and physical-attempt IDs. These are SDK-decoded observations,
not raw HTTP captures. Missing fields stay unknown. A reasoning-token count does
not establish that reasoning text was returned. When capture is disabled, its
notice names the flag to enable for a new run. Capture loss is separate: content
flags cannot recover unavailable content. Existing bounds/truncation still apply.
Response diagnostics always include `reasoning_text_characters`: `null` when
unknown, `0` when observed empty, or the measured count. Request diagnostics omit
response-only fields. Actual plaintext stays in the permitted response attachment;
for Responses, inspect `provider_response.output` items of type `reasoning`.
`reasoning_items` counts those items; it is not the reasoning text itself.

An HTTP-success response can still fail provider-contract or credential-safety
validation. Bedrock retains a fixed rejection code, admitted SDK diagnostics and
reported usage. Known validation checks also identify a fixed rule and schema
path without including provider-supplied field names or values in diagnostics.
After contract rejection, bounded SDK response evidence can still be captured
under the independent destination flags above, provided its separate credential
safety checks pass. This is failure evidence, not a validated completion.
Both worker admission and final report/trace projection protect against split
credential fragments and serialized JSON carriers. Final projection rechecks
the frozen credential snapshot even when detailed diagnostics were scrubbed.
Ambiguous or over-limit evidence is withheld as a whole, not partially released.
Safety-rejected bodies stay withheld regardless of flags. Allure marks a body
that could not be retained as unavailable; this does not mean no upstream response
arrived. These failures do not gain automatic retries or relaxed validation.

Capture can retain only what its adapter supplies. Normalized-only streaming
adapters may omit original reasoning. Shared-provider integration calls now have
an independent, backend-owned trace lane: `LOG_CONTENT=False` does not veto
explicit trace permission, and trace-only payloads never enter test event streams
or Allure artifacts. Call and transport-attempt IDs correlate remote spans with
accounting and Allure; this is not distributed workflow-parent propagation.
All destinations retain credential scrubbing and capture bounds. Enabling a flag
does not recover earlier content or reasoning discarded by an adapter.

Set `TOKEN_USAGE_CSV_ENABLED=True` to append usage to `token_usage.csv` in the
selected pack root, or the base root without a pack. `TOKEN_USAGE_CSV` overrides
that path; an explicit empty value disables it. Its columns are provider, selected LLM,
call count, session ID, input, output, total, cache-read, cache-creation and
reasoning tokens. Allure retains its own usage summary and CSV independently.
The default CSV, its lock and legacy backups are Git-ignored.

## Failure diagnostics

Integration failures use short pytest tracebacks by default: exception causes and
source locations remain visible without dumping each transport frame's source.
Use `DEBUG=True` for expanded source tracebacks and existing inspection behavior.
The project explicitly uses function-scoped async fixture loops; other pytest
warnings remain enabled.

Terminal integration output pauses during private authentication conversations,
including password-manager unlock and provider URL/code handoffs. Deferred output
is replayed after the channel closes; large queues spill to temporary storage
without truncating diagnostics. Setup and provider-operation timeouts retain a
safe `deadline` classification without requiring DEBUG. A deadline alone is not
evidence of a network outage. See [authentication limits](authentication-limitations.md).

Physical inference recovery defaults to five total attempts, with nominal waits
of 0.2s, 1s, 5s and 25s plus bounded jitter. It covers HTTP 429/5xx and explicitly
classified transient network failures, within the shared deadline. A failed
connection may follow a billable request; attempt evidence retains unknown costs.
Empty successful answers, local validation and exhausted deadlines do not trigger
replacement inference. See [completion recovery](architecture/provider_contracts_v1.md#bounded-completion-recovery).

## Optional Allure viewer prerequisite

The launcher prepares the pinned Allure 2 CLI for HTML report generation and
viewing through mise; recording and publishing results do not require it. A
custom executable can instead be supplied through `ALLURE_CLI`; its own Java
and availability requirements still apply.

`ALLURE_CLI` defaults to `allure` and is visible in Review configuration. It accepts a quoted executable and optional prefix arguments, without shell expansion. That executable must be available on PATH in the launcher's environment;
a host installation alone may not be visible inside a container. The
[managed viewer](prds/implementation_plan_d1.md) checks this prerequisite.

## Viewing Allure results

Choose **Allure serve** or run `./launch.sh --tui-mode allure` interactively to
browse finalized integration/E2E runs for the selected repository/pack. The
managed browser opens the latest report with native test History and trend
charts, built chronologically from up to 20 launches of the same mode.
**Browse previous runs** in the launcher or report executor opens the complete
newest-first archive; older reports are generated on demand. Independent
launches remain distinct from retries. **Refresh history** discovers later
publications without changing already served report bindings. The
viewer uses `ALLURE_VIEWER_PORT` (default 5252); an occupied port fails clearly.
Leaving the viewer closes its browser, listener and temporary reports; stored
results remain intact.

In live or completed test results, highlight one completed test and press
**a View in Allure** for that exact attempt. Pending/running tests, groups, units
and multi-row selections have no shortcut. A live preview labels pending
suite-wide accounting; final publication gets a separate immutable binding.
The viewer remains until leaving results, Back to selection, End inspection or
quit. Viewer failures do not change test/publication outcomes. Normal prompts,
responses and trusted Studio icons are visible with `LOG_CONTENT=True`;
authentication fields and registered credentials remain protected.

## Testing

Select `unit`, `integration` or `e2e` through `--tui-mode`. File/node selectors
can be repository-qualified; `tag:NAME` selects a marked group. Multiple
selectors select their union. Unknown, ambiguous or empty matches fail.
```bash
./launch.sh --tui-mode "<tests_type>" "<selector>"
./launch.sh --tui-mode "<tests_type>" --collect-only
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
When `DEBUG=True` and more than one distinct integration or E2E test is selected,
confirm with literal `y` at the browser-window warning, including under `--no-std-in`.
Set `DEBUG=False` and omit `--debug` to avoid the prompt; `--start` does not bypass it.
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

Unit runs use private per-run scratch in the user cache for coverage, workers and
nested temporary files. An explicit `TMPDIR` chooses the scratch base. Scratch is
removed after confirmed process disposal; if disposal cannot be confirmed, the
run directory is retained and identified in the error note. An authenticated local
control socket uses a short private system-temporary directory if its path under
the scratch base, including an explicit `TMPDIR`, would exceed the portable Unix
socket limit. Managed Chromium similarly uses a private short process-temporary
directory when needed for its singleton socket; its Playwright profile and bulk
scratch stay under the selected base.

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

For declaration-only inspection, `./launch.sh --view-flow` opens the selected
workflow in its own managed headed browser without provider setup or graph
execution. Add one double-quoted flow path, for example
`./launch.sh --view-flow "demo/contrived/flow.md.html"`, to inspect a different
graph relative to the current directory. See [workflow diagrams](workflow_viewer.md) for static file opening,
markup freshness, generator prerequisites and staged checks.

To inspect provider login, use DEBUG with selected integration tests:
```bash
./launch.sh --tui-mode integration --no-std-in --debug \
  api-agent::tests/integration/test_llm_discovery.py
```

A created browser is retained after success/failure; a cached session may need
no browser. Current debug teardown waits for retained windows to close.
Closing a login browser before callback ends authentication promptly.
E2E_DEBUG selects retained headed E2E browsers for focused tests.

For the included Studio request test, keep DEBUG inspection in the pytest
controller with a serial run:
```bash
./launch.sh --debug --tui-mode integration --test-parallel-jobs=0 \
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
headless/headed mode or retention policy; launch.sh owns cleanup. Keep callback
parameters, credentials, cookies and storage out of diagnostic output. See the
[Chrome DevTools MCP options](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/configuration.md),
[Playwright MCP options](https://github.com/microsoft/playwright-mcp#configuration)
and [inspection contracts](architecture/provider_architecture.md).

## Developer environment and builds

Install mise and use a coding agent with terminal access. The launcher uses mise
to prepare its pinned Python and uv tools and its Python dependencies; Node is
prepared when needed.
Browser inspection additionally needs:

- Node.js and npm available to the agent when using the `npx` MCP commands
  above; use the managed tools or configure a suitable custom installation.
- Chrome DevTools MCP (preferred) or Playwright MCP configured in the agent.
- A compatible browser: attaching to an existing managed browser needs no
  separate Chrome installation. The launcher prepares Playwright Chromium when
  browser work first needs it; required host libraries must also be available.
  Compatibility with the chosen MCP/browser versions still needs verification.

If the MCP launches its own browser for independent UI development, configure
its `--executable-path` when its default Chrome executable is unavailable.
Use the CDP attachment options above when inspecting a launch.sh browser.
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
On Linux, two unit proofs exercise headed Chromium on a test-owned display;
they require Xvfb on `PATH` but no desktop. A missing Xvfb fails those tests
with a prerequisite message. Display isolation on other platforms remains
unverified.
Environments and build artifacts are ignored. Another worktree can share local
certificates through intentional symlinks. A .env symlink permits shared reads,
but configuration editors refuse to write through it; use an independent copy
when editing settings through the TUI.

### Included frontend checks

The Studio host uses Node's standard library. With Node.js 24 or newer and npm
installed, launcher unit execution automatically prepares missing locked frontend
dependencies. For direct browser development checks, prepare the pinned tools
and their Chromium explicitly:
```bash
npm ci
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

### Selected UI-session checks

To review the selected UI provider without an LLM request:

```bash
./launch.sh --debug --tui-mode integration \
  api-agent::tests/integration/test_ui_provider_session.py
```

The included Studio provider is anonymous. Other selected providers may prepare
login using their declared roles and the selected unattended index. The test
launches the normal provider process and hands back its original managed session;
ordinary LLM discovery still opens no frontend. Integration listeners use isolated
loopback ports, preserving the configured scheme/host and saved settings.

Studio also contributes `test_studio_session.py::test_studio_theme`, which changes
Appearance without inference. DEBUG retains original browsers and their hosts
after reports complete, including parallel workers. Use the launcher's existing
end-inspection control to release them before starting another suite.

With `--log-content=True`, these prepared-session tests attach original viewport
PNGs through the usual Allure publication and secondary copy. A trusted provider
must supply credential masking; unsafe or unsupported capture reports unavailable
evidence. Content disabled performs no browser capture and attaches a fixed
notice image explaining how to enable screenshots; provider safety still applies. Authentication-provider
browsers retain their separate evidence/lifecycle tasks. The one-prompt Studio
request test now uses the shared prepared session and screenshot path. These checks do not implement Run local or application
E2E, and no inference is needed for this review.

A host command prefix can be configured as `ALLURE_CLI="distrobox-host-exec /absolute/host/path/to/allure"`. Check the same command with `--version` first. A noninteractive host PATH may differ from an interactive shell’s PATH, so a bare host command can exit 127 despite working in a terminal. An explicit host executable path avoids depending on shell startup files. Quotes group arguments containing spaces; shell variables, pipelines and substitutions are not expanded.

Completed integration/E2E tests open timing, token and cost details with Enter, including passed tests without LLM calls. Within details, **c Close** restores the results row/scroll without ending inspection; **a View in Allure** opens that same attempt.

Viewer notifications and history pages keep wrapped report/archive URLs as whole links. Click to open the report and copy its complete URL; rendering and hovering do not copy. The separately labelled **Browser CDP** address is for automation: click it to copy the connection base. Its root may return an empty page; `/json/version` returns debugger discovery metadata. Use the report URL for viewing results and the CDP base for attaching inspection tools.
