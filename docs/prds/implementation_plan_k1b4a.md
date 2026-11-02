# Managed collector recovery diagnostics

[Optional observability availability](implementation_plan_k1b4.md)

- [x] Explain an incompatible managed LangFuse web container at the infrastructure boundary, shared by local execution, viewers and tests. Identify changed admin email/password initialization settings only when inspected container metadata proves the difference; never log their values. Keep an honest generic explanation when metadata cannot establish the specific setting.
- [x] Surround actionable recovery guidance with blank-line padding and a terminal-safe, 72-column attention border. Render the same plain text through existing terminal and TUI diagnostic paths, without presentation-specific provider rules.
- [x] Tell operators to stop all users of the selected stack, use the same container engine/context, remove only the identified stopped web container without force or volume removal, and rerun. Preserve traces and all volumes; explain that initialization settings do not rotate an existing database account. Do not perform automatic replacement or bypass ownership/lease checks.
- [x] Accept Textual's no-count stderr writer only when an owned diagnostic sink has retained the scrubbed notice. Preserve real short writes, write/flush errors and unavailable required-diagnostic failures; do not turn optional collector failure into authentication failure because of a writer return convention.
- [x] Verify both changed settings, unknown/malformed metadata, unrelated incompatibilities, credential non-disclosure, retained resources, real Textual capture and causal rejection controls. Complete applicable coverage, boundary and external review gates.
- [x] Human acceptance on November 2, 2026: the recovery notice appeared with and without `--no-std-in`; E2E passed with `Continuing without tracing.` Removing the identified web container resolved the warning on the next run. This does not establish the remaining broader observability lifecycle gates.
