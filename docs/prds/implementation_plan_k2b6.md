# Run local stopped inspection and output boundaries

[Local startup and shutdown](implementation_plan_k2b.md)

## Requirements

- [x] End the unavailable custom tracing viewer warning with a newline before the browser startup activity. Preserve ordinary log-source filtering and terminal output ownership.
- [x] Interactive Stop must acknowledge disposal of all owned resources while retaining the Run local page. Keep captured logs selectable and source menus and previously advertised browser destinations usable for inspection.
- [x] After disposal, replace the Stop hint with `c Close`. Within a destination/source menu, `c` returns to logs; from the stopped log view, `c` returns to the main menu without another stop confirmation. Quit retains its existing confirmation and application cleanup policy.
- [x] Preserve responsive asynchronous Stop and serialized Stop/Quit/unmount cleanup, including retained cleanup receipts and retries. Never acknowledge disposal before owned cleanup finishes.
- [x] Complete regression and causal mutation checks, independent review and applicable public-boundary checks.
- [x] Human accepts this delivered Run local follow-up without requiring another manual retest: inspect retained logs and destinations after Stop, then Close; verify the warning/browser startup line boundary.

- [x] Display a valid owned loopback HTTP Browser CDP endpoint on a separate status line and append it as the final browser destination. Open and copy it through the same explicit actions as other destinations. Preserve it for stopped inspection without exposing it through the local status HTTP endpoint.
- [x] Render destination names and URLs on separate visible lines at ordinary terminal sizes while retaining two columns and scrolling for long lists. Verify actual rendered output.

## Implementation evidence

The tracing warning producer terminates its event at the log boundary. The local
screen keeps the latest advertised browser destinations when shutdown status
snapshots omit them. Resource retirement still uses the existing shared close
task; it refreshes the retained page instead of automatically opening the menu.
The Close binding uses acknowledged disposal and the active page/menu state.

Regression coverage uses the real local controller with an owned cleanup callback
and checks the stopped page, log selection, browser opening, source selection and
Close without a confirmation. Existing delayed cleanup and concurrent Quit tests
continue to exercise responsive serialized disposal.

Focused regression verification: 156 tests passed across the local controller
and local screen suites. Five isolated causal mutations were rejected: removing
the warning newline, destination retention, retained stopped presentation,
the Close action, or the guard preventing Close before disposal. The later independent and final gates supersede this initial checkpoint.

Independent review follow-through: Stop before the deferred controller start now
settles the retained page as stopped without allowing late startup or leaving an
elapsed startup label. Actual startup failures keep their status and diagnostic.
A rejected application Quit refreshes the Close hint after local disposal for
both Quit-only and concurrent Stop/Quit. The focused screen/controller suites
passed 157 cases; the final five updated cases passed separately. Three further
causal mutations verify early-stop state, Quit refresh and startup-failure
preservation. Both findings were independently re-reviewed before the final gate.

Browser inspection follow-through: the separate Browser CDP status line and
last destination derive only from the runtime snapshot inspection endpoint.
Accept numeric loopback HTTP origins with a valid explicit port and reject
credentials, paths, query strings, fragments and whitespace. Shutdown snapshots
retain this endpoint for inspection. No local HTTP status payload was extended.
Browser button content aligns to the top left of two reserved content rows;
per-line truncation preserves the URL row even with long names.

The 89-case screen suite passed, including actual rendered URL rows and exported
screen compositor output at ordinary terminal size, long-name rendering and
scrollable two-column menus. Five isolated mutations reject missing loopback
validation, missing inspection retention, missing status/destination, and a
button too short to display its URL. Human visual acceptance remains open.

Final input review: reject leading or embedded C0 controls before URL parsing,
which otherwise strips leading controls. Fifteen endpoint validation cases pass,
including NUL/ESC and missing hostnames. Removing only the C0 guard rejects the
causal mutation through the new NUL/ESC cases.

## Human acceptance

Substantive implementation and required independent/external reviews are complete.
The final joined unit gate passes at exact line and branch coverage, with package,
installed and public-boundary checks. Explicit human acceptance is recorded
above; earlier approvals and historical failed evidence are preserved.

The explicit acceptance covers this delivered follow-up; later tool presentation and full tracing parity remain separate.
