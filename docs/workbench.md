# Settings and TUI controls

## Getting started

Run `./launch.py` from api-agent or a resource pack's own launcher. Python 3.13+
is required. The launcher prepares a local `.venv` and dependencies; `uv` speeds
up that setup when installed. An editable package installation is unnecessary.

To create and activate the environment explicitly:

```bash
python3 -m venv .venv
source .venv/bin/activate
./launch.py
```

Activate `.venv` in each new shell. If a file copy lost executable permissions,
run `chmod +x launch.py`. `python3 launch.py` is an alternative.

Choose **Run local** to start the agent. Review these settings for connected
services:

- `RUN_UI`: True also launches the selected UI provider's frontend locally.
  UI-specific paths, projects and ports come from that provider's settings.
- `API_ENDPOINT`: for a pack offering managed upstream API hosting, a nonempty
  URL uses the remote service; an explicit blank requests its local API stack.
  Unsetting restores any declared default, which may be a remote URL.
- `AGENT_DJANGO_URL`: the agent's Django listener address. This does not choose
  the upstream API; its base default is `https://localhost:8443`.

The pack/API provider owns upstream provisioning and any database options;
there is no separate shared `RUN_API` switch. See [local operations](development.md#running-locally)
for readiness, logs and cleanup. The current launcher does not yet execute local-run or upstream API provisioning;
[the infrastructure plan](implementation_plan_h.md) tracks that wiring.

## Review and save settings

Startup review lists unacknowledged defaults and missing potentially required
settings. A new default appears automatically, giving you a chance to override
it or preserve a reproducible configuration. Advanced/operation-only fields
may be hidden here; their executable declarations and help remain available.

- **a** accepts and saves a default immediately.
- **e** edits a value; Enter saves. Enums and booleans use a picker.
- **u** removes the saved assignment and restores its fallback.
- **?** opens the setting's description.
- **c** advances to the main menu.

The table shows effective values and their sources. Saved values take effect
immediately; later operations re-resolve settings. **s** switches the save target
between base and pack `.env` files. Resaving moves the assignment to that target.

**Clear .env configuration** deletes one selected assignment, with confirmation,
from all applicable `.env` files. Hidden settings appear there when saved.
Account descriptors have their own [credential editor](connections.md).

## Overrides and command-line options

Defaults come from shared, pack and selected-provider declarations. Providers
can contribute or override fields; changing providers refreshes the inventory.
Explicit overrides then apply in this order:

base `.env` → pack `.env` → environment variables → CLI arguments

Only keys present in the pack `.env` override the base file. Use `.env` for local
choices; source-controlled TOML describes defaults. The reader performs no
shell expansion or command execution. Never commit `.env` or fetched secrets.

Run `./launch.py --help` for CLI behaviors. Settings live in
[shared configuration](../cfg/config.toml) and selected pack/provider declarations.

- `--field-name` and `--field_name` are equivalent.
- `--field=value` and `--field value` are equivalent; booleans accept exactly
  True/False, and a bare boolean option means True.
- A space-separated value starting with `--` starts another option. Use
  `--field=VALUE` when that is the intended literal value.
- Duplicate and unknown options fail. `--` ends option parsing.

Declared path anchors determine relative setting paths; the working directory
is not a universal anchor. Explicit `--resource-pack-dir PATH` resolves relative
to the calling directory. A pack launcher selects its own pack by default.

## Navigation and worktree safety

The bottom bar shows available shortcuts. Tab selects controls; arrows and
h/j/k/l navigate tables and scroll panes. Space or Enter toggles test selections.
Copy uses Ctrl+C on non-Mac systems and Cmd+C on Mac. On the main menu, **q** exits
immediately; active operations require confirmation and owned cleanup.

When Git successfully confirms a newer worktree, startup requires explicit
**y** and Enter before proceeding—even with help, collection or NO_STD_IN.
Missing Git or unverifiable worktree metadata silently skips the check. The
gate never creates or changes worktrees. See [the provenance contract](implementation_plan_a6.md)
for recency rules and timestamp limitations.
