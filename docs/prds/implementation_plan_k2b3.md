# Default local engine binding and copyable startup errors

[Local startup and shutdown](implementation_plan_k2b.md)

- [x] When active conditional infrastructure lacks an explicit engine transport, verify the existing supported default Unix engine through the current trusted-socket and CLI/direct-API identity checks, then pin that verified transport before planning and acquisition. Keep the planner side-effect-free and strict.
- [x] Preserve explicit transport selection and rejection of conflicting, malformed, remote or ambiguous engines. Do not query an engine merely because dormant infrastructure declarations exist. Keep bounded verification and cancellation settlement; readiness grants no ownership by itself.
- [x] Carry the selected transport consistently through the plan, operation, prerequisite manager and reporting identity. Prove normal local startup without a test-only engine override, including installed native execution and shutdown.
- [x] Keep the startup error below the shortcuts, make it selectable and copy its focused selection with Ctrl+C. Preserve existing log/menu copying, selection during status refresh and rejection of cleared or stale selections. Keep empty errors unobtrusive and long errors readable.
- [x] Close causal regressions, exact applicable unit coverage, independent review, package/installed/native and public-boundary checks, then external review before delivery.
- [ ] Human retest: start Run local with the ordinary environment and bundled local API, confirm startup progresses beyond infrastructure planning, and select/copy an error with Ctrl+C when one is displayed.

The confirmed failure was the missing explicit Unix transport at conditional
infrastructure admission. The previous native proof supplied a transport itself;
this follow-through must exercise the ordinary default-selection path instead.

**Deferred human review**

Substantive implementation, causal tests, exact unit coverage, package/installed
native verification and independent/external reviews are complete. The human
retest above remains unchecked; earlier core and cleanup approval stay accepted.
