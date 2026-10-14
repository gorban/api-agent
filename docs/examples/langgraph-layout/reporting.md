# Component timing v1 — proposed reporting convention

Use the same scoped component lifecycle for live progress, Allure, and correlated tracing. The shared progress objects currently supply scope ID, parent ID, name, effective timeout context, elapsed time, and active/inactive state. Outcome, original declaration/resolver provenance, and attempt/call metadata need explicit integration; they are not already present merely because a component stopped.

## Record identity and measurements

| Field | Meaning |
| --- | --- |
| `version` | Timing attachment schema version: `1` |
| `run_id`, `test_id`, `test_attempt` | Existing recorded run/test/confirmation identity |
| `scope_id`, `parent_scope_id` | One actual component instance and its owned parent |
| `component_id`, `instance_id`, `name` | Definition, Layout instance, and fixed content-free display name |
| `declaration`, `function_id` | Relative declaration/reference and registered implementation identity |
| `config_field`, `value_source`, `resolver_id` | Configuration or derived-budget provenance, with unused fields omitted |
| `declared_seconds`, `effective_entry_seconds` | Optional declared/resolved budget and ancestor-clamped allowance at entry |
| `limiting_scope_id` | Actual scope responsible for deadline exhaustion, when known |
| `elapsed_seconds` | Monotonic inclusive duration; no subtraction of child durations |
| `started_at`, `finished_at` | Report timestamps; monotonic measurements remain authoritative for duration |
| `group_attempt`, `node_attempt`, `call_ordinal` | Applicable iteration identities, kept separate |
| `outcome` | Explicit completed, rejected, timed_out, failed, cancelled, or incomplete disposition |

An owner-supplied derived budget also records safe input counts and configuration identities needed to explain its calculation. Unknown values remain unknown. Fixed names and allowlisted numeric/structural metadata must not contain prompts, raw tool data, user identities, URLs, headers, or credentials.

## Allure mapping

- Record integration/E2E components as nested steps under the correct test/attempt; use scope ancestry rather than arrival order to reconstruct parallel events. Preserve parallel start/stop intervals.
- Attach a versioned `component-timing-v1.json` containing the richer measurements and provenance. Existing per-test attachment support can carry this data; live lifecycle collection and step serialization still need implementation.
- Represent completed work as a passed step; record rejection/failure according to the operation's actual disposition. A timeout or cancellation remains visible as such, including when a parent recovers and the test ultimately passes.
- Retain failure/cancellation/incomplete evidence honestly when cleanup or publication does not finish. Do not report unfinished scopes as passed.
- Preserve existing Allure hierarchy, attempts, immutable publication, and privacy boundaries. Unit mode emits live progress but no Allure files.
- Retain scope start/finish records and bounded diagnostic detail rather than every second of an unbounded heartbeat. Aggregate only after each recorded test/attempt is finalized.

[Allure steps](https://allurereport.org/docs/steps/) support nesting, individual duration/status, parameters, and attachments. [Its result-file format](https://allurereport.org/docs/how-it-works-test-result-file/) defines how results/steps and attachments are serialized. This convention specifies the additional integration, not an implemented exporter.

## Synthetic attachment example

```json
{
  "version": 1,
  "run_id": "synthetic-run",
  "test_id": "example::query_scenario",
  "test_attempt": 1,
  "components": [
    {
      "scope_id": "tool-call-3",
      "parent_scope_id": "query-group-1",
      "component_id": "timeout",
      "instance_id": "graphql_call_clock",
      "name": "GraphQL API call",
      "declaration": "layout.md",
      "function_id": "selected_api.query_graphql",
      "config_field": "GRAPHQL_CALL_TIMEOUT_SECONDS",
      "value_source": "shared_default",
      "declared_seconds": 60.0,
      "effective_entry_seconds": 30.0,
      "elapsed_seconds": 23.4,
      "group_attempt": 1,
      "call_ordinal": 3,
      "outcome": "completed"
    }
  ]
}
```

The example child received only its parent's remaining 30 seconds. Live presentation can show `23s elapsed; timeout 30s — GraphQL API call`, while the attachment explains the original 60-second declaration. IDs here are synthetic structural identifiers, not actual run evidence.

## Analysis convention

Compare matching logical components across baseline/candidate runs while retaining their separate attempts. Report call counts, retry counts, observed queue/operation time where measured, aggregate duration, configured/resolved allowances, and differences in effective budgets.

A broad parent allowance does not prove one LLM call took that long. A 900-second passing test might spend time in many calls, repair rounds, waiting, or one slow component; the evidence should distinguish those cases. Inclusive parent and child durations overlap, and concurrent children can overlap each other. Never sum them into a false test total or label residual time as queueing unless it was measured.

Preserve existing timeout policies during structural migration. Use this evidence to propose and validate later tuning, not to silently tighten deadlines or infer new retry behavior.
