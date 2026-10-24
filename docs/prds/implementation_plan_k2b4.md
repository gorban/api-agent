# Two-column Run local menus

[Local startup and terminal](implementation_plan_k2b.md)

- [x] Present Log sources and Browser destinations as two columns, retaining vertical scrolling for resource-pack contributions rather than imposing a source count limit.
- [x] Make browser buttons one line taller than log-source buttons and show the destination name and actual URL on separate content lines. Long names must not wrap over the URL line; narrow terminals may shorten displayed text, while Ctrl+C copies the full URL.
- [x] Keep Enter, the first ten digit shortcuts, arrow/Vim spatial navigation, All logs, focus reconciliation and selection copying working in both menus.
- [x] Verify real widget geometry, constrained-height scrolling and existing selection/cleanup behavior; close independent and external review before delivery.
- [ ] Human check both menus in an ordinary terminal and confirm the destination URL and copying.

**Deferred human review**

Implementation, layout regressions, full unit/coverage and package checks,
independent review and external review are complete. The terminal check above
remains unchecked; earlier approvals remain accepted.

## Production stylesheet correction

- [ ] Preserve two visible destination content rows in the production app,
  including its global button styles. Reserve at least one character of left
  padding before the shortcut and name; retain the URL on the next row.
- [ ] Verify ordinary and compact production-app layouts, rendered URL text,
  final CDP destination, scrolling and full-value Ctrl+C copying. Complete
  required unit coverage and independent external review.
- [ ] Human acceptance confirms the corrected padding and visible URL row.
