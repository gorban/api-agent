# Shared timeout component — proposed declaration

## Definition

| Property | Value |
| --- | --- |
| Component ID | `timeout` |
| Registry handler | `shared.component_timeout` |
| Existing implementation | [component_timeout and progress scopes](../../../../src/api_agent/providers/progress.py) |
| Live formatter | [component_timing](../../../../src/api_agent/tui/test_results.py) |
| Reporting contract | [Component timing v1](../reporting.md) |

## Inputs

| ID | Type | Source | Meaning |
| --- | --- | --- | --- |
| component_name | fixed printable string | instance `Name` | Stable description; no prompts, users, URLs, or credentials |
| seconds | optional positive finite number | instance configuration field or owner-supplied resolved budget | Declared seconds; omission adds no deadline |
| budget_provenance | structural metadata | configuration/resolver binding | Field/declaration/resolver identity and effective value source |
| body | registered callable / owned scope | node, group, or operation instance | Work wrapped by this component |

## Outputs

| ID | Type | Meaning |
| --- | --- | --- |
| result | body's declared output | Unchanged successful result |
| budget | shared `TimeoutBudget` | Scope/parent identity, fixed entry allowance, monotonic deadline and remaining time |
| lifecycle | scoped events | Start/progress/finish with explicit outcome metadata supplied by the caller/adapter |

## Notes

This section is nonexecuting. The wrapper is shared; its Layout registration and enriched reporting contract are proposed. Each instance obtains its own scope, while inheriting its enclosing deadlines.

- Call the existing `component_timeout` around the actual body. Do not add an independent timer that races or changes its deadline policy. Existing externally enforced work can use `report_timeout` with the same budget where that owner performs cancellation and cleanup.
- Read the selected effective configuration, not just the source default. Preserve configuration type, explicit override provenance, and declared units. [The example fields](../cfg/config.toml) use `acknowledge_default = false` for advanced settings.
- Keep a group/graph wrapper open across its children and retries. A node or tool retry may have its own new attempt scope but cannot extend the ancestor deadline.
- A timed child entered with 30 seconds remaining on its tightest ancestor and declaring 60 seconds receives 30 seconds. Display that fixed entry allowance while elapsed time grows; do not display a shrinking allowance as though it were the declared timeout.
- Untimed children remain constrained by ancestor deadlines. The live formatter uses the nearest timed ancestor's name/clock; without one, show the active child's elapsed time and omit a timeout value.
- Queue/wait time spent inside a scope consumes its budget. Record separate queue/execution spans only when actually measured. Shared backend-owned authentication scopes remain independently owned and are linked to waiters rather than incorrectly nested under one waiter's deadline.
- Propagate deadline ancestry and operation/test identity explicitly across process/worker boundaries. Context-variable inheritance alone does not cross them.
- The callable/process/session owner still performs cancellation, draining, and cleanup. A timeout signal is not proof that a thread, subprocess, HTTP request, or mutation stopped.
- Preserve the caller's timeout/recovery/error semantics. The reporting adapter maps the actual outcome; it cannot infer success from an inactive progress event or convert a timeout into a passed scope.
