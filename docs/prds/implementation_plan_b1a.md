# Versioned workflow resources and diagram views

[Declarative descriptions](implementation_plan_b.md) · [Markdown grammar](implementation_plan_b1.md) · [staged checks](implementation_plan_i1.md)

Versioned declarations and the standalone static/managed viewer are implemented. Application startup, final workflow tabs, broader CI enforcement and execution overlays retain their own requirements below. Public examples are independently authored from generic contracts; earlier accepted fixture evidence remains historical.

The ordered migration and behavior-preservation acceptance live in [B1b](implementation_plan_b1b.md); this file owns the target resource/viewer contract.

Review the [contract freeze checklist](implementation_plan_b1b1.md) before dependent conversion.

## Resource layout

- [ ] Use root `flow.md.html` as the human entry and `README.md` for navigation. Application repositories also retain their launcher, license and required packaging, pack-manifest and Git metadata at root. Place declaration children under `workflows/components/`, `workflows/contracts/`, `workflows/routing/`, `workflows/streams/`, `workflows/overlays/`, with `workflows/runtime.md.html` for progressive disclosure. Keep prerequisites in `docs/`, source bindings in `cfg/` and sample generation/preview tools in `tools/`. Callable implementations remain native Python in their owning source package.
- [x] Store generated SVGs under `.private_resources/diagrams/`, mirroring declaration paths: root `flow.md.html` maps to `flow.svg`; each nested declaration maps to its corresponding nested SVG. Put viewer scripts, styles and required third-party notices under `.private_resources/viewer/`. Package declarations and assets together; navigation works after relocation without sibling source checkouts. Source links remain external where declared; retain configuration-line links and optional local source viewing within allowed roots.
- [ ] Preserve grouped nodes, inline prompts, conditional routes, type fields and inbound backlinks in the versioned grammar. Resolve registry identifiers to native Python functions and validate input/output bindings, conditions, prompt order, timeout scopes and explicit selected-pack overrides before graph execution. A versioned, nonexecuting `Diagram` section may contain fixed node/edge tables for conceptual views that cannot be inferred; it cannot alter executable routing or contain Python expressions or raw PlantUML.

## Viewer and generated views

- [x] Derive registered nodes, forward routes, inbound backlinks, type fields and navigation links from the Markdown declaration body. Build SVG links and companion Markdown links from the same inventory. A standalone SVG may expose its links while GitHub's embedded SVG image requires companion Markdown navigation.
- [x] Generate temporary `.puml` only inside an owned temporary directory; render with pinned PlantUML, dispose of temporary files on success, failure and cancellation, and atomically publish SVGs. Track no `.puml` source. Verify deterministic markup projection and source provenance; font-dependent geometry does not determine freshness.
- [x] Keep a root-only help panel with the heading **Viewing this file**, the Python 3.13 or newer prerequisite and the exact root command `./launch.py --view-flow`. Nested declarations contain only rendering bootstrap. Remove `?view=help` special handling; ordinary URLs bearing that query render normally. IDE comments stay invisible in rendered views and use exactly five empty lines before and after, two leading spaces on every prose line, and the command at column one.
- [x] Restrict local preview access to declared resources and an explicit `.private_resources` asset subtree allowlist. Reject traversal, symlink escapes and source access beyond the optional declared source viewer. Rewritten Markdown, SVG, script and stylesheet links must work from root and nested declarations.

## Runtime integration acceptance

