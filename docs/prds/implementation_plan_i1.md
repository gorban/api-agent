# Managed pre-commit assurance and shared quick CI checks

[Implementation follow-ups](implementation_plan_i.md).

The minimal dispatcher and diagram slice is [I1a](implementation_plan_i1a.md); broader quick checks and CI wiring remain planned. Local checks and CI share repository-owned rules. Complete-unit and exact production coverage gates remain mandatory and separate. Manual review enforcement for prompts and `AGENTS.md` is undecided; automated checks cannot approve those changes.

## Launcher and hook ownership

- [ ] On any launcher invocation in an ordinary Git checkout, assure the missing stable pre-commit dispatcher and recognize its trusted identity. Do not routinely replace an existing hook. Installation starts no checks, generators, tests, authentication or services.
- [ ] Respect existing hooks and `core.hooksPath`. Never overwrite an unowned hook, change the user's hook configuration or modify a shared hook directory automatically. Preserve an existing managed hook's documented integration with other hooks. When ownership prevents installation, report assurance unavailable with a concrete manual integration path; never claim enforcement succeeded.
- [ ] Skip hook assurance without Git or checkout metadata, including installed/archive and CI use, without blocking launch. Help, collection and `NO_STD_IN` may assure the dispatcher but must not start generation or quick checks.
- [ ] Keep installation atomic, idempotent and bounded, including interruption and unwritable destinations. Agree the managed identity and compatible chaining mechanism before implementation; do not install anything merely because these requirements were recorded.

## Staged input contract

- [ ] Default entrypoints are read-only check mode and require Git. Evaluate the entire index snapshot, including staged source, declarations, checker/formatter settings, dependency pins, generator inputs and generated outputs. Partial staging must check the proposed commit, not the working tree's newer version. Run checker/generator entrypoints from that snapshot or a trusted pinned runner; unstaged script/configuration edits must not bypass a failing index.
- [ ] Materialize an isolated snapshot with indexed modes/paths and reject unresolved index conflicts or incomplete inputs. Never fall back to unstaged/untracked files, personal configuration, `.env`, ambient local overrides, user formatter settings or neighboring checkouts. Checked-in settings come from the same index as the code they govern.
- [ ] Keep temporary output outside candidate source inputs and dispose owned staging on success, failure, cancellation and timeout. Refuse path traversal, unsafe links and output destinations outside the owning repository. Report which snapshot/check failed without exporting private configuration or content.
- [ ] Provide an explicit CI-only Git-independent mode, provisionally `--no-git`, for a supplied clean commit snapshot. Trust the CI caller's declared commit/files; bypass index discovery only. Require all the same content, schema, security and generated-output checks without invoking Git. Missing configuration or generator inputs fail; the mode is not a fast-pass or local default.

## Explicit fixes and deterministic graph documentation

- [ ] `--fix` may apply selected safe automatic fixes to working-tree files only, leaving every change unstaged. Never auto-add, update the index or commit. Protect unrelated/unstaged edits; refuse a conflicting transformation instead of overwriting them. The hook stays read-only and fails until corrected files are deliberately staged.
- [ ] Generate main-application LangGraph charts and useful source/contract links statically, without inference, runtime/provider initialization or execution of node functions. Use staged declarations and deterministic metadata only. Generate portable source/prompt/tool links that work in the agreed local and GitHub previews; resolve cross-repository links explicitly rather than emit machine paths. For the accepted versioned resource format, use declaration-derived SVGs rendered with pinned PlantUML; the earlier renderer-choice discussion is historical.
- [ ] Keep output under its owning repository or resource pack, with explicit generated-file ownership. Public output must not contain private pack content, personal paths, credentials or local overrides. Generation cannot cross the public/private boundary merely to resolve a link.
- [ ] Compare deterministic HTML/companion/assets and SVG source metadata/integrity against expected staged filenames, including additions/deletions/renames and orphaned charts. Missing, stale or extra owned output fails. Generation/fixes leave corrections unstaged; source commits still require refreshed diagrams staged alongside them.
- [ ] Preserve executable Markdown's four-space nesting and padded ordered markers. A generic formatter must prove it preserves these contracts or exclude those declarations with a dedicated syntax check. Do not regenerate a different graph just to make a formatter pass.

## Linked rendered charts and freshness

