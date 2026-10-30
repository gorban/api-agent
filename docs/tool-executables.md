# Add a tool in another language

[Named tools](tools.md) · [Resource packs](resource-packs.md) · [Infrastructure](tool-infrastructure.md)

A resource pack can select its own `flow.md.html` and link named tool instances
at graph, component or node scope. Each instance selects an implementation.
The implementation still exposes the small Python `create_tool(config, services)`
adapter contract; its actual work may run in a .NET, Rust, Node or other subprocess.
Selecting a language toolchain does not create an adapter or translate protocols.

## Declare executable requirements

Add an owner-relative link to the implementation descriptor, before its tables:

```toml
version = 1
interface = "verified-read@1"
factory = "factory.py:create_tool"
executables = "executables.toml"
```

Create `executables.toml` beside it:

```toml
version = 1
requires = ["shared/build-tools.toml"]

[tools]
"core:dotnet" = "8.0.425"
```

An explicitly linked `shared/build-tools.toml` can add another requirement:

```toml
version = 1

[tools]
"core:rust" = "1.90.0"
```

These are illustrative exact pins, not a recommendation to keep those versions
forever. Choose and qualify the versions your implementation supports. Omit
`requires` when no further manifest is needed. A Rust-only tool can declare only
`core:rust`; a downloaded native binary may need no Rust compiler at all.

Requirements follow relative links within the declaring implementation's owner,
including distribution-owned assets. Absolute paths and cross-owner links reject.
They do not follow arbitrary directories or infer
requirements from filename extensions. Ship every linked file in an installed
pack's distribution receipt. Same-version duplicates share a closure; cycles,
escaping paths and conflicting versions inside one closure fail before startup.
The closure is limited to 64 manifests and 64 distinct executable identities.

Use fully qualified `core:`, `aqua:`, `github:`, `cargo:`, `dotnet:` or `npm:`
identities with exact three-part versions (optional `v` prefix or release suffix).
Short aliases, ranges and moving channels such as `latest` or `stable` reject.
Availability still depends on mise's backend and the host platform.

## Use the prepared environment

After configuration and Python dependency validation, runtime startup prepares
the selected executable closure before importing the tool factory. Factories
receive immutable environment **overrides** in `services['tool_environment']`.
Merge them into the environment of every child that needs those tools:

```python
import os

def create_tool(config, services):
    environment = {**os.environ, **services['tool_environment']}
    return DotnetTool(config, environment)  # your version-1 owner implementation
```

Inside that owner's bounded startup/invocation methods, pass `env=environment`
to your subprocess API. Use argument vectors, not shell interpolation; retain
the ordinary tool validation, deadlines, cancellation and owned cleanup contract.
The example above is adapter wiring, not a complete callable tool.

Do not modify `os.environ`, shell startup files or global language defaults.
Different tools can use different subprocess toolchains. Mise supplies Rust
toolchain/home variables and an isolated .NET SDK environment, not just PATH.
The app does not load a component's mise env, hooks, tasks or templates. The
manifest is constrained requirement data, not an arbitrary `mise.toml` passthrough.
Only use trusted packs: installing and running their tools executes third-party code.

## Preparation boundaries

- Help, graph discovery and test collection read declarations without installing
  toolchains. Runtime startup prepares only the operation's selected tools;
  exact test selections narrow the captured closure.
- Shared identical closures are prepared once per runtime startup. Mise reuses
  installed versions on later operations. Installers are bounded and cancellation
  waits for their owned processes to finish cleanup.
- NuGet, Cargo, npm and Python dependencies keep their native package/lock
  mechanisms. The adapter owns bounded restore/build preparation and its source
  assets; this executable manifest does not automatically run `cargo build`,
  `dotnet restore` or npm scripts. Do not install during individual tool calls.
- Exact executable versions are pinned, but this extension does not yet consume
  pack-owned `mise.lock` artifact URLs/checksums. It uses mise's backend download
  verification; the base launcher retains its separate checked artifact lock.
- Native libraries/linkers, Docker engines, credentials and permissions remain
  conditional host requirements. Container-only toolchains belong in the image,
  not in this host executable declaration. A remote endpoint does not remove
  a selected local adapter's own toolchain requirement.

Verify a real executable/version and an actual adapter operation on each supported
platform. Parser or mocked installer tests alone do not establish platform support.
Acquisition/version smoke checks have run on Linux/x64 with .NET 8.0.425 and
Rust 1.90.0. Other platforms and actual application-specific adapters require
their own qualification.
