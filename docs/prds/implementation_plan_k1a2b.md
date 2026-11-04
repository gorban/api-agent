# Captured tracing collectors for Studio and tests

[Tracing providers](implementation_plan_k1a2.md)

The normal Studio/test protected launch snapshot must preserve selected custom
collector inputs before the worker begins. Reuse the existing `trace_collector_plan`
field, infrastructure parser/planner, inventory authority and lease manager. Do
not serialize executable plans, duplicate configuration values or create another
scheduler or resource owner.

- [ ] Capture owner-bound descriptor, selected infrastructure declaration/compose/
  asset bytes and digests, activation field-type projection, inventory authority
  and four controlled baseline assets in a bounded version-2 receipt.
- [ ] Restore solely from explicit bytes, with strict schema, digest, size and owner
  checks; missing captured assets never fall back to live files or neighbors.
  Bind the admitted factory descriptor to the receipt before effects.
- [ ] Acquire only the descriptor's recursive selected closure. Remote empty
  selection records its inert descriptor identity and allocates no engine or
  schema. Preserve built-in version-1 compatibility and existing lifecycle order.
- [ ] Prove actual normal Studio snapshot/startup ordering, source replacement,
  tampered/missing inputs, no credential values in receipts, lazy factories and
  selected service isolation. Complete exact applicable coverage, causal controls,
  source/install checks and independent review before acceptance.

The runtime consumer owns optional tracing failure policy and retained cleanup;
this receipt does not authorize live authentication or waive final integration
and human backend acceptance.

Credential exclusion applies to the complete serialized receipt: inventory-only
declarations, field names, owner/context metadata and decoded captured assets as
well as selected service inputs. Reuse shared schema-derived credential authority
and credential detection. Reject before handoff if exclusion would change required
source bytes; preserve exact TOML/digests and needed inventory. Do not silently
sanitize sources, omit authority, inspect unrelated files or log secret values.

Credential screening uses the shared scalar response scrubber on original strings without invented wrapper text, so absent short or structured credentials cannot collide with screening carriers. Actual packet and decoded-source literal collisions remain rejected.

## Runtime consumers

- [ ] The protected launch writer treats collector capture failure as optional:
  publish a fixed diagnostic and an explicit empty invalid receipt in the
  existing field. Workers reject that receipt before manager/SDK effects; a
  failed managed capture cannot silently become a remote selection.
- [ ] Normal Studio binds its admitted tracing descriptor to the protected
  receipt before manager construction, acquisition or exporter startup. Missing,
  tampered and replaced descriptor inputs disable tracing with a safe diagnostic.
  Captured infrastructure bytes remain authoritative after live source replacement.
- [ ] Shared setup preserves inference on optional collector planning or tracing
  admission failure. Genuine cancellation and diagnostic failure propagate.
  Disabled tracing allocates no collector or exporter under any content flags.
- [ ] Exercise the actual default resolver, protected writer/reader, selected
  custom SDK, shared setup transport and normal Studio HTTP/session consumers.
  Verify selected closure isolation, remote no-effects behavior, independent
  content flags and exporter disposal before collector release. Retained cleanup
  and evidence finalization contracts remain unchanged.
