# Workflow capture defaults

[Tool evidence](implementation_plan_o4h1.md)

- [x] Bind framework-owned workflow capture at every managed integration/E2E
  entry point without a per-test workflow marker. With no explicit manifest,
  enroll standard UI/tool fixture owners and the automatic E2E UI; preserve closed
  explicit manifests and completeness for all credential producers, protected configuration
  provenance, independent log/trace policies and invocation-local cleanup.
- [x] Keep both workflow engines and application adapters on the shared capture
  context. Capture only declared node I/O and typed tool payloads; no arbitrary
  internal-state dumps or implicit content opt-in.
- [x] Show bounded input/output capture states for every node and tool, including
  missing phases after failures. Distinguish deliberate policy withholding from
  unavailable payload/authority. Never use raw diagnostic text as a report label.
- [x] Audit managed entry points and both engine paths; verify new flows inherit
  defaults, explicit opt-out, missing producers, safe failure labels, isolation,
  full applicable coverage and independent review.
- [ ] Human acceptance of useful payloads or clear omission explanations across
  nodes, tools and retry attempts. Existing immutable reports remain unchanged.

No per-flow capture switch is introduced. Standalone library callers still own
and supply their observation destination and credential boundary; importing or
creating a workflow does not enable logging or tracing by itself.

An explicit application capture owner continues to replace the inherited bundle;
it must provide its own complete credential boundary. Incomplete explicit owners
report unavailable rather than borrowing another owner’s inventory.

Verification: 27,475 applicable unit tests pass with exact 100% line and branch
coverage. Recorded controlled integrations pass with content enabled and disabled;
no inference calls are made. Independent review, browser inspection and four
defect-detecting mutation controls pass. The retained report shows node/tool
capture states separately and keeps rejected-attempt omissions distinct from
withholding or authority failure.
