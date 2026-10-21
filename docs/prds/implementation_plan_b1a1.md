# Portable static documents and markup freshness

[Workflow resources and viewer](implementation_plan_b1a.md) · [Staged checks](implementation_plan_i1a.md)

- [x] Support direct browser opening of root and nested `.md.html` without a server or network dependency. Use an exact versioned presentation envelope around inert executable Markdown; reject arbitrary HTML/scripts. Preserve readable declarations, root-only fallback help and specified IDE comments. Bundle the licensed Markdown/sanitization scripts and their notices. Hosted augmentation cannot change execution semantics.
- [x] Keep SVGs external under the owning `.private_resources/diagrams/` tree, with companion Markdown for repository-hosted navigation. Use best-effort common platform/free font-family fallbacks inside SVGs and document styles. Bundle or download no fonts and require no container toolchain. Static viewing needs no Java/PlantUML; geometry and glyph appearance may differ by platform.
- [x] Define freshness against the admitted canonical markup and selected declaration/resource closure, plus projected PlantUML and pinned generator/options provenance. Exclude only the versioned presentation envelope and generated navigation section; bind presentation assets separately and avoid circular self-hashes. Compare the embedded projected source and input hashes to the current projection. Never require equality with locally regenerated SVG geometry.
- [x] Check actual saved SVG bytes for bounded safe vocabulary, links and geometry, and verify their recorded graphic digest. This detects corruption separately from markup freshness; a self-declared digest is not an independent signature or proof of drawing fidelity. Compare generated HTML, companion links and viewer assets with their expected deterministic content.
- [x] Managed viewer preflight and explicit/staged check commands validate the selected saved snapshot without Java, renderer lookup, downloads or graph/provider execution. Missing, unsafe, corrupt or source-stale output fails with generation guidance. Direct `file://` opening displays the last generated artifact; it does not run the Python freshness check.
- [x] Use the pinned optional PlantUML generator only during explicit generation or when a managed selected variant needs a new temporary diagram. Dispose owned processes/temporary files on success, failure and cancellation. Explicit source generation leaves corrections unstaged and preserves concurrent edits. Ordinary agent execution requires no generator, viewer or hook installation.
- [x] Keep owner-local static artifacts separate from managed library/replacement variants. Derive temporary views from admitted selected inputs, disclose graph/definition identity and bind diagrams to that markup. Never overwrite owner diagrams, reload unselected originals or substitute a different graph. Preserve source and installed files unchanged while viewing.
- [x] Require explicit per-path confirmation before removing obsolete generated outputs. Markers and self-computed hashes cannot establish deletion ownership; preserve unknown or edited files.
- [x] Prove fresh checks work without Java; alternate safe font geometry does not become stale; changed declarations, linked resources, projected-source metadata or companion links reject. Reject unsafe SVG even with a recomputed digest and reject damaged graphics without a matching digest. Verify static/hosted root/nested and relocated-wheel navigation, parser equivalence and original managed-browser cleanup before combined human acceptance.

## Verification and human acceptance

The source implementation and repairs passed independent code review, the complete applicable unit gate at exact line/branch coverage, actual renderer/browser checks and relocated installed-package verification. Ordinary freshness checks require no renderer. Broader application startup, execution overlays and full CI wiring retain their own requirements.

- [ ] Human acceptance alongside the combined declaration/diagram and managed-viewer checkpoint.


## Static presentation order and fallback visibility

- [x] Show root viewing guidance only when scripts are disabled, blocked or unavailable. A successful static or hosted render removes the fallback and does not prepend a replacement help panel.
- [x] Place the saved diagram directly after the document's top heading, with the image itself linking to its standalone SVG. Preserve nested navigation and useful diagram links without exposing generated-section bookkeeping as an extra bottom diagram. Apply this presentation consistently to root and nested documents.
- [x] Keep executable Markdown, source locations, selected graph identity, sanitization and owner-local image checks unchanged. Verify actual file and hosted browser behavior, script-disabled/missing-asset fallback, SVG navigation and causal layout controls before renewed combined human acceptance.


## Three-context template and SVG navigation

- [x] Keep the linked SVG directly after the top heading in saved Markdown as well as browser output. Retain expanded diagram-navigation alternatives for repository Markdown rendering, and remove them after successful file or loopback rendering.
- [x] Admit an exact versioned Markdown-compatible browser bootstrap. Use the Markdown-mode editor hint for every declaration; only the root receives the spaced editor instructions and visible no-script/blocked-script guidance. Put exactly five empty lines inside the instructional comment before the prose and after the launcher command. Explain browser opening and Python 3.13 or newer for managed viewing; keep the command at column one and prose indented by two spaces.
- [x] Keep raw Markdown visually suppressed when scripts are unavailable, retain the exact carrier hidden for successful-render inspection, assign a heading-derived page title, and provide stable heading anchors and fragment navigation. Preserve common-font light/dark table, code and quotation presentation without bundled fonts.
- [x] Generate internal standalone-SVG targets as scheme-relative GitHub repository URLs with the reserved branch placeholder `_____switch_branch_____`. File and loopback viewing rewrite those targets to the corresponding admitted local documents; hosted repository views retain them. Keep local SVG image sources relative and owner-bounded.
- [x] Bind repository identity and browser navigation assets into diagram provenance. Record owner identity in `cfg/workflow-view.toml`; initialize from a safe GitHub origin during explicit first generation or require an explicit URL when no origin exists; staged checks use the recorded identity and never silently rewrite files or depend on a live remote. Preserve finite route inventories, explicit owner boundaries, unchanged graph semantics and renderer-free freshness.
- [x] Preserve canonical single-decode URL semantics for admitted percent-bearing declaration and diagram paths. Keep literal percent characters encoded through image admission, standalone SVG navigation and finite host lookup; reject once-decoded dot segments, separators, controls, invalid UTF-8 and noncanonical aliases. Verify image and adapter controls separately, including literal percent-escape names and parent directories. Actual direct-file browser acceptance remains unchecked; pure URL checks and loopback HTTP evidence do not replace it.
- [x] Independently verify root/nested declarations, SVG node clicks, fragments, disabled/missing scripts, editor comments, source/relocated-wheel operation and repository-Markdown alternatives. Keep actual repository-platform verification distinct from local simulation. Version presentation changes, preserve executable line maps, and requalify changed sampling-bound code through the existing authorization/review gate.

## Locked offline dependency delivery

- [x] Prepare original pinned npm browser assets through an explicit root `npm ci`, with committed lockfile integrity and deterministic verified copying. Keep generated third-party JavaScript out of tracked source; retain required upstream notices. After preparation, static and managed viewing remains offline. Missing source dependencies show root/nested install guidance and managed preflight fails clearly without fetching. Installed Python viewing and graph execution require no Node/npm preparation.
- [x] Build self-contained wheels and sdists only after validating all expected generated asset hashes. Builds never fetch or invoke npm. Verify missing-dependency failures, root/nested HTTP viewing, source and relocated installed-package behavior; keep actual direct-file browser acceptance unchecked.

The joined source and retained-journal repair passed independent and external source review at `9125c59e`. The full gate passes 12,708 units at exact measured line/branch coverage. Locked npm preparation, installed-only runtime checks and package notices are verified; direct-file and repository-platform human acceptance remains open above. [Fresh runtime qualification](implementation_plan_b2b1a4.md) preserves the earlier observations for their original source bindings.


## Scope and discovery correction

The [scope-correct diagram and recursive discovery repair](implementation_plan_b1a3.md) reopens presentation delivery. Earlier source/qualification evidence remains historical; opaque scope views and the permanent fixture require renewed technical and human review.
