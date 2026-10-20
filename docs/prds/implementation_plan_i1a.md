# First-launch dispatcher and staged diagram checks

[Managed checks](implementation_plan_i1.md) · [Workflow views](implementation_plan_b1a.md)


- [x] On any launcher invocation from a Git checkout, assure the missing managed pre-commit dispatcher once, including noninteractive modes. This supersedes the earlier interactive-only installation restriction; installation itself runs no checks, generators, tests, browsers, authentication or services. Skip without failing normal launch when Git or repository metadata is absent, including clean CI/archive use.
- [x] Install a stable dispatcher to one source-controlled omnibus Python check entrypoint. Future checks change that tracked entrypoint, not the hook; an existing hook needs no routine version rewrite. Preserve foreign hooks and custom/shared hook locations, never overwrite them, and explain manual integration when managed assurance is unavailable.
- [x] Bring forward declaration/diagram freshness and the minimal staged-snapshot runner with this viewer delivery. Keep proposed unrelated lint/type/security tool choices and full CI wiring separately pending. A fixed index must give the same result despite unstaged checker/configuration changes; installed dispatch must not execute an unstaged replacement runner to bypass checks.
- [x] Before managed viewer display, validate the selected saved snapshot. Hook checks validate indexed markup/source provenance, SVG safety/integrity and companion links without Java or regeneration; platform font geometry is not freshness. Explicit generation leaves corrections unstaged; newer working-tree charts cannot repair stale indexed output. Direct file opening shows the last generated artifact. Production execution requires none of these tools.
- [x] Bound index entries, individual and total staged bytes, checker output and aggregate elapsed work. Dispose of owned process groups on failure, including descendants whose leader already exited; preserve actionable bounded diagnostics.

## Verification and human acceptance

The source implementation and repairs passed independent code review, the complete applicable unit gate at exact line/branch coverage, actual renderer/browser checks and relocated installed-package verification. Ordinary freshness checks require no renderer. Broader application startup, execution overlays and full CI wiring retain their own requirements.

- [ ] Human acceptance alongside the combined declaration/diagram and managed-viewer checkpoint.
