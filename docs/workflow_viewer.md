# Workflow diagrams and browser inspection

Open [flow.md.html](../flow.md.html) directly in a browser to read the workflow
and its saved SVG diagram. Nested declarations use relative navigation. Bundled
Markdown and sanitization scripts work offline after explicit source preparation;
no Java, container or additional font installation is needed. Diagram text uses
common platform/free font fallbacks, so its appearance may vary by system.
The linked SVG sits directly under each document heading. Expanded **Diagram links**
remain visible in repository Markdown, where an SVG image cannot expose its own
node links; successful local file or loopback rendering omits that extra list.
Companion Markdown under `.private_resources/diagrams/` provides another repository
navigation path. When scripts are unavailable, only the root shows launcher guidance;
the inert declaration carrier remains hidden.

Click a linked diagram to open a fitted overlay in both saved files and managed
viewing. Use Zoom in, Zoom out, Fit or Close; Escape returns to the document.
Diagram links remain interactive, so components, schemas and settings open directly.
Browsers lacking the required overlay APIs keep ordinary standalone SVG navigation.
The shared enhancement uses local packaged assets and needs no additional service.

Direct file opening shows the last generated diagram. It does not check whether
declarations have changed since generation. The commands below check freshness.

## Source dependency preparation

For a source checkout, install Node.js 24 or newer with npm. The launcher verifies
frontend dependencies before managed viewing and automatically runs the root
locked `npm ci` when preparation is needed. It reports preparation and missing
tools; it does not install Node or npm. The first install needs package access or
an npm cache. Postinstall copies unchanged official browser builds and notices
into ignored runtime files. Ready launches verify and reuse those files without
another install. Python graph execution does not require Node, npm or these
optional viewing assets.

Direct file opening cannot run launcher setup: run `npm ci` from the repository
root first, or launch managed viewing once. Fresh root and nested pages show
`npm ci` guidance if dependencies are missing. Unexpected existing generated
asset bytes require explicit inspection and preservation/removal before setup
can continue. Explicitly disabled browser scripts instead retain the generic
no-script fallback; that alone does not prove dependencies are missing.
Installed Python distributions already include the prepared files: do not run
npm in an installed package. Reinstall a complete distribution if its assets are
missing or damaged.

Building a wheel or source archive requires prepared, hash-verified assets.
Builds fail clearly rather than invoking npm or downloading. A prepared source
archive also contains the assets and notices, so rebuilding its wheel needs no
Node or network. The locked npm install and asset-copy helper are source-only.

## Managed viewing

From the checkout, run:

```sh
./launch.sh --view-flow
./launch.sh --view-flow "demo/contrived/flow.md.html"
./launch.sh --view-flow "tests/integration/fixtures/tool_instances/shared_primary/flow.md.html"
```

With no path, this opens the selected application's declared flow. The bundled
demo application currently selects `demo/simple/flow.md.html`; the repository-root
`flow.md.html` is the minimal chat example and can be selected explicitly.
Supply one `.md.html` path to inspect another flow. Relative paths use the current
directory; source-view mappings preserve each fixture's base/pack ownership.
The generated instructions in each graph page include its repository-relative
path in double quotes, except the repository-root flow, whose command omits it.
Declared tool-instance links open the captured TOML file, like schema and setting
links; resolved environment values are not served.

The launcher validates the selected declarations and saved diagram, starts a
temporary loopback viewer on an available port, and opens managed headed
Chromium. It prints the view URL and original browser CDP endpoint. Attach
inspection tools to that endpoint and use its existing page. Close the browser
or press Ctrl+C to release its browser and host. Viewing performs no graph
execution, provider authentication or application startup.

mise and the managed browser's host prerequisites are required;
the launcher prepares its browser dependencies. Fresh owner-local diagrams are
checked without Java or renderer setup. A library/composed/replacement selection
may need a new temporary diagram and therefore the optional generator below.
That view discloses its selected definition identity and never overwrites owner
diagrams or reloads replaced originals. Explicitly referenced, captured Python
sources open as UTF-8 plain text, alongside captured JSON schemas and TOML
declarations. The server verifies their hashes and serves only its finite captured
inventory; it never executes Python or reads arbitrary requested files. Other
unserved resources appear as nonclickable source details. Static files retain
their ordinary source links. The viewer link-crawl integration test checks rendered
pages and standalone diagrams across the included workflow hierarchies.

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
explicit generation to update saved diagrams. Managed viewing can render a
temporary view of current changes without modifying saved diagrams; when needed,
it prepares the pinned renderer in the development cache. Rendering needs Java
17 or newer available as `java` on PATH. To update saved diagrams, prepare the
pinned optional MIT PlantUML JAR, then generate:

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

A managed copy installed before locked npm asset delivery needs a one-time
manual refresh: that older copy does not pass the original dependency root to
the snapshot checker. First verify that the existing hook is the exact older
managed copy you own, and back it up outside the hook directory. Remove only
that confirmed owned copy, then run `./launch.sh --view-flow` to install the new
reviewed dispatcher in the vacant slot. Preserve unfamiliar, custom and shared
hooks and integrate the dispatcher manually instead. The launcher does not
automatically replace an older copy. The refreshed hook verifies original npm
assets against the staged lock and manifest; it never runs npm or fetches.

The hook checks an isolated index snapshot and compares its workflow inputs and
generated views with the working files. Stage the relevant changes together;
unrelated working-tree changes do not block the check. CI can run
`python tools/checks.py --head --root .` for the same checks and a comparison
against HEAD. If Git or checkout metadata is unavailable, this checks the actual
files and reports that a HEAD comparison was unavailable.
Checks bound index bytes, output and elapsed work, and dispose owned checker
descendants on failure or interruption. Broader lint/security checks and CI
wiring remain separately planned.
