# Custom test evidence

For investigating a recorded run, start with [progressive diagnostics](testing_diagnostics.md).

Tests can publish bounded JSON attachments without invoking a tool or LLM. Import `publish` from `api_agent.testing.evidence` and pass the owning
configuration and credential boundary. `content_boundary(config)` from
`api_agent.testing.inference_content` registers configured credentials and uses
`LOG_CONTENT` to control attachment content. Register credentials acquired later
on that same boundary before publishing them.

The following examples use your collected journal dictionary and observed scope
and call events; they do not discover execution owners from names or timing.

```python
from api_agent.testing.evidence import publish
from api_agent.testing.inference_content import content_boundary

boundary = content_boundary(config)
boundary.register_secret(session_token)
publish("Validation result", {"matched": True}, boundary=boundary)
publish("Graph result", {"matched": True}, boundary=boundary,
        invocation_id=journal["invocation_id"])
publish("Scope result", {"matched": True}, boundary=boundary,
        invocation_id=journal["invocation_id"], scope_id=scope_event["scope_id"])
publish("Call result", {"matched": True}, boundary=boundary,
        call_id=call_event["call_id"])
publish("Trial result", {"matched": True}, boundary=boundary, trial_index=3)
```

Pass actual execution IDs, as canonical UUID strings. Graph and scope owners
require the invocation ID; a scope also requires its scope ID. A call requires
only its call ID. A trial requires only an integer from 1 through 60. Omitting
all IDs attaches evidence to the test. These target combinations are exclusive.
Missing or ambiguous recorded owners appear under Unattached evidence.

The integration runner supplies `API_AGENT_INTEGRATION_EVENTS` and
`PYTEST_CURRENT_TEST`. Outside that environment publishing validates inputs and
writes nothing. Attachments use the existing workflow evidence channel; they do
not alter workflow sampling evidence.

`LOG_CONTENT` must permit content at both emission and report ingestion.
Tracing content configuration is independent. Disabled content produces an
explicit withholding marker while retaining a scrubbed title. Credentials and
encrypted reasoning and internal state are removed before IPC; the report consumer validates again.
Provider-returned plaintext reasoning/summaries are captured only with explicit
content opt-in, separately from the final answer. Enabling capture cannot make
an empty visible answer a valid test measurement.
Labels are always scrubbed, including when content is withheld. The boundary
must come from the authority that owns configuration and credential registration.

Withheld custom evidence appears as a separately labelled **capture diagnostics**
attachment containing `capture_reason: "withheld"`, not as a substitute content
value. Permitted attachment values remain the scrubbed producer data. Routine
successful reasoning capture adds no explanatory notice; notices describe only
omissions, withholding or incomplete capture and stay outside provider payloads.

Values accept only JSON objects with string keys, arrays, strings, booleans,
null, interoperable integers, and finite numbers. Object coercion and arbitrary
file attachment paths are unsupported. Cycles and values exceeding 16 nesting
levels, 2,048 visited JSON items, or 65,536 encoded bytes are rejected. Dictionary
keys count toward the item budget. Titles have a 1,024 encoded byte limit. The
complete packet must also fit the byte and structure limits. Invalid input
raises `ValueError` before writing an event.

Recording also limits custom attachments per selected test to 256 packets and
4 MiB of serialized packet data in total. Exceeding either aggregate limit
raises `ValueError`; these budgets apply to custom attachments independently of
other workflow evidence.

## Workflow capture defaults

Managed integration and E2E runs bind workflow capture once per test. New flows,
nested workflows and retries inherit that context; no `workflow` marker or
per-diagram logging setting is required. Without an explicit authority marker,
standard UI/tool fixtures enroll their owners automatically; E2E also enrolls its
automatically prepared UI. Additional manual credential producers need explicit
admission, such as `credential_authorities('ui', 'tools')`. An explicit manifest
remains closed. Every enrolled producer must register its boundary before
content is admitted. ToolGate records typed tool execution automatically; do not
add a duplicate generic `record_call` for the same execution.

