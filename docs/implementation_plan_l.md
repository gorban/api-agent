# Container inventory and cleanup

## Completed inventory slice

- [x] Implement live host inventory/count, selected stop confirmation, bounded engine calls, error handling and refresh. No containers were stopped during implementation. Ownership/profile integration and live human review remain below.

## Checklist

- [ ] Add interactive `cleanup-containers` mode and a main-menu running-container count. Query the selected host engine freshly on menu entry/refresh; show unavailable/error honestly if inventory fails. Collection/help must not start or stop containers.
- [ ] List running container names, identities, images, and status with selection controls. Clearly confirm selected container names before stopping them; only an explicit affirmative response executes cleanup. A cancelled confirmation changes nothing. Retained test tracers and containers left after forcefully killed TUI processes must remain discoverable.
- [ ] Keep engine operations bounded and argument-safe, report individual failures, and refresh the count/list after cleanup. Stop selected containers without automatically deleting persisted trace data or volumes. Define any later remove/volume cleanup as a separate explicitly confirmed operation.
- [ ] Add stable launch ownership, checkout/resource-pack, profile and reuse labels to all future prerequisite stacks. Let the inventory distinguish current-stack containers from other host containers. Never automatically stop an unrelated or concurrently owned service; use selected explicit cleanup when needed.
- [ ] Integrate [tracer retention and reuse](implementation_plan_k.md) with cleanup before the Ok milestone. Add deterministic tests for empty inventory, engine failure, checkbox selection, confirmation/refusal, refresh, stop failure, and crashed/retained stacks. Live cleanup is human-reviewed.
