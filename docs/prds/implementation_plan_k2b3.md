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

The earlier implementation, causal tests, exact unit coverage, package/installed
native verification and independent/external reviews were completed. The human
retest above remains unchecked; earlier core and cleanup approval stay accepted.

## Local output selection follow-up

- [x] Support ordinary mouse selection and double-click selection in the header, startup error and streamed logs. Ctrl+C must copy the exact currently selected text before considering focused menu buttons; never display a quit prompt in place of copying. Retain existing browser/log choice copying and preserve selection across unchanged status and log updates. When selections move between output and header, the latest selection is authoritative in either direction.
- [x] Keep the bounded streaming text area, scroll and retention behavior, and cleanup ownership. When nothing is selected or copyable, retain the application's normal Ctrl+C fallback. Reject hidden, cleared or stale selections and preserve copy failure feedback without losing the selection.
- [x] Verify real Textual mouse selection and keyboard dispatch, existing log/menu and shutdown behavior, causal regressions, applicable coverage and independent review.
- [x] Complete the merged external recheck.
- [x] Human accepted local output selection/copying on October 30, 2026, covering header, log and startup-error selection, latest-selection precedence, refresh retention and Ctrl+C copying without Quit.

Focused mouse and keyboard checks cover all three surfaces, both selection directions, appended logs, changed or hidden header selections, menu copying and shutdown. Isolated in-memory mutations show the regressions detect disabled double-click, header copy, selection precedence and append preservation. The full applicable unit suite passed with exact line and branch coverage; source, installed, boundary and independent causal reviews also passed. The merged external recheck passed; human acceptance of this output-selection follow-up is complete. The separate ordinary-startup retest above remains scoped independently.

## Selected macOS context and Podman compatibility

- [x] Without an explicit transport on macOS, resolve the stored current Docker context's
  endpoint using a bounded read-only command. Require a local Unix socket, verify
  its trusted ownership and engine identity, and pin the same canonical transport
  for subsequent CLI, planning and acquisition. Never switch contexts or start
  another engine. Explicit transport retains precedence; missing, remote or invalid
  context evidence fails with actionable guidance.

- [x] Verify the Docker CLI against Podman's Docker-compatible API even when its
  per-response ID changes. Require both version responses to identify Podman with
  the same nonempty version before using the existing corroborated Unix namespace.
- [x] Retain trusted socket generation, matching stable host/store values and
  nonempty immutable inventory checks. Native Docker identity mismatches, unknown
  servers, malformed evidence and differing inventories still reject. Verification
  performs read-only requests and never creates or adopts a service.
- [x] Display each startup phase once in terminal mode, including agent/all log
  selections, while retaining the phase in interactive logs and status.
- [x] Verify regressions, causal controls, applicable coverage, installed behavior
  and independent/external review before delivery.
- [ ] Human retest ordinary Run local startup on the affected installation.

Environment-selected contexts remain separate follow-up work:

- [ ] Carry `DOCKER_CONTEXT` through controlled inspection on supported platforms,
  honor its documented precedence, then remove it after pinning the verified Unix
  transport. Verify conflicting saved/environment contexts and endpoint overrides.
  This checkpoint supports the stored current macOS context and explicit transport;
  it does not qualify an environment-selected context.
