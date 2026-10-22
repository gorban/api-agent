# Generic host-process prerequisite extension

[Follow-through](implementation_plan_o.md) · [Delivered Compose foundation](implementation_plan_o1.md)

This future extension is outside bounded Compose v1. Existing UI process ownership remains separate.

- [ ] Specify generic host-process prerequisites separately if needed: executable/argv/cwd ownership, captured assets, identity/readiness, logs, bounded cancellation and confirmed disposal. Until that contract is implemented, reject such contributions rather than treating the UI owner as a universal process runner.
