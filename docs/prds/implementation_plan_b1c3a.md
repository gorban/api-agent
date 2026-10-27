# Scoped executable declaration and setting identities

[Historical source identity](implementation_plan_b1c3.md)

Exact captured source provenance and executable comparison identity serve
separate purposes. Historical rendering must retain the original documents and
owning commits; statistical test calibration is not an identity service.

- [x] Preserve existing exact definition/resource/source-manifest identities and
  their native-admission, accounting and reporting checks. Removing workflow
  calibration must not weaken committed-source verification or historical joins.
- [x] Add a versioned, explicitly scoped executable-declaration identity from the
  captured parsed hierarchy, ordered steps/edges/prompts, schemas, native binding
  resources and selected replacements. Presentation wrappers, generated diagrams, source line
  positions and machine-specific paths must not define this comparison identity.
- [x] Include only configuration declarations referenced by the parsed graph,
  rather than unrelated settings in a shared TOML resource. Separately identify
  resolved declarative timeout/pass values actually used by the invocation.
  Honor normal overrides and distinguish definitions from effective settings.
- [x] Preserve exact whole-source hashes for audit even when scoped identities
  remain equal. Do not claim that a declaration-only hash proves equivalence of
  arbitrary native-code configuration reads, provider behavior or dependencies.
- [x] Record the new identities through shared graph evidence for all parsed
  executors as integrity-bound diagnostic metadata. Preserve trusted runner
  admission unchanged, and expose an optional comparison against an independently
  expected descriptor. Do not treat producer metadata alone as trusted semantic
  admission. Keep older recorded envelopes readable;
  do not silently reinterpret or replace their existing identities.
- [ ] Prove stability under relocation, presentation and unused-config edits;
  sensitivity to referenced defaults/effective values, executable declarations,
  prompts, schemas, native bindings and replacement selection; reject malformed
  descriptors, reject tampering and mismatches against an independently expected
  descriptor, and preserve privacy and capture/admission equality. Self-consistent
  producer metadata alone is not trusted semantic admission.
- [ ] Complete independent/external review, exact applicable unit coverage,
  source/installed/relocated provenance and public-boundary checks.
- [ ] Human review confirms graph identity and effective-setting evidence.

The shared runtime binds fresh journals, including application-created observation
journals, to the invocation's validated timeout and pass maps before events are
retained. An existing descriptor must agree; adding or changing a descriptor
after retention starts is rejected. Historical envelopes without the optional
field remain readable and are never assigned a reconstructed identity by readers.

Selected-field configuration trimming applies only when budgets are the file's
sole consuming role. Dual-use prompt, schema or native bindings retain exact
bytes; explicitly declared opaque v3 configuration resources also retain their
whole-byte hash, including unrelated fields. This scope does not infer arbitrary
native configuration dependencies or establish native/provider equivalence.
