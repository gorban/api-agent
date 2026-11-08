# Progressive integration and E2E diagnosis

[Test execution](development.md#testing) · [Evidence and capture policies](testing_evidence.md) · [Tracing](tracing.md)

Start with the existing run, not another inference run. Read enough metadata to
identify the execution boundary, then open only the evidence that can distinguish
the remaining explanations. Keep complete durable logs; bounded inspection means
limiting what enters agent context, not truncating the stored evidence.

## 1. Establish which outcome needs explanation

Record the repository-qualified test ID, run/report identity, source revision,
effective configuration/profile, launcher versus legacy harness, failure phase
and outer test attempt. Use the run's resolved
settings, including per-test overrides; today's `.env` may differ. Read the
terminal summary and that attempt's short failure details before opening bodies.

| Observation | Interpretation and next evidence |
| --- | --- |
| First-chance test pass, internal node/provider retry | The outer test passed. Inspect the recovered scope only if investigating recovery, latency, spend or a reported defect. |
| First-chance failure, later passing confirmations | Preserve both first-chance failure and final outcome. Inspect the failed outer attempt and its confirmations separately. |
| Confirmation failure or exhausted workflow retry | Find the authoritative failed attempt and the innermost relevant failure, then its owning component and final application outcome. |
| Setup, browser, cleanup or publication failure | Begin at that boundary. A missing graph can mean execution never reached it. Distinguish application/test outcome from report delivery. |
| Passing test with missing image, content or spans | Investigate capture/publication separately; passing assertions do not establish complete evidence. |

Workflow/component/node passes, provider transport retries and outer E2E reruns
are different layers. A failed child visit does not establish final graph failure.
Confirm recovery using matching owner/ancestry and later accepted outcomes, not
names, colors or adjacent timestamps alone. A genuine partial result may remain
partial. An initial E2E pass needs no confirmations; after initial failure,
the configured additional confirmations must all pass. Unit tests never retry.

## 2. Use Allure as the common starting point

Open the exact test attempt. Follow Conversation → User turn → graph →
component/node/tool/call where that hierarchy is recorded. Inspect status,
attempt/max-passes, failure classification and timing. Open the rendered-turn
image for browser, layout or rendered-data symptoms, and LLM diagnostics for
provider/model symptoms.
Do not expand every successful sibling or every request body.

Use **Execution evidence** for the event journal and **Payload index** for capture
availability and references. Record actual conversation/turn, invocation,
scope/visit, workflow attempt and call IDs. Transport attempts may additionally
have `transport_attempt_id`; trace/span IDs are separate identifiers. Not every
boundary supplies every ID. Missing or ambiguous ownership is a diagnostic gap,
not permission to join events by matching labels or wall-clock proximity.
Some shared backends correlate call/transport spans by ID without propagating a
workflow parent across processes; distinguish that documented boundary from a
missing call or lost exporter payload.

Find the earliest relevant failure inside the authoritative attempt, its typed
error/code, retry feedback and next pass. Then check the terminal result and
assertion. A successful model call can precede deterministic validation failure;
a failed provider response may have no decoded final answer or usage.

For a large journal, filter metadata by the observed IDs before reading content.
For an Execution evidence JSON envelope, this example prints at most 40 matching
event summaries, without payload bodies:

```python
import json
from pathlib import Path

journal = json.loads(Path("/absolute/path/to/execution-evidence.json").read_text())
wanted = "COPY_AN_OBSERVED_ID"
ids = ("invocation_id", "turn_id", "scope_id", "visit_id", "attempt_id",
       "call_id", "transport_attempt_id", "parent_id", "cause_id")
summary = ("event", "sequence", "owner", "kind", "outcome", "attempt",
           "max_passes", "reason", "code", *ids)
matches = [event for event in journal["events"]
           if any(event.get(key) == wanted for key in ids)]
print(f"{len(matches)} matching events")
for event in matches[:40]:
    print(json.dumps({key: event[key] for key in summary if key in event}))
```

Follow explicit parent/cause references as needed. Narrow further when more
matches remain. Inspect only the affected input, returned output, error and
retry feedback through their retained attachments; do not dump the whole state,
all payloads, an entire browser DOM or an unbounded traceback into context.

## 3. Branch on available tracing and content

| Existing evidence | Next step |
| --- | --- |
| Tracing disabled | Continue with Allure, its journal/payload index, durable test log and scoped application/browser diagnostics. No trace backend is required. |
| Tracing enabled and correlated spans available | Locate the exact run/turn/call using recorded IDs. Inspect the affected ancestry, retry and terminal spans; compare provider errors with the application outcome. |
| Tracing enabled but missing/incomplete | Check resolved trace mode/export destination, collector lifetime/readiness and exporter warnings first. Check sampling, span/capture budgets and explicit omission reasons. Missing spans do not establish that no call occurred. |
| Content absent | Check capture state and destination policy before treating it as an empty request/response. `withheld`, `truncated`, `unavailable`, `not_observed` and `ambiguous` have different meanings. |

`LOG_CONTENT` controls log/Allure content; `TRACELOOP_TRACE_CONTENT` controls
trace content independently. Neither enables the other or retroactively restores
content. Credentials remain protected in both modes. No trace-content setting
permits raw encrypted reasoning or arbitrary private state. A masked screenshot
can still establish layout and controls without exposing the response.

Tracing/export failure must produce a clear warning and preserve the test's
application outcome. Diagnose an Allure publication failure separately; report
the actual publication status and do not claim unavailable evidence was retained.

## 4. Inspect source or reproduce only to close a concrete gap

Map the identified node/component to its selected flow declaration and native
handler. Inspect its declared I/O, validation, retry budget, feedback mapping and
terminal projection, then the smallest related implementation/test files.
An opaque delegated graph may require private runtime diagnostics; its wrapper
is not evidence of converted internal orchestration. Keep private details out of
public reports and code.

State the unresolved hypothesis before rerunning. Prefer an offline regression
or controlled reproduction. If new live evidence is required, run the smallest
relevant selection under the applicable existing authorization, retain its full
log and identify any changed tracing/content/configuration. Do not repeat an
entire paid suite or toggle content indiscriminately to compensate for unread
existing artifacts. Do not silently clear authentication caches or switch
providers to bypass the observed failure.

Report first-chance and final outcomes separately, the affected flow boundary
and IDs, decisive artifact references, confirmed cause versus hypothesis, and
any missing evidence. A recovered error is worth reporting when relevant; it
does not turn a passing final test into a failure or erase its earlier failure.

Keep one compact evidence packet for subsequent work: run/selector/outer attempt,
first-chance and final status, evidence directory, trace ID if present, first
divergent owner, typed error/code, retry layer/count, relevant source location
and capture availability. Compare this signature between failed and passing
attempts before comparing whole prompts. Distinguish provider transience,
external-service variability, application nondeterminism, test races and capture
failure; a later pass alone does not choose between them. Stop broadening the
search when the owning layer and supported cause are established.
