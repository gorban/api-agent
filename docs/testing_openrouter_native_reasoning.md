# OpenRouter native reasoning qualification

[OpenRouter testing](testing_openrouter_reasoning.md) ·
[Requirements](prds/implementation_plan_f4o3b5.md)

On November 3, 2026, bounded Medium-effort calls passed two-call conversation
checks for these catalog-available IDs. Native state was preserved exactly on
both APIs except Luna 5.6 Responses, whose Medium pair returned no native state
and therefore qualified ordinary replay only:

| Family | Qualified versions |
| --- | --- |
| Luna | 5.6, 6 |
| Terra | 5.6, 6.1 |
| Sol | 5.6, 6, 6.1 |
| Astra | 6 |
| GPT-OSS | 20b, 120b |

Numeric IDs use `openai/gpt-VERSION-FAMILY`; OSS uses `openai/gpt-oss-SIZE`.
Astra 5.6, Terra 6, Luna 6.1 and Astra 6.1 were absent from the catalog and
were not inferred to be working routes. Each call had a 2,048-token output cap.
The short multiplication/`Ok` fixture proves accepted requests, and, where
returned, retained native state, ordered transport replay and separation from
the final answer. Answer checks normalized case, whitespace and trailing periods;
they do not establish literal exact-`Ok` compliance. It does
not establish puzzle correctness, token streaming or cache hits.

The native parser follows OpenRouter's shared documented carrier contract on
canonical numeric GPT Luna, Terra, Sol and Astra routes, and exact GPT-OSS20B/120B
routes. Native schema admission has no effort-version floor: known fields,
types, sizes, output correspondence, route identity and credential guards remain
mandatory. Effort selection and guaranteed display capabilities retain their
independent qualification rules. An available future canonical version may use
the same wire contract; the measured table above does not claim to test it.

Responses retains original output items, including encrypted-only items and
empty summaries. Completions retains the original assistant message including
`reasoning`, `reasoning_content` and ordered `reasoning_details`. Tool results
follow the preserved assistant state. Summaries take display precedence over
plaintext detail text; plaintext aliases do not duplicate structured text.
Encrypted detail data and signatures remain server-side and are excluded from
request/response evidence and browser payloads. Evidence uses the same native
item/message grammar as inference: malformed known fields, aliases and wrappers
are withheld completely on success, truncation and rejection paths. Valid
plaintext aliases remain text, including JSON-shaped text; their contents are
not reclassified as opaque metadata. Opaque state never becomes a readable
explanation.

Independent bounds still apply: encrypted/signature fields permit at most
32,768 characters each, reasoning plaintext permits 65,536 UTF-8 bytes, HTTP
responses permit 1 MiB, and rich wire objects permit 2 MiB. Native Responses
output and Completions detail arrays permit 64 items. Credential protection
limits each carrier representation to 4,096 carriers; the joint literal view of
two groups permits at most 8,192 carriers, with one shared 8 MiB matching-work
budget. A larger catalog
output-token ceiling does not override these limits. The short measured probes
do not establish that long puzzle responses fit all native-state bounds.

A separate High-effort Luna 5.6 tool check passed both APIs through normal
provider setup: native reasoning accompanied the tool call, the following
request preserved it exactly before the tool result, and the final answer used
the returned value. Completions may include a tool-call `index`; when present,
it must be an integer matching the original array position. Replay preserves it.
Tool support is exposed only when valid catalog parameter metadata explicitly
includes both `tools` and `tool_choice`; requests require supporting providers
without routing fallbacks.

The numeric-family probes used cache Off. GPT-OSS used provider-default caching:
explicit Off is currently rejected locally because that cache route is not
qualified, before any inference. That limitation is independent of native
reasoning support and is not evidence of an upstream inference failure.

[Native requirements](prds/implementation_plan_f4o3b5.md).

Unexpected metadata and provider error wrappers are not native replay authority.
Their evidence projection drops known opaque-native fields and objects, including
serialized metadata in diagnostic strings. Prefixed/escaped diagnostic metadata
is withheld within explicit work bounds. Normal permission/model/parameter errors
keep actionable text, status and known usage. Actual assistant, user and tool
plaintext remains separate, including JSON-shaped text. Non-null Completions
detail IDs must be unique; a supplied detail index must match its original array
position. Accepted continuation state is never rewritten by evidence projection.
See [metadata requirements](prds/implementation_plan_f4o3b5a.md).

Request evidence uses explicit transport direction: validated outgoing controls,
tool definitions and structured schemas retain their effective wire values,
including authored fields named reasoning or signature. Assistant continuation
carriers still lose opaque replay fields in evidence only. Incoming response or
error metadata cannot acquire request authority by echoing those controls.


## High tool conversations and useful-demo HTTP qualification

A later November 3 run used High effort, a 2,048-token cap per call and
provider-default caching (`cache_policy=None`) for every model. Of the ten
catalog-available models listed above, nine passed each API: 18 of 20 cases.
Each passing case requested a tool, replayed the assistant before its tool
result, and returned exactly the supplied value. Seventeen preserved native
state exactly; Luna 6 Responses returned no native state and qualified ordinary
replay only. The four absent catalog IDs remained untested.

GPT-OSS20b failed both APIs in this High-effort fixture. Follow-up structural
inspection found inconsistent upstream usage, including reasoning tokens above
reported output tokens, and output-budget exhaustion. HTTP 200 did not establish
a valid provider result or completed tool conversation. These failures do not
invalidate the earlier bounded Medium checks and leave High-effort GPT-OSS20b
qualification open.

A separate Luna 5.6 Medium run exercised the selected `useful_api_demo` graph
through the normal application HTTP bridge on Responses and Completions. Both
completed the actual plan → review → render path on the first component pass,
using three physical model calls per API, a 16,384-token cap, one inference
attempt and at most two component passes. The tool owner was the real bundled
GraphQL MCP implementation against the synthetic demo API; the puzzle selected
the no-tool branch. Both HTTP responses contained syntactically valid Python.
Every returned native continuation matched its original response state exactly,
and observed encrypted/signature fields were absent from evidence and browser
projection. This graph did not reuse continuation between its independent node
requests; native transport replay is qualified by the tool conversations above.

These application HTTP checks do not establish puzzle correctness: generated
code was not executed. They also do not establish managed-launcher/browser
behavior, token streaming or cache hits. The earlier numeric cache-Off limitation
remains separate from this later provider-default run.
