# api-agent

The shared Python runtime is being rebuilt. Configuration, credential setup, pytest selection, unit execution and fixed coverage checks use an interactive Textual workbench. Integration Start runs selected tests with Allure recording. The bundled GPT provider supports lazy subscription setup; inference, application services and E2E execution remain separate work.

## Prerequisites

Use Python 3.13 or newer, Git on PATH (required by the launcher's provenance gate), and `uv` for the development setup below. The workbench uses Textual. `./launch.py` prepares the repository-local `.venv` and declared external tools automatically when an interactive operation or collection needs them; it loads source directly without an editable installation. `--help` needs only Python and Git. Development commands below install the full test/build tools explicitly.

## Development setup

Run these commands from `api-agent`:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
./launch.py --help
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m build
```

For development tools, activate `.venv` in each new shell; its `#!/usr/bin/env python3` shebang selects Python from the active environment. Both launch scripts are executable in Git. If a file copy loses the executable permission, restore it with `chmod +x launch.py`. Without activation, use `.venv/bin/python launch.py --help` explicitly.

The source launchers load the selected shared checkout directly; neither needs an editable package installation to launch. With Python 3.13 or newer on PATH, `./launch.py --help` works without a virtual environment; interactive launch and collection prepare the local environment when needed. The development install above supplies pytest and build tools. Build artifacts and the environment are ignored. Unit tests make no live LLM calls. Source launchers use their own checkout, independently of the current directory; installed console entry points require the known editable source layout, and direct wheel execution reports an actionable source-launcher error instead of guessing a checkout.

## Foundation review

```sh
.venv/bin/python launch.py --help
.venv/bin/python launch.py --no-std-in
.venv/bin/python launch.py --no_std_in=True --tui-mode unit
.venv/bin/python launch.py --no-std-in=True --tui-mode integration
```

The missing-mode example returns configuration error status `2`. Unit execution returns `0` when selected tests pass; All tests additionally requires every measured production coverage area to reach 100%. Partial selections report coverage informationally; coverage or test failures return nonzero. Integration/E2E execution rejects `NO_STD_IN=True`. Help returns `0` after the worktree check and any required stale-checkout approval. Git/pack/schema/bootstrap/collection errors return `2`. Configured test collection returns `0` only when valid nonempty matches are listed, without opening the TUI or executing tests.

## Configuration and discovery workbench review

```sh
./launch.py
./launch.py --tui-mode=config-cleanup
./launch.py --tui-mode=unit --collect-only
./launch.py --tui-mode=unit --collect-only api-agent::tests/unit/test_selection.py
```

The launcher creates/updates local external tools on first use; subsequent launches reuse them. It does not install the shared source package. Every launch attempts to check worktree recency; missing Git or unavailable worktree metadata silently skips this check. If a requested checkout is older than another registered worktree, the warning requires literal `y` followed by Enter, even for help, collection and `NO_STD_IN`; Enter, refusal or EOF stops startup. Current/newest checkouts proceed without displaying approval or reading stdin. `q` opens a quit confirmation; enter literal `y` to confirm. `c` returns to the main menu where available. Buttons and tables show focus, and the bottom bar lists contextual shortcuts.

Startup configuration review automatically lists every unsupplied declared default across all modes, plus missing settings that can be required by any mode in the selected agent/resource-pack combination. New defaults join the list without maintaining a separate registry. Select a row, press **a** to save its default immediately, or **e** to edit its effective value and Enter to save; Source identifies the file where a value was saved. **u Unset** removes the selected saved setting from its source `.env`, even when another save target is selected, and restores its fallback. Required settings without defaults allow only editing, including an explicit empty string when its declared type permits it. Press **c** for **Next menu**, or Escape while editing to return without saving. Controlling changes revalidate conditional settings. Missing required settings are shown before mode selection, even when their runtime condition is not currently active.

The save target defaults to the directory of the launcher used, even when a resource pack is selected. Below the configuration table, a read-only list aligns the full `.env` target paths with a checkmark for the selected target and a blank for others; **s Switch target .env** changes the target when a pack is active. Resaving a setting moves its assignment to the selected file. Source shows the owning `.env` as a `../` relative path. Saving preserves unrelated assignments/comments and skips `NO_STD_IN`, `COLLECT_ONLY`, `API_AGENT_DIR`, `UI_SOURCE_DIR`, `UI_NX_APP`, `UNATTENDED_SECONDARY_USER`, and debug/index defaults according to metadata.

From the main menu, select unit tests to review discovery and selection: All tests comes first, then tag/file groups, then individual identities. Enter toggles the focused group/test and counts update across overlapping groups. Tables and focused scroll panes support arrow keys and `h/j/k/l` for left/down/up/right; text inputs retain ordinary typing. `s` starts selected unit tests and shows their status/duration, failure details, and line/branch coverage by production file. After completion, `b` returns to a freshly collected picker with your selection retained. Select a failed test to inspect its traceback; use arrow/Vim keys or Page Up/Down to scroll and `c` to return. Confirmed Quit cancels an active run with nonzero status. An explicitly configured interactive unit mode opens the full-suite picker with only startup selector matches preselected; All tests remains unchecked for a subset. Returning through the main menu discards those selectors and initially selects all tests. `NO_STD_IN=True` uses raw pytest output plus the final summary and forwards the result status. Unit mode fails if any test does not pass; All tests additionally requires every measured production file to reach 100% line/branch coverage, including unimported files. Partial selections report coverage without enforcing that threshold; all measured production files currently meet the exact coverage gate. Packs without production `src/` code have no pack-code percentage. Integration Start records selected tests in Allure; E2E execution remains unavailable.

Cleanup lists a narrow unnamed deletion indicator, Setting, Effective value, and Source. The indicator shows a checkmark for deletable `.env` rows. Settings hidden from startup review appear here only while saved in a relevant `.env`. Select a row whose source is an agent/resource-pack `.env`; **d Delete .env assignment** asks for confirmation to delete that setting from all relevant `.env` files, preserving unrelated content. Delete one setting at a time. Cleanup has no save-target controls. Closing with **c** returns to configuration review if deleted settings need acknowledgement again. `UNATTENDED_AUTH` remains excluded because its separate setup mode owns it.

Selectors are a de-duplicated union of `tests/unit/file.py`, full pytest node IDs, or `tag:NAME`. Prefix file/node/tag selectors with `api-agent::` or the selected owner repository name to disambiguate combined units. Unknown/ambiguous selectors and empty matches are errors. Collection uses each repository's cwd/config and does not run tests or prerequisites. Missing optional pack unit suites are skipped; missing/empty base units are errors. Integration/E2E suites not yet created report missing-suite errors.

The worktree gate asks only when a newer checkout is confirmed. Application services, actual inference and E2E execution remain unfinished tasks.

## Executable declarations

- [Shared configuration](cfg/config.toml) declares a version and each field's type, defaults, conditions, path anchor, help, and presentation metadata.
- [Demo pack](demo/pack.toml) selects the [demo extensions](demo/cfg/config.toml), test owner, suite paths, and default overrides. The included demo is also bundled in the wheel.
- Resource packs may provide their own root `pack.toml` and `cfg/config.toml`, app defaults, and authentication declarations. Authentication is lazy: a selected integration requests provider setup through the common interface.

Selected providers may declare integration directories in adjacent `cfg/tests.toml` using `version = 1` and `[tests] integration = ["tests/integration"]`. Discovery combines shared, pack, and selected-provider suites without loading provider factories. Declared directories and their symlinked contents must stay inside the provider root. The same physical test file and function/parameter identity is collected once, retaining its first shared, pack, or provider identity. Put selection-dependent provider suites outside shared and pack suite directories, since those directories are collected regardless of the selected provider.

Values resolve as shared defaults, then pack defaults, then api-agent's root `.env`, the selected resource pack's root `.env`, environment variables, and CLI options. Pack `.env` values override only the keys they provide; missing keys retain the base `.env` values. The bundled demo uses the base repository's `.env`. External pack overrides are independent of where its tests are owned. Resolution retains whether a value is explicit and its source. An extension can refer to shared controls in its conditions; inactive requirements do not block an operation.

Types currently include `bool`, `int`, `str`, `path`, and compact JSON objects. CLI/environment booleans are exactly `True` or `False`; bare boolean flags mean `True`. `--field-name` and `--field_name` are equivalent, as are `--field=value` and `--field value`. `-h` sets `HELP=True`. With space-separated values, a value beginning with `--` starts another option: a preceding boolean becomes `True`, while a nonboolean reports its missing value. Use `--field=VALUE` to preserve such a literal value. `--` ends option parsing, and positional selectors require an explicitly configured test mode. The literal `.env` reader accepts `KEY=value`, quoted values, comments on their own lines, and optional `export`; it performs no shell expansion or command execution.

Directory values use declared `base_root`, `pack_root`, or `owner_root` anchors rather than the process working directory. Configure `UI_SOURCE_DIR` explicitly relative to the test-owner repository root when UI execution is required. A resource pack's `API_AGENT_DIR` defaults to `../api-agent` relative to its pack root and may be overridden through its `.env`, environment, or CLI.

The general LangGraph Markdown Layout and prompt interpreter are later work. No empty workflow, provider, knowledge, or navigation files have been created to imply those features already exist.

## Worktree provenance

Before help, collection, or any future TUI operation, the gate requires Git and checks the selected base, resource-pack source, and test-owner repositories, deduplicating nested roots. It compares registered checkouts independently by the newer of HEAD's committer timestamp and the newest nonignored unstaged/untracked file's modification time. Staged-only changes do not count; missing/deleted files have no invented modification time. Ties are accepted. It preserves host Git configuration while removing inherited variables that could redirect repository discovery or the index.

If a selected checkout is stale, the gate lists all relevant roots, branches/SHAs, dates, reasons, selected checkouts, and newest checkouts, then requires explicit `y` on stdin. This intentionally applies even with `NO_STD_IN=True`, help, or collect-only. Decline/EOF/unavailable stdin stops with a nonzero status; there is no bypass option. Inaccessible or unrelated registered checkouts also fail verification. The gate never creates, switches, repairs, or deletes worktrees.

Select an explicit resource pack with `--resource-pack-dir PATH` or `RESOURCE_PACK_DIR=PATH` (CLI takes precedence). Relative explicit pack paths resolve from the calling directory. Resource-pack launchers default to their own pack; api-agent defaults to its demo and shows a bootstrap hint only when no pack was explicitly selected.

Human-edited declarations and future workflow Layout/prompt files live under `cfg/`, organized by responsibility and node. Python parsers remain under `src/api_agent/config/`. Each resource pack has its own `cfg/` tree, selected by its root `pack.toml`.

Tracing configuration uses optional `TRACE_MODE` (empty/omitted, `LangFuse`, or `OTEL`) and `TRACE_TARGET`. The target defaults conditionally to `http://localhost:3000` for LangFuse or `http://localhost:4318/v1/traces` for OTEL. Changes to the mode update a defaulted target; explicit targets keep their value. `LOG_CONTENT` defaults to `False`. Exporter wiring, Compose tracer profiles, retention/reuse, and content scrubbing are scheduled before the first Ok run; declaring these fields does not enable an exporter yet.

The main menu shows the live host running-container count. **Stop running containers** (`--tui-mode=cleanup-containers`) lists containers, lets Enter select them, and requires literal `y` before stopping selected containers. Inventory uses the Docker-compatible host CLI (including the local remote-Podman wrapper), with Podman as fallback when Docker is absent. Inventory failures are shown explicitly. Discovery lists running containers from the host engine, excluding the container hosting this session using `/run/.containerenv` and Distrobox identity variables. Stop requests also reject that container by name or full/abbreviated ID. The menu count includes only listed, stoppable containers. This mode stops containers without removing containers or volumes.

Credential retrieval helpers accept configurable executable paths from each `UNATTENDED_AUTH` entry. Choose the Keeper Commander executable or use `op` on PATH for 1Password; version checks were verified with Keeper Commander 18.1.6 and 1Password CLI 2.40.0. Helpers close stdin, use bounded supervision and never print successful credentials. Stderr login prompts fail promptly; stdout-only prompts fail on a nonzero command result or timeout, because successful raw stdout must not be interpreted as a login message. The credential setup wizard is available; subscription browser consent begins lazily through launch-managed integration setup. Automated login-page assistance remains pending.

## Lazy subscription setup

Run `./launch.py --tui-mode integration api-agent::tests/integration/test_llm_setup.py`, then **s Start attended tests**. The test requests the selected provider’s setup capability; the bundled GPT provider reuses a saved connection or opens managed Playwright Chromium for initial consent. Attended sign-in always shows a window, independently of RUN_UI, so you can sign in and approve access. Chromium runs alongside the loopback callback listener and closes after setup, failure or cancellation. Its matching browser binary is prepared lazily if missing. Help, collection and selection never sign in. No inference is performed by this test. Allure storage configuration is required for recorded integration execution. Automatic storage-prerequisite acquisition is still a follow-up task; unavailable storage makes recording fail rather than claiming successful history publication.

`GPT_APP_NAME` defaults to `api-agent` even with a resource pack. Override it through normal configuration to supply your application's display-name hint for first ChatGPT registration. Keep the name consistent across installations. Changing it does not rename a saved registration; later sign-ins reuse the issued client ID without resending the hint.

For retained subscription-login debugging, use:

```bash
./launch.py --no-std-in --tui-mode integration --debug \
  api-agent::tests/integration/test_llm_setup.py::test_llm_setup
```

This explicit integration debug exception starts selected tests without terminal input, but sign-in/consent can still require human interaction in Chromium. It runs serially even when TEST_PARALLEL_JOBS is higher, sharing lazy setup within each worker session. If a login browser is created, its loopback inspection endpoint is printed immediately. The original browser stays open after setup success or failure, including callback timeout; close its windows to let pytest teardown and Allure publication finish. Ctrl+C stops the run. The interactive integration TUI also honors DEBUG, but its final summary waits for retained windows to close. Cached setup may complete without a window. Ordinary runs close login browsers normally. E2E retains its explicit human acceptance; this integration exception does not enable noninteractive E2E.

Inspection clients can attach to the printed CDP endpoint and select the existing context/page; attachment must not create a replacement login context. This development endpoint exposes the browser session to local clients and disappears when the owning browser closes. Cross-worker shared authentication, final results before inspection ends, E2E/local-run attachment actions and inspection without debug are planned follow-ups.

With Playwright MCP, `browser_run_code_unsafe` can attach through the existing MCP browser's browser type:

```javascript
async (page) => {
  const browser = await page.context().browser().browserType().connectOverCDP(endpoint);
  const originalPages = browser.contexts().flatMap(context => context.pages());
  // Select the operation's existing page; do not create a new context/page.
  return originalPages.map(tab => new URL(tab.url()).origin);
}
```

Replace `endpoint` with the launcher's printed loopback URL. The MCP extension connection/permission page is separate from the application login window. Keep URLs containing OAuth parameters, cookies, credentials and browser storage out of diagnostics; report only the minimum page identity needed to select it.


An explicit `UNATTENDED_USER_INDEX` enables **u Start unattended tests**. Unattended GPT may reuse a saved subscription connection; missing initial consent reports an actionable error without opening a browser or waiting for input. **s** remains attended for that run and does not rewrite the saved index. There is no manual GPT profile setting. Unattended GPT requires the selected LLM User account descriptor and a configured shared password manager/CLI; its retrieved username selects the matching cached subscription. Plain startup offers Setup / Skip whenever an enabled role or manager is incomplete. Skip keeps attended login available; an explicitly selected TUI_MODE still bypasses the startup suggestion. Attended OAuth identifies the signed-in account when the cache is empty or ambiguous. GPT_STATE_DIR remains a hidden advanced storage override. See [the lazy setup checklist](docs/implementation_plan_f2b.md) for supported behavior and follow-ups.
