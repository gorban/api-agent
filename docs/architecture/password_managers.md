# Password-manager implementations

The shared credential reader retrieves username, password or TOTP independently. API-key consumers request only password; subscription consumers request the fields their login flow needs. Manager selection does not change account roles or their paired indexes.

## Declarations

Bundled choices come from [the manager declaration](../../src/api_agent/auth/password_managers/cfg/managers.toml). A selected resource pack may add `cfg/password_managers.toml`:

```toml
version = 1

[[managers]]
id = "example"
label = "Example vault"
implementation = "../password_managers/example.py"
cli_default = "example-cli"
```

The implementation path is relative to the declaration file and must resolve inside its owning pack. Identifiers must be unique across bundled and pack declarations. The TUI displays friendly labels and derives the implementation path and CLI default from the chosen identifier. The stored shared-manager descriptor still contains only `manager` and `cli_path`; the derived implementation path is not saved.

Discovery reads declarations without importing implementation code or invoking CLIs. Changing the selected pack changes available choices. A saved unavailable choice requires correction or restoration of its pack; it never silently selects another manager. Each running operation captures its own registry so unrelated selections do not affect its credential reader.

## Implementation interface

An implementation file supplies `version = 1` and two ordinary synchronous functions:

```python
def command(source, kind, cli):
    # source has manager, cli_path, vault and user descriptors.
    # kind is "username", "password" or "totp".
    return [cli, "retrieve", source.vault, source.user, kind]

def parse(output, kind):
    return output.removesuffix("\n")
```

These functions create an argument list and parse stdout; they do not launch processes or authenticate. The shared runner invokes the explicitly configured executable without a shell, with closed stdin, bounded execution and owned process cleanup. Argument lists must start with the resolved configured executable, and parsers return one nonempty string. Blank usernames are invalid; password whitespace is preserved. Synchronous function signatures and version are checked before CLI execution.

The included [1Password](../../src/api_agent/auth/password_managers/onepassword.py) and [Keeper](../../src/api_agent/auth/password_managers/keeper.py) implementations preserve their established commands and CSV/raw parsing. Runtime loading uses fresh isolated module names and verifies the captured path has not been retargeted. Implementations are trusted pack code, not sandboxed plugins.

## Editor

**Edit shared password manager** shows the selected implementation's resolved `.py` path in a read-only field between **Password manager** and **CLI executable path**. Changing the dropdown updates that field immediately. A blank selection displays `-`. The derived path has no Tab stop; the executable path remains editable, with explicit overrides preserved.

The current POSIX loader opens each resolved directory component without following symlinks, then reads a regular-file descriptor. This prevents a directory swap during loading from redirecting execution outside the captured location. Platforms without directory-handle support retain canonical-path and final-file checks; equivalent Windows race hardening has not been verified.
