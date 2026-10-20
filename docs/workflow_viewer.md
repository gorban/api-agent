# Workflow diagrams and browser inspection

Open [flow.md.html](../flow.md.html) directly in a browser to read the workflow
and its saved SVG diagram. Nested declarations use relative navigation. Bundled
Markdown and sanitization scripts work without a server or network connection;
no Java, container or additional font installation is needed. Diagram text uses
common platform/free font fallbacks, so its appearance may vary by system.
The linked SVG sits directly under each document heading. Expanded **Diagram links**
remain visible in repository Markdown, where an SVG image cannot expose its own
node links; successful local file or loopback rendering omits that extra list.
Companion Markdown under `.private_resources/diagrams/` provides another repository
navigation path. When scripts are unavailable, only the root shows launcher guidance;
the inert declaration carrier remains hidden.

Direct file opening shows the last generated diagram. It does not check whether
declarations have changed since generation. The commands below check freshness.

## Managed viewing

From the checkout, run:

```sh
./launch.py --view-flow
```

The launcher validates the selected declarations and saved diagram, starts a
temporary loopback viewer on an available port, and opens managed headed
Chromium. It prints the view URL and original browser CDP endpoint. Attach
inspection tools to that endpoint and use its existing page. Close the browser
or press Ctrl+C to release its browser and host. Viewing performs no graph
execution, provider authentication or application startup.

Python 3.13 or newer and the managed browser's host prerequisites are required;
the launcher prepares its browser dependencies. Fresh owner-local diagrams are
checked without Java or renderer setup. A library/composed/replacement selection
may need a new temporary diagram and therefore the optional generator below.
That view discloses its selected definition identity and never overwrites owner
diagrams or reloads replaced originals. Captured native/configuration source is
not exposed as HTTP routes; hosted resource links identify path, kind and digest.

## Check and regenerate

Freshness compares canonical workflow markup, its selected declaration/resource
inputs and projected PlantUML with saved source metadata. Presentation and SVG
self-references are excluded to avoid circular hashes. Saved SVG safety and
graphic integrity are checked separately. Font-dependent coordinates are not a
freshness criterion; the integrity digest is not an independent signature.

Using the checkout's prepared Python environment, check saved output:

```sh
.venv/bin/python tools/workflow_views.py check --root .
```

Check mode is read-only and needs no renderer or download. Changed declarations,
linked resources, diagram source metadata, companions or viewer assets require
explicit generation. Generation needs Java 17 or newer available as `java` on
PATH. Prepare the pinned optional MIT PlantUML JAR, then generate:

```sh
.venv/bin/python tools/workflow_views.py prepare
.venv/bin/python tools/workflow_views.py generate --root .
.venv/bin/python tools/workflow_views.py check --root .
```

Repository navigation is recorded in `cfg/workflow-view.toml`:

```toml
version = 1
repository_url = "//github.com/example/agent"
```

An optional `path_prefix` selects a directory within that repository. Explicit first
generation can initialize this file from a safe GitHub `origin`; without one, supply
`generate --root . --repository-url //github.com/example/agent`. Review the generated
configuration with the diagrams. Read-only and staged checks use this recorded
identity and never query or modify a remote.

Saved SVG links use `//github.com/OWNER/REPOSITORY/blob/_____switch_branch_____/PATH`.
The placeholder deliberately does not guess a branch. File and loopback viewing
rewrite that exact prefix to admitted local documents, including heading fragments;
repository-hosted SVG links retain the placeholder. The SVG image path itself stays
relative. Repository identity, the adapter and its allowed mappings are bound into
the saved diagram provenance.

Preparation uses an owned development cache; `API_AGENT_PLANTUML_JAR` can point
to an already downloaded JAR with the required digest. The JAR and Java runtime
are not shipped in the application wheel; diagrams add no bundled fonts. Generation uses temporary
PlantUML input and leaves all source changes unstaged. Review declaration,
companion and SVG changes together before staging them. Normal agent execution
requires none of these viewer/generator tools.

Unknown or edited obsolete outputs are preserved. Removing an orphan requires
both `--fix-orphans` and an explicit `--confirm-orphan PATH` for each inspected
path. A generated-looking marker does not authorize deletion.

## Commit-time checks

Any launcher invocation assures a missing managed pre-commit dispatcher in an
ordinary Git checkout. Installation runs no checks. The stable hook delegates to
tracked `tools/checks.py`, allowing future checks to change that omnibus runner
without replacing the hook. Existing user hooks, custom hook locations and shared
worktree hooks are preserved; the launcher explains manual integration when
needed. Without Git or checkout metadata, launch continues without installation.

The hook checks an isolated index snapshot. Stage regenerated diagrams with
their declarations: an unstaged correction cannot repair a stale proposed commit.
Checks bound index bytes, output and elapsed work, and dispose owned checker
descendants on failure or interruption. Broader lint/security checks and CI
wiring remain separately planned.
