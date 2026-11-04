# Ordered resource-pack composition

## Requirements

- [x] Accept repeatable `--resource-pack-dir PATH` in order while retaining single-pack and environment selection.
- [x] Compose typed pack declarations and defaults in order through the actual launcher. Preserve explicit CLI/environment precedence and resolve declared path/provider defaults against their contributing owner.
- [x] Preserve earlier provider declarations, test suites, production coverage roots, dotenv files and infrastructure with their original owners. Canonical pytest collection/execution use every admitted owner root and `src` only in child environments; earlier implicit `src` roots remain measured. Integration workers use the same ordered child source admission, proven with three pack layers. Home-relative path/provider defaults expand before owner rooting, including conditional defaults.
- [x] Require `[composition] version=1` and `replace=["ui", "api"]` when later layers redeclare those providers. Select implementations through existing typed declarations or `[defaults]`; no demo pack is implicitly inserted for external selections.
- [x] Preserve one shared workflow and tool catalog across later layers. A competing workflow requires explicit `replace=["workflow"]` and uses existing workflow/library/replacement contracts. Reject multiple tool catalogs and distinct workflow/catalog owners rather than silently discard resources or invent compiler roots.
- [x] Preserve the selected identity adapter's pack ownership when later packs omit identity declarations; a later explicit identity declaration selects that pack's adapter.
- [x] Prove ordinary unit collection and execution in a freshly prepared composed environment. Development dependencies cover SDK imports used by the unit suite independently of the selected runtime provider; runtime-only installations retain optional provider extras.
- [x] Complete repository gates and independent review before integration.
- [ ] Perform attended acceptance with real services separately.

## Proof and limits

Synthetic pack units exercise ordered defaults, provider selection, rooted paths, suites and resource retention, plus actual workflow definition loading. Invalid manifests and competing responsibility ownership fail before service startup. Existing compiler and tool lifecycle contracts remain authoritative.

This PoC intentionally supports one workflow/tool catalog owner. A future catalog union or cross-pack graph replacement protocol needs explicit ownership semantics and separate requirements. No live identity or inference acceptance is implied by controlled proofs.

Auxiliary `[providers].configs` declarations currently have one consumer owner in
provider bootstrap. An earlier layer's auxiliary relative path/selector defaults,
including conditional defaults, are rejected before startup when ownership would
be ambiguous. Use absolute auxiliary defaults or move those defaults into the
owning pack's main configuration. A per-auxiliary-owner bootstrap protocol remains
an explicit future interface change.

The broader [ordered composition contract](implementation_plan_m4.md) retains
open declaration-admission, relaunch integrity, multi-pack dotenv precedence and
write-authority requirements. The completed PoC requirements above do not close
those additional gates.