- [ ] Consider linking the last generated SVG/image from the executable Markdown, alongside the authoritative declaration and source/prompt/tool link index. Record its source fingerprint and renderer provenance; explain that it can become stale between source edits and regeneration. Do not present a stale chart as current execution evidence.
- [ ] For PlantUML SVG, check canonical declaration/resource fingerprints, embedded projected source and pinned generator provenance against current markup. Validate saved SVG safety and graphic integrity separately. Do not regenerate or compare font-dependent geometry during checking; [portable diagram freshness](implementation_plan_b1a1.md) owns this contract. Reject missing/mismatched source metadata, unsafe SVG and corrupted graphics.
- [ ] Managed viewing checks saved chart freshness without writes. Other ordinary launch modes do not implicitly generate diagrams. Explicit generation remains unstaged, bounded and preserves concurrent edits. Hook installation runs no checks; pre-commit evaluates the index, so newer working-tree charts cannot excuse stale staged output.
- [ ] Verify clickable navigation in both approved previews. SVG embedded as an image may not expose its internal links; retain companion Markdown links or a link opening the standalone SVG. Keep executable source and generated metadata within the owning repository's privacy boundary.

## Versioned workflow resource enforcement

The selected future format is [B1a](implementation_plan_b1a.md). Earlier renderer-choice items above record the original decision history; implementation must use the accepted pinned PlantUML SVG contract for this format.

- [ ] Check the complete staged snapshot for strict root, declaration and resource placement. Use an explicit reviewed root allowlist including `README.md`, `flow.md.html`, existing `AGENTS.md`, application launcher/license and required package, pack-manifest and Git configuration files. Preserve required source, test, documentation and asset directories; layout checking must not delete or rewrite agent instructions. Declarations belong under `workflows/`; prerequisites under `docs/`, source bindings under `cfg/`, tooling under `tools/`, and owned diagrams/viewer assets under `.private_resources/`. Reject misplaced or extra generated resources and every tracked `.puml` file.
- [ ] Derive SVG paths from staged declarations with nested mirroring; reject missing, stale, orphaned or misplaced outputs. Compare markup/projected PlantUML metadata and saved SVG safety/integrity, including companion navigation, without renderer execution. Explicit generation and fixes leave changes unstaged.
- [ ] Preserve identical repository-owned rules in the existing managed hook and CI pipeline, with CI checking a supplied clean snapshot. Check mode is read-only and invokes no browser, provider, graph, network service or application startup. Verify partial staging, add/delete/rename, stale metadata, orphan output, temporary-file disposal and path/symlink rejection with negative cases.

## Quick-check choices and operating limits

- [ ] Select pinned rules, file scope, supported versions and bounded budgets with the user. Candidates are [Ruff lint](https://docs.astral.sh/ruff/linter/) and [formatter check](https://docs.astral.sh/ruff/formatter/), targeted [mypy](https://mypy.readthedocs.io/en/stable/getting_started.html) or [Pyright](https://github.com/microsoft/pyright), applicable [ESLint](https://eslint.org/docs/latest/use/command-line-interface)/[Prettier](https://prettier.io/docs/cli), schema/link/syntax checks and inexpensive security checks. Document explicit exclusions rather than silently skipping applicable content.
- [ ] Use one shared check pipeline locally and in CI, with clear failures and cancellable deadlines. Keep it quick: no complete test suite, browser, inference, live provider, network/service startup or application execution. Deterministic generator validation is distinct from runtime integration testing.
- [ ] Document check, fix and generation commands, restoration/re-staging steps and no-Git CI responsibilities. Explain rendered chart behavior using [GitHub's diagram guidance](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/creating-diagrams); GitHub supports native Mermaid, but editor styling support does not prove diagram rendering. Verify both selected editor and GitHub viewing targets before adding renderer dependencies. The selected workflow renderer is pinned PlantUML and its viewer entry is `./launch.py --view-flow`; other quick-check command names remain implementation decisions.

## Integration/E2E classification

- [ ] Enforce [explicit feature metadata](implementation_plan_d4.md) against the complete staged snapshot and clean CI snapshot. Share one static rule, preserve provider/pack scope, verify inheritance/parameterization and reject missing or unprovable metadata without importing tests.

## Acceptance

- [ ] Prove partial staging, unstaged formatter configuration and untracked graph poisoning cannot change the result for a fixed index.
- [ ] Detect forgotten staging of automatic fixes, new charts, stale charts and orphan removals; verify idempotent generation and meaningful freshness mutations.
- [ ] Verify read-only defaults, explicit safe fixes, preservation of concurrent edits, existing/custom hooks, shared hook paths and non-Git launcher status.
- [ ] Verify clean-snapshot CI parity without a Git executable, including negative schema/security/freshness cases, cancellation and deadline disposal.
- [ ] Independently review ownership, privacy, scope and the selected budgets/tools. Human-review hook assurance, staged correction guidance and generated documentation before claiming delivery.

## First-launch dispatcher decision

- [ ] Deliver the [stable first-launch dispatcher and staged diagram checks](implementation_plan_i1a.md) with the viewer. Its newer launch-installation decision supersedes the earlier interactive-only restriction; broader quick-check choices and CI wiring remain pending.
