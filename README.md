# api-agent

The shared Python runtime is being rebuilt. Configuration/default acknowledgement, `.env` cleanup, mode selection, pytest collection, and grouped test selection now have an interactive Textual workbench. Actual test execution, authentication setup/login, providers, application services, and Allure are still being implemented. Their entries explicitly report unavailable operations; no tests or services are started by those entries.

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

The missing-mode example returns configuration error status `2`. Explicit unit execution currently returns status `3` because the runner is not implemented. Integration/E2E execution rejects `NO_STD_IN=True`. Help returns `0` after the existing stale-worktree gate. Git/pack/schema/bootstrap/collection errors return `2`. Configured test collection returns `0` only when valid nonempty matches are listed, without opening the TUI or executing tests.

## Configuration and discovery workbench review

```sh
./launch.py
./launch.py --tui-mode=config-cleanup
./launch.py --tui-mode=unit --collect-only
./launch.py --tui-mode=unit --collect-only api-agent::tests/unit/test_selection.py
```

The launcher creates/updates local external tools on first use; subsequent launches reuse them. It does not install the shared source package. `q` opens a quit confirmation; enter literal `y` to confirm. `c` returns to the main menu where available. Buttons and tables show focus, and the bottom bar lists contextual shortcuts.

Skip the suggested unattended setup for now: that wizard is explicitly unavailable. Configuration review lists defaulted and missing required settings together. Select a row, press **a** to accept its default or **e** to edit, then Enter to save to `.env`; saved rows show a checkmark. Required settings without defaults allow only editing, including an explicit empty string when its declared type permits it. Press **c** for **Next menu**, or Escape while editing to return without saving. Controlling changes revalidate conditional settings. Select a mode to expose its applicable settings. Saving updates only the active repository/pack `.env`; it preserves unrelated assignments/comments and skips debug/index defaults according to metadata.

From the main menu, select unit tests to review discovery and selection: All tests comes first, then tag/file groups, then individual identities. Enter toggles the focused group/test and counts update across overlapping groups. Continue currently shows the unavailable runner; it does not execute the selected tests. An explicitly configured unit mode bypasses the picker and also reports the pending runner.

Cleanup shows the active file path and effective values/sources. A check means the assignment exists in that file, a blank means absent, and Enter toggles a present row to/from `X`. `d` appears only while focused on a present row and deletes marked assignments, including duplicates, preserving unrelated content. Inherited base `.env` values are shown but can only be deleted from their owning repository's cleanup. `UNATTENDED_AUTH` is excluded because its separate setup mode owns it.

Selectors are a de-duplicated union of `tests/unit/file.py`, full pytest node IDs, or `tag:NAME`. Prefix file/node/tag selectors with `api-agent::` or the selected owner repository name to disambiguate combined units. Unknown/ambiguous selectors and empty matches are errors. Collection uses each repository's cwd/config and does not run tests or prerequisites. Missing optional pack unit suites are skipped; missing/empty base units are errors. Integration/E2E suites not yet created report missing-suite errors.

The current worktree gate still asks only when stale. The polished always-interactive worktree approval screen, credential wizard, running/results/failure screens, provider/usage/Allure wiring, and services are separate unfinished tasks.

## Executable declarations

- [Shared configuration](cfg/config.toml) declares a version and each field's type, defaults, conditions, path anchor, help, and presentation metadata.
- [Demo pack](demo/pack.toml) selects the [demo extensions](demo/cfg/config.toml), test owner, suite paths, and default overrides. The included demo is also bundled in the wheel.
- Resource packs may provide their own root `pack.toml` and `cfg/config.toml`, app defaults, and authentication declarations. No authentication runs in the foundation.

Values resolve as shared defaults, then pack defaults, then api-agent's root `.env`, the selected resource pack's root `.env`, environment variables, and CLI options. Pack `.env` values override only the keys they provide; missing keys retain the base `.env` values. The bundled demo uses the base repository's `.env`. External pack overrides are independent of where its tests are owned. Resolution retains whether a value is explicit and its source. An extension can refer to shared controls in its conditions; inactive requirements do not block an operation.

Types currently include `bool`, `int`, `str`, `path`, and compact JSON objects. CLI/environment booleans are exactly `True` or `False`; bare boolean flags mean `True`. `--field-name` and `--field_name` are equivalent, as are `--field=value` and `--field value`. `-h` sets `HELP=True`. With space-separated values, a value beginning with `--` starts another option: a preceding boolean becomes `True`, while a nonboolean reports its missing value. Use `--field=VALUE` to preserve such a literal value. `--` ends option parsing, and positional selectors require an explicitly configured test mode. The literal `.env` reader accepts `KEY=value`, quoted values, comments on their own lines, and optional `export`; it performs no shell expansion or command execution.

Directory values use declared `base_root`, `pack_root`, or `owner_root` anchors rather than the process working directory. Configure `UI_SOURCE_DIR` explicitly relative to the test-owner repository root when UI execution is required. A resource pack's `API_AGENT_DIR` defaults to `../api-agent` relative to its pack root and may be overridden through its `.env`, environment, or CLI.

The general LangGraph Markdown Layout and prompt interpreter are later work. No empty workflow, provider, knowledge, or navigation files have been created to imply those features already exist.

## Worktree provenance

Before help, collection, or any future TUI operation, the gate requires Git and checks the selected base, resource-pack source, and test-owner repositories, deduplicating nested roots. It compares registered checkouts independently by the newer of HEAD's committer timestamp and the newest nonignored unstaged/untracked file's modification time. Staged-only changes do not count; missing/deleted files have no invented modification time. Ties are accepted. It preserves host Git configuration while removing inherited variables that could redirect repository discovery or the index.

If a selected checkout is stale, the gate lists all relevant roots, branches/SHAs, dates, reasons, selected checkouts, and newest checkouts, then requires explicit `y` on stdin. This intentionally applies even with `NO_STD_IN=True`, help, or collect-only. Decline/EOF/unavailable stdin stops with a nonzero status; there is no bypass option. Inaccessible or unrelated registered checkouts also fail verification. The gate never creates, switches, repairs, or deletes worktrees.

Select an explicit resource pack with `--resource-pack-dir PATH` or `RESOURCE_PACK_DIR=PATH` (CLI takes precedence). Relative explicit pack paths resolve from the calling directory. Resource-pack launchers default to their own pack; api-agent defaults to its demo and shows a bootstrap hint only when no pack was explicitly selected.

Human-edited declarations and future workflow Layout/prompt files live under `cfg/`, organized by responsibility and node. Python parsers remain under `src/api_agent/config/`. Each resource pack has its own `cfg/` tree, selected by its root `pack.toml`.
