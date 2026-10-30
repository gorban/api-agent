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

Discovery opens declaration files nonblocking and checks their descriptors are regular files before reading. FIFOs fail promptly without importing implementation code or invoking CLIs. Changing the selected pack changes available choices. A saved unavailable choice requires correction or restoration of its pack; it never silently selects another manager. Each running operation captures its own registry so unrelated selections do not affect its credential reader. The accepted integration run captures one registry before suite callbacks and shares those definitions with its backend and every repository-suite snapshot. Snapshot creation never adopts newer declarations between suites. DEBUG workers compare current declarations with that snapshot before allocating a loop or retrieving credentials; changed, missing or invalid declarations require restarting launch.sh. A worker then uses that validated registry without reloading declarations during credential retrieval. This binds adapter locations, not the contents of trusted implementation code.

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

These functions create an argument list and parse stdout; they do not launch processes or authenticate. The shared runner invokes the explicitly configured executable without a shell, initially with closed stdin, bounded execution and owned process cleanup. Argument lists must start with the resolved configured executable, and parsers return one nonempty string. Blank usernames are invalid; password whitespace is preserved. Synchronous function signatures and version are checked before CLI execution.

The included [1Password](../../src/api_agent/auth/password_managers/onepassword.py) and [Keeper](../../src/api_agent/auth/password_managers/keeper.py) implementations preserve their established commands and CSV/raw parsing. Runtime loading uses fresh isolated module names and verifies the captured path has not been retargeted. Implementations are trusted pack code, not sandboxed plugins.

## Editor

**Edit shared password manager** shows the selected implementation's resolved `.py` path in a read-only field between **Password manager** and **CLI executable path**. Changing the dropdown updates that field immediately. A blank selection displays `-`. The derived path has no Tab stop; the executable path remains editable. Opening the editor preserves the saved CLI path. A synchronous value watcher records each live executable edit before queued dropdown events run, including clearing and restoring the same text. Deliberate identical-text replacements also count as edits, including selected paste or retyping; empty paste and no-op deletion do not. Generated defaults are recorded before the programmatic assignment, so they do not count as explicit edits; queued input notifications do not drive draft state. Unedited defaults follow the previous manager’s own default; explicit edits remain unchanged even if they equal another manager’s default.

The current POSIX loader opens each resolved directory component without following symlinks, then reads a regular-file descriptor. This prevents a directory swap during loading from redirecting execution outside the captured location. Platforms without directory-handle support retain canonical-path and final-file checks; equivalent Windows race hardening has not been verified.


## Interactive recovery

An adapter may additionally supply `classify_login(stderr) -> bool`. This synchronous hook recognizes its own authentication-required failure; omitted hooks preserve the command/parser interface. Keeper recognizes its persistent-login failure and genuine login prompts; lookup failures remain failures. Discovery validates the hook without invoking it. 1Password currently retains noninteractive retrieval without this extension.

The credential helpers accept an optional `interaction` and remaining enclosing `recovery_timeout`. An interaction opens a manager-labelled channel with `write(text)`, `read(timeout)` and `close()`. Reads yield an input, retry, cancel or EOF reply; reply representations omit their value. Recovery retries the original retrieval command, returning its private stdout through the existing parser. Keeper uses an owned POSIX controlling terminal for stdin and authentication output, while credential stdout remains a separate pipe. This path is currently supported on POSIX; other platforms fail with an explicit recovery-unavailable explanation.

The launcher selects terminal relay for its NO_STD_IN integration START/DEBUG routes or a temporary TUI screen for interactive execution. POSIX terminal imports occur only when recovery initializes a real terminal, so integration dispatch without recovery remains available on other platforms. Requesting unsupported recovery gives the explicit POSIX explanation. Terminal relay has no isatty prerequisite: it displays login output immediately and accepts supplied stdin. Reads remain bounded to 4096 bytes including the current reply's newline; fragmented replies preserve queued future replies rather than counting prefetch against the current reply. EOF, cancellation and applicable component deadlines stop the invocation. This exception does not grant integration/E2E execution acceptance. The TUI shows Output, masked Input, Send value (Enter), Cancel with literal-y confirmation, and Ctrl+R retry. The runner disposes the old process before closing its screen/channel or opening a retry generation.

Recovery is coordinated within the operation; pending requests retry retrieval after login without sharing password/TOTP values. A queued request can be canceled without canceling the active invocation; canceling the invocation’s owner disposes it before the next request retries. Shared readiness consumers retain their existing cancellation shielding. Operation shutdown cancels all and drains owned helpers. A separate POSIX guardian watches a caller-owned pipe, so even a forcibly terminated DEBUG worker cannot leave its helper group running. The guardian retains the helper leader until group disposal; on Linux it also adopts and reaps orphaned descendants. The short retrieval attempt and human recovery use the shared component progress interface and inherit enclosing deadlines, including time spent waiting for coordination. Retry never renews that deadline.

Live authentication output and entered values use a dedicated interaction boundary. They do not enter pytest output, Allure, traces, general logs or readiness failures. DEBUG workers use a protected loopback interaction endpoint in their launch snapshot; ordinary shared setup retains the launcher-owned interaction directly. Captured manager definitions and selected credential roles remain unchanged. Error reporting exposes safe login-required/recovery explanations without credential stdout or raw CLI transcripts.

Recovery installs a filter before Textual formats diagnostic events for its file or devtools sinks. Password Input change/submission/blur values and raw key/paste payloads are redacted. Key/paste redaction remains active for the process after recovery closes, covering queued or bubbled events. The raw terminal parser's separate `keys.log` sink is disabled for existing and future parsers before recovery entry, even with `TEXTUAL_DEBUG=1`; general debug diagnostics remain available. A per-call logger clone preserves the original devtools emitter's filename, function and line without mutating a shared logger or application sink. Masked rendering alone is not a logging privacy boundary.
