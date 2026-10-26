# Scoped execution evidence and custom diagnostics

[Complete flow-aligned evidence](implementation_plan_b1c1a2.md) · [Custom evidence guide](../testing_evidence.md)

Keep diagnostic evidence available through progressive disclosure at its actual
execution owner. Attachment placement is a projection of recorded identities;
it must not change execution, accounting, coverage qualification or privacy.

- [x] Reference each canonical execution journal once beneath its matching GRAPH
  invocation, reusing the manifest-bound file and preserving historical hydration.
- [x] Place existing per-run structural diagnostics beneath their uniquely matched
  graph scope. Place cumulative coverage progress beneath its observed Trial;
  retain a clearly named final Coverage summary at test level.
- [x] Provide a shared, documented JSON attachment interface for test, graph,
  component/node visit, call or trial ownership. Resolve explicit identities within
  the current test after event collection, independent of receipt order. Do not
  infer ownership from names, timestamps or an unrelated graph's proximity.
- [x] Preserve evidence with absent or ambiguous owners as explicitly Unattached
  at test level. Graph-free calls remain inspectable without inventing a graph.
  Existing unscoped and legacy evidence remains readable.
- [x] Apply LOG_CONTENT and unconditional credential/hidden-content redaction
  before custom evidence crosses the worker boundary and again at recording.
  TRACELOOP_TRACE_CONTENT does not authorize Allure content. Bound JSON values
  and reject malformed ownership without arbitrary object conversion or paths.
- [x] Preserve attachment references through conversation/turn/trial regrouping,
  final accounting and manifest publication. Store payloads once; never manufacture
  execution steps, change outcomes or duplicate cost facts for custom JSON fields.

## Acceptance

- [x] Deterministic producer/IPC/recording tests cover multiple invocations and
  trials, recursive visits, call-only/no-GRAPH cases, early/late receipt order,
  unknown/ambiguous owners, single-copy references, hydration and independent flags.
- [x] Applicable unit tests pass with exact line/branch coverage; installed and
  relocated artifacts, public boundaries and unchanged sampling fingerprints pass.
  Independent and external reviews close reported findings.
- [ ] Human review confirms per-run evidence is available beneath GRAPH, cumulative
  progress beneath Trial, and only relevant test-wide summaries remain at top level.

**Deferred human review**

Implementation and required automated, independent and external reviews are complete. Only explicit human acceptance above remains unchecked.
