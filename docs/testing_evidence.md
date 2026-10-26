# Custom test evidence

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
hidden reasoning are removed before IPC; the report consumer validates again.
Labels are always scrubbed, including when content is withheld. The boundary
must come from the authority that owns configuration and credential registration.

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