`LOG_CONTENT=False` and `TRACELOOP_TRACE_CONTENT=False` remain the independent
defaults. Enabling either permits only scrubbed declared node I/O and typed tool
payloads for that destination. It does not permit arbitrary internal state.

Allure node and tool steps include input/output capture states and fixed
explanations. `withheld` means destination policy withheld content;
`unavailable` means the payload or its capture authority was unavailable.
`not_observed` means no phase capture event was recorded, for example after an
earlier failure. It does not assert an authority error. Ambiguous duplicate
node-phase captures are labelled `ambiguous` rather than choosing the last one.
An empty or null payload is still captured if its value was actually retained.

Standalone library callers and explicit application capture owners supply their
own observation factory and complete credential boundary. A bare content-policy
object is insufficient. An explicit owner replaces the inherited bundle without
borrowing another owner's credentials; missing authority records unavailable
content while workflow execution continues. Historical reports retain the
evidence captured at the time and are not changed retroactively.

## UI responses before the next prompt

UI tests using the prepared UI fixture can wrap each prompt and its rendered
response in the shared `api_agent.testing.ui_turn.ui_turn` async context manager.
The included Studio prompt helper already does this. Other interfaces supply
their own submission and rendered-response checks inside the same scope:

```python
from api_agent.testing.ui_turn import ui_turn

async with ui_turn(prepared) as turn:
    request = await submit_prompt()
    turn.bind(conversation_id=request.conversation_id, turn_id=request.turn_id)
    await wait_for_rendered_response()
# The bounded screenshot attempt has finished before another scoped prompt starts.
```

Use the actual submitted request's canonical UUIDs. For the included HTTP API,
`conversation_id` identifies the conversation and `session_id` identifies the
user turn. The scope serializes on the prepared test session, including capture;
tests must keep prompt submission inside it. It is not an interception mechanism
for arbitrary clicks outside the scope. A session without the fixture's capture
service retains ordinary context-manager behavior without claiming a screenshot.

The selected UI provider supplies the existing original-viewport safety policy.
With LOG_CONTENT=True, each admitted turn captures only the prepared application's
current page, after the caller's rendering check, with the same credential masking
and live safety checks as final UI evidence. TRACELOOP_TRACE_CONTENT has no effect
on screenshot permission. With LOG_CONTENT=False, a provider with an explicit
masked-content contract can capture the same layout with black confidential
interiors, keeping surrounding cards, borders and controls visible. The included
Studio and Nx shell contracts cover prompts, responses, reasoning, clarification
and approval content. Generated response frames remain completely masked.
Masks are clipped to visible content, and capture refuses missing markers or
layout changes rather than revealing content or covering unrelated controls.
Providers without that contract retain the withholding notice without inspecting
the browser.

Each turn capture uses a cooperative six-second deadline; cancellation cleanup
may take longer. Capture remains best effort. Unavailable or rejected pixels
produce fixed metadata; capture errors do not replace the test's outcome.
Cancellation propagates. The fixture permits 64 turn attempts, at most 64 PNGs
and 16 MiB total turn-image bytes, followed by one explicit budget-exhausted marker.
The transport and recorder also bound aggregate screenshot packets and bytes.
Final UI context retains its separate capture path for tests without turns and
for end-of-test inspection.

Allure stores the screenshot once and places its reference beneath the uniquely
matching User turn, even after final accounting regrouping. Missing or ambiguous
owners remain explicitly Unattached. Existing finalized reports are unchanged.
## Self-owned UI sessions

Pack E2E tests that build and close their own browser, UI and provider lifecycle
may declare `requires_owned_ui_session` instead of `requires_ui_session`.
The normal E2E approval, recording, confirmation and credential-authority gates
still apply. The marker suppresses the framework's additional prepared UI.
Register the owned UI access token with
`api_agent.testing.workflow_capture.register_owned_ui_access` before exposing
the listener, and enroll other credential producers through the normal capture
scope. The test remains responsible for bounded cleanup.

Such tests may call providers outside the framework recorder. Their call
inventory is conservatively incomplete for cost estimation; an empty recorder
does not establish zero spend. Complete provider accounting remains a separate
integration requirement.
