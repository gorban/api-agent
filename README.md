# api-agent

The shared Python runtime is being rebuilt. The foundation implements typed configuration, resource-pack discovery, source launchers, and a Git/worktree provenance gate. The interactive TUI, test runner, authentication, providers, and application API are the next implementation group. `--help` works; other operations currently return a clear nonzero "pending Segment B" result and start no tests or services.

## Development setup

Use Python 3.13 or newer and a project virtual environment. From this repository:

```sh
uv venv --python python3 .venv
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
./launch.py --help
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m build
```

Activate `.venv` in each new shell before using `./launch.py`; its `#!/usr/bin/env python3` shebang selects Python from the active environment. Both launch scripts are executable in Git. If a file copy loses the executable permission, restore it with `chmod +x launch.py`. Without activation, use `.venv/bin/python launch.py --help` explicitly.

This installs the current runtime checkout for development. Build artifacts and the environment are ignored. Unit tests make no live LLM calls. Source launchers use their own checkout, independently of the current directory; installed console entry points require the known editable source layout, and direct wheel execution reports an actionable source-launcher error instead of guessing a checkout.

## Foundation review

```sh
.venv/bin/python launch.py --help
.venv/bin/python launch.py --no-std-in
.venv/bin/python launch.py --no_std_in=True --tui-mode unit
.venv/bin/python launch.py --no-std-in=True --tui-mode integration
```

The missing-mode example returns configuration error status `2`. A valid unit configuration currently returns status `3` because the runner is not implemented; it does not pretend to pass tests. Integration/E2E execution rejects `NO_STD_IN=True`; collection-only bypasses that execution restriction but collection itself is also pending. Help returns `0` after the provenance gate. Git/pack/schema/bootstrap errors return `2`. These foundation statuses will be extended by the real operation-specific statuses in the TUI implementation.

## Executable declarations

- [Shared configuration](src/api_agent/config/shared.toml) declares a version and each field's type, defaults, conditions, path anchor, help, and presentation metadata.
- [Demo pack](demo/pack.toml) selects the [demo extensions](demo/config.toml), test owner, suite paths, and default overrides. The included demo is also bundled in the wheel.
- Resource packs may provide their own root `pack.toml` and `config.toml`, app defaults, and authentication declarations. No authentication runs in the foundation.

Values resolve as shared defaults, then pack defaults, then the active `.env`, environment, and CLI. The bundled demo uses the base repository's `.env`; a resource pack uses its own root `.env`, independently of where its tests are owned. Resolution retains whether a value is explicit and its source. An extension can refer to shared controls in its conditions; inactive requirements do not block an operation.

Types currently include `bool`, `int`, `str`, `path`, and compact JSON objects. CLI/environment booleans are exactly `True` or `False`; bare boolean flags mean `True`. `--field-name` and `--field_name` are equivalent, as are `--field=value` and `--field value`. `-h` sets `HELP=True`. A value beginning with `--` is treated as another option, even after `=`; use a relative directory prefix such as `./` when appropriate. `--` ends option parsing, and positional selectors require an explicitly configured test mode. The literal `.env` reader accepts `KEY=value`, quoted values, comments on their own lines, and optional `export`; it performs no shell expansion or command execution.

Directory values use declared `base_root`, `pack_root`, or `owner_root` anchors rather than the process working directory. Configure `UI_SOURCE_DIR` explicitly relative to the test-owner repository root when UI execution is required. A resource pack's `API_AGENT_DIR` defaults to `../api-agent` relative to its pack root and may be overridden through its `.env`, environment, or CLI.

The general LangGraph Markdown Layout and prompt interpreter are later work. No empty workflow, provider, knowledge, or navigation files have been created to imply those features already exist.

## Worktree provenance

Before help, collection, or any future TUI operation, the gate requires Git and checks the selected base, resource-pack source, and test-owner repositories, deduplicating nested roots. It compares registered checkouts independently by the newer of HEAD's committer timestamp and the newest nonignored unstaged/untracked file's modification time. Staged-only changes do not count; missing/deleted files have no invented modification time. Ties are accepted. It preserves host Git configuration while removing inherited variables that could redirect repository discovery or the index.

If a selected checkout is stale, the gate lists all relevant roots, branches/SHAs, dates, reasons, selected checkouts, and newest checkouts, then requires explicit `y` on stdin. This intentionally applies even with `NO_STD_IN=True`, help, or collect-only. Decline/EOF/unavailable stdin stops with a nonzero status; there is no bypass option. Inaccessible or unrelated registered checkouts also fail verification. The gate never creates, switches, repairs, or deletes worktrees.
