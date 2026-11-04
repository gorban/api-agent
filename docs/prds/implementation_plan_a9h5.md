# Unit concurrency qualification

[Platform applicability](implementation_plan_a9h4.md)

- [x] Qualify a conservative bundled unit-worker default against unchanged isolated validation and evidence deadlines. Use 16 workers instead of the previous fixed 32; keep the global test-worker default at eight. Resource packs may declare a different unit default through existing configuration overrides. Preserve explicit overrides, empty inheritance from TEST_PARALLEL_JOBS, serial zero, and integration/E2E settings.
- [x] Compare bounded real-worker workloads at different concurrency with identical host CPU availability and payloads. Distinguish resource-unavailable validation from invalid schema semantics and retain strict unavailable rejection. Do not claim this establishes every native failure or a universal capacity bound.
- [x] Repair independently reproduced fixture ownership/readiness races without changing production deadlines or ownership defenses: [fixture prerequisites](implementation_plan_a9h6.md).
- [x] Complete full unit coverage, independent adversarial and external reviews, targeted causal controls and public-boundary checks before publication.
- [ ] Human acceptance of the normal unit command on macOS. Explicitly saved worker counts remain user choices.

Verification: 26,816 Linux unit tests pass at 16 workers with exact 100% line and branch coverage. Controlled identical validation workloads pass all 192 calls at 16 and eight workers; 32 workers exhaust the unchanged worker limits. Independent adversarial and external source reviews pass. This does not establish native acceptance.
