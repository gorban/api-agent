# Managed workflow viewer lifetime and SVG fidelity

[Workflow resources and views](implementation_plan_b1a.md) · [Direct source navigation](implementation_plan_b1a5.md)

This repair reopens managed viewer lifetime, interrupt cleanup and browser rendering after concrete interactive failures. Existing declaration/execution, source-navigation and qualification closures remain valid for their earlier scope.

- [x] Keep the finite snapshot-backed listener available throughout the managed inspection lifetime. Refresh and navigation from the original page and a separate browser must continue after startup budgets have elapsed. Accepted requests use their own bounded timeout and progress scope; a completed startup operation cannot become their parent deadline. Preserve existing startup and per-request limits and the immutable route inventory.
- [x] One normal foreground Ctrl+C must finish owned viewer/browser/driver/listener disposal and return interruption status without requiring a second signal or indefinitely repeating pending-cleanup/closed-pipe messages. Preserve retryable ownership and truthful diagnostics for genuinely unsettled resources; do not hide errors globally, discard retained receipts or claim disposal solely because the page disappeared.
- [x] Preserve the pinned generated SVG's visual styles in standalone and embedded browser views served by the managed host, including node borders, arrows/dashes, labels, fills and geometry. Admit only validated captured styling under a bounded content-security policy; retain script, resource and network restrictions. Do not disable the policy or add unrestricted inline styling.
- [x] Verify timeout, schema and step navigation from the real managed document and standalone SVG, including correct raw-source bytes and local rebasing. Preserve equivalent saved diagrams and source/package immutability.
- [x] Reproduce the reported failures before repair. Use controlled deadline/ownership/CSP mutations and real Chrome-compatible browser and process/HTTP proofs through the actual launcher: delayed refresh beyond startup, original and separate browser access, one foreground SIGINT, natural closure, interrupted acquisition and active-client teardown. Confirm ports close, request tasks drain and owned browser/driver processes terminate. Keep failed attempts and diagnosis.
- [x] Renew applicable exact unit line/branch coverage, installed/relocated artifact and license checks, source/index/metadata audits, independent adversarial review and external joined-source/delivery review before promotion. Viewing requires no inference, provider authentication or graph execution.
- [ ] Human acceptance of managed refresh/navigation, undistorted SVGs and one-Ctrl+C cleanup within the combined D5 checkpoint.

Technical repair and exact-source delivery are independently and externally reviewed. Human acceptance remains open.
