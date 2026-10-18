# Resource-pack integration guide

[Documentation](implementation_plan_c.md) · [Provider boundaries](../architecture/provider_architecture.md)

The README currently links a short resource-pack overview. Deliver a practical,
standalone authoring guide without turning the shared repository into an
application template or duplicating executable configuration inventories.

- [ ] Add a concise README explanation: api-agent includes a batteries-included
  sample agent; applications extend the runtime through a separately owned
  resource pack instead of copying and modifying the shared repository. List
  workflow/nodes/tools, LLM, UI, API/upstream access, credentials and configuration
  extension points, linking to the comprehensive guide.
- [ ] Expand [resource packs and providers](../resource-packs.md) as the canonical
  integration entry point. Link focused contract and worked-example sections
  where needed; retain existing incoming links and avoid competing guides.
- [ ] Complete the [interface and behavior inventory](implementation_plan_c1a.md)
  and [bootstrap, configuration and example](implementation_plan_c1b.md).
- [ ] Link the actual bundled `flow.md.html` and explain selection of a custom
  graph, node additions/replacements and tools after the declarative workflow
  conversion is delivered. Preserve root entry and nested resources; do not
  publish a broken link or describe planned grammar as already supported.
- [ ] Explain the current red `Bootstrap: --resource-pack-dir PATH or
  RESOURCE_PACK_DIR selects a resource pack.` menu guidance. It is a pack-selection
  hint shown for the implicit bundled demo, not a fatal bootstrap error or proof
  that the sample cannot run. Separate genuine missing/invalid pack diagnostics.
- [ ] Verify the examples against actual public contracts, source and installed
  resources, then obtain dedicated documentation human acceptance. Reconcile
  pending local-run, tool-hosting and workflow capabilities before claiming them.

Keep api-agent-owned material under its existing David Bruck copyright/license;
independently authored external packs/providers retain their owners' notices and
chosen licenses. Preserve bundled third-party licenses and explain applicable
redistribution obligations rather than attributing every bundled file to one
author. Keeping the shared checkout intact makes upstream fixes easier to adopt.