- [ ] Convert the parser/compiler and selected application declarations to the versioned `.md.html` format together. Prove equivalent graph transitions, prompt composition, routing, state/bindings, timeouts, streaming and explicit pack selection/overrides before treating the new format as executable.
- [x] Implement `./launch.py --view-flow` through the shared managed Playwright lifecycle. Prepare the browser dependency, start an owned loopback preview on an available port and open root `flow.md.html` automatically in a headed browser regardless of caller working directory. Cleanup closes owned browser/server resources. Viewing performs no graph execution, provider login or application startup.
- [ ] In Run local, host the same workflow resources and open the selected flow as the final tab in its existing managed Playwright browser after readiness, regardless of UI or tracing settings. This is a static declaration/diagram view of the graph compiled for this invocation, including resolved pack overrides. Preserve the other Run local tabs and ownership rules; [K2](implementation_plan_k2.md) owns their readiness and ordering.
- [x] Verify root-only fallback, ordinary query rendering, exact comment whitespace, markup freshness, temporary cleanup, local configuration/source views, GitHub Markdown links, packaged-resource navigation, launcher/browser cleanup and staged-check rejection cases. See [I1](implementation_plan_i1.md) for the index and CI contract.

## Base runtime and resource conversion

- [ ] Convert base `api-agent` itself: its parser/compiler in `src/api_agent/workflows/layout.py` and `execution.py`, workflow CLI, packaged permanent fixture in `demo/contrived/`, and layout-path consumers such as `contrived.py` and `fixture_driver.py`. Provide root `flow.md.html` and nested `workflows/` resources for the selected base graph; retain native Python callables and prompt, schema and configuration bindings. Keep test fixtures explicitly selectable, with their own resource entrypoints.
- [ ] Update package manifests, resource lookup, CLI validation/diagram commands and fixture source fingerprints together. Verify routing, bindings, prompt order, retry/timeout behavior, streaming, provenance and permanent fixture behavior before retiring old Layout paths. Requalify changed fixture fingerprints through the existing calibration process; retain historical results as dated evidence. Test installed CLI and packaged navigation from a relocated wheel without sibling checkouts. This migration preserves the completed [B2](implementation_plan_b2.md) contracts and does not resume its deferred phase two.

## Get started documentation

- [ ] Once the browser tabs and workflow entry are implemented, update `README.md` under **Get started**, immediately after the launcher command. Describe Run local starting the app and opening a managed browser with the Agent app, the enabled UI provider for interacting with the agent's API, configured tracing viewers, and the selected workflow as the final tab. Link the exact words `actual LangGraph flow` to root `flow.md.html`. Verify this sequence with UI/tracing enabled and disabled and with explicit pack selection before README acceptance.

Target wording, to publish with the implemented behavior:

```markdown
Select **Run local** to start the app and open a managed browser with the Agent app, the configured UI provider (when enabled) for interacting with the agent's API, and any configured tracing viewer after it is ready. A final tab shows the [actual LangGraph flow](../../flow.md.html) used by this run.
```

## Later dynamic inspection

- [ ] After this static viewer and application/E2E evidence are delivered, extend this same host with [historical, recorded and live execution overlays](implementation_plan_b1c.md). Freeze attribution seams during runtime work; full inspection remains late before profiling. Static viewing retains its no-execution/no-auth behavior.

## Declaration and diagram compatibility checkpoint

- [x] Prove that diagram generation and browser presentation preserve the admitted executable graph: selected nodes, effective routes, ordered prompts, schemas, bindings, timeouts and overrides. Exercise root and nested documents, source and relocated installed packages, and declared replacements. Reject unsupported presentation markup explicitly; never silently reinterpret it as executable declarations.
- [x] Use causal controls for altered executable content, stale or substituted diagrams and changed unselected originals. Demonstrate that viewing performs no native graph execution or authentication and leaves source and installed files unchanged.
- [ ] Revisit human acceptance of the concrete declaration format and execution evidence together with the completed diagram viewer. Existing preparation approval and absence of a reported blocking issue do not constitute this acceptance. Application lifecycle acceptance remains separate.

## Static document and freshness decision

- [x] Deliver [portable static documents and markup-based diagram freshness](implementation_plan_b1a1.md). Freshness compares canonical declaration/resource inputs and projected PlantUML, independently of platform font geometry. SVG safety and graphic integrity remain separate checks. Static browser opening requires no generator, server, container or bundled font.
