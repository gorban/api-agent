# Stable diagram provenance and reproducible rendering

[Workflow resources and views](implementation_plan_b1a.md) · [Markup-based freshness](implementation_plan_b1a1.md) · [Staged checks](implementation_plan_i1.md)

Read-only freshness and reproducible rendering are different contracts, and the generator currently conflates them.

## Why

- **Freshness is already portable.** The read-only checker compares canonical declaration, resource and configuration inputs, the projected PlantUML and viewer assets with the metadata saved in each SVG, and verifies the saved graphic's own integrity digest. It never rerenders, so host fonts cannot make a valid saved diagram fail. A checkout moved between macOS and Linux checks cleanly when its inputs are unchanged.
- **Generation is not incremental.** Explicit generation rebuilds the inventory without verification and rerenders every admitted diagram. PlantUML lays text out with the host's font metrics, so each host produces different coordinates for unchanged diagrams. In one observed regeneration, 25 SVGs were rewritten; 23 had identical input metadata and differed only in their graphic digest. Only two had a changed input. That churn hides real changes in review and makes commits from different machines conflict needlessly.
- **Rendering has no pinned environment.** The output depends on the ambient JDK, font set, font configuration, locale and platform. Two machines with the same sources cannot be expected to produce the same bytes, and an intentional renderer or font upgrade is indistinguishable from accidental host drift.
- **Freshness is not enforced everywhere it matters.** A declared input can change without its diagrams being regenerated (for example, editing a node's native source module), leaving committed diagrams stale until someone runs the checker by hand.

## Requirements

### 1. Portable checks and incremental generation

- [ ] Prove that unchanged admitted inputs pass the read-only check across checkout roots, macOS/Linux, source and installed layouts and library/replacement selections, without starting Java, a container engine or the renderer. Normalize only genuinely machine-specific identities; keep canonical logical owner and path distinctions. Reproduce any suspected absolute-path dependency before changing it.
- [ ] Keep semantic freshness separate from the digest of the exact saved bytes. Preserve graphic integrity, safe SVG and link validation, asset provenance, source admission and staged-snapshot protection. Do not weaken or drop input hashes to silence churn.
- [ ] Default generation reuses a saved diagram only after that same complete freshness and integrity validation passes, and renders only missing or stale diagrams. A no-op run changes no bytes and makes no renderer call. Keep transactional writes, owned-output and orphan checks, cancellation and concurrent-change guards. A failed integrity check never authorizes overwriting an unowned or tampered file.
- [ ] Provide an explicit whole-set rerender for a reviewed renderer or font upgrade. Moving a checkout to another host never triggers one implicitly.
- [ ] Freshness failures name the affected document and the changed category (declaration, resource, configuration, projection, viewer asset, renderer contract or graphic integrity) without dumping full SVGs or projections.

### 2. One canonical rendering environment

- [ ] Define a rendering contract that pins the base image digest, target platform, JDK, MIT PlantUML JAR and checksum, freely redistributable fonts and font configuration, locale, layout flags and other relevant environment. Record the contract in saved metadata so an intentional change is visible and reviewable. Carry font and tool licenses.
- [ ] Choose one canonical platform, or demonstrate byte-identical output across every supported platform; a shared multi-platform tag alone is not proof.
- [ ] Build the toolchain image dependency-first: OS packages, fonts, JDK and JAR in early cached layers, with only their manifests copied for those steps. Copy changing application code last, or mount admitted source read-only with separately owned output, so an application edit reuses every toolchain layer. Keep the build context free of secrets, virtual environments, `node_modules` and retained test artifacts.
- [ ] Local explicit generation runs in that environment through the existing container-engine selection. CI may run directly in the prepared image or bake its pinned tools into an existing task image; neither path needs nested containers just to run Java. Resolve Java to the prepared executable, not a shim that depends on the ambient `HOME` or working directory.
- [ ] The read-only check stays renderer-independent and needs none of this toolchain. Production runtimes never ship it.

### 3. Explicit read-only checks in every owner's CI

- [ ] Each repository that owns saved diagrams runs the shared read-only check in CI with its selected framework, library and pack roots, including packs that import another owner's base hierarchy. A stale or tampered diagram fails the build.
- [ ] Keep the check separate from compiled-plan checks, cheap and unprivileged: no generation, downloads, source edits, commits, renderer or container daemon.
- [ ] A canonical full-regeneration comparison, if added, is a separately identifiable job that reports differences without repairing them. Measure its cost before making it run on every commit.

## Acceptance

- [ ] An unchanged multi-repository tree moved between macOS and Linux and back passes the read-only check each time, with no writes and no renderer invocation.
- [ ] Default generation run twice against a fresh set, including from hosts with different fonts, invokes no renderer and leaves the worktree clean.
- [ ] Changing one declared input identifies the affected provenance; generation rerenders only the affected diagrams and preserves every unaffected one byte for byte.
- [ ] Canonical forced rendering produces identical bytes on repeated runs and from macOS and Linux hosts using the selected image and platform, covering Unicode, long labels, nested components, imported roots and links.
- [ ] Tampering with graphic bytes, hashes, links, provenance, selected resources or viewer assets fails the relevant check; focused mutations prove the specific ownership and integrity checks rather than a secondary rejection.
- [ ] Each owner's CI check passes on a prepared source snapshot with renderer and network tools unavailable, and fails on a stale input without starting a container daemon or Java.
- [ ] A warm toolchain image rebuild after a source-only edit repeats no OS, font, JDK or JAR installation; a toolchain change invalidates the intended layers and the recorded rendering contract.
- [ ] Unit and coverage gates, independent adversarial review and external review pass. No paid inference or secret-dependent end-to-end run is required.
