# OpenRouter native reasoning admission and retained diagnostics

[Effort controls](implementation_plan_f4o3b2.md) ·
[Testing guide](../testing_openrouter_reasoning.md) ·
[Metadata and identity requirements](implementation_plan_f4o3b5a.md)

An accepted reasoning effort does not qualify a native response format. A
successful upstream answer must remain usable when an independently qualified
route returns encrypted reasoning alongside it.

- [x] Reproduce the Terra 6.1 Responses puzzle rejection with bounded selected-account
  inference and retain status, usage and response structure separately from content.
- [x] Qualify `openai/gpt-6.1-terra` native Responses parsing and exact ordered
  continuation replay. Reuse existing bounds, credential checks and opaque-state
  exclusion from evidence and browser payloads. Keep answer text separate.
  Prove node retries retain the native assistant payload until invocation cleanup,
  preserve its wire prefix and begin the next user turn with fresh retry history.
- [x] Separate native-state admission from visible-reasoning capability. Encrypted
  reasoning is not readable text; do not advertise or synthesize a native explanation
  from ciphertext. Actual validated plaintext, when returned opportunistically,
  remains displayable; a catalog capability is not a filter discarding received text.
  Do not inherit native-state qualification from effort version ranges.
- [x] Log retained integration bridge execution errors to stderr with credential
  scrubbing and stack locations. Use the current manual owner's registered authority;
  withhold arbitrary messages without it. Preserve diagnostics regardless of
  `LOG_CONTENT`. Do not add request/response dumps to this diagnostic path.
- [x] Verify live puzzle completion and two-call replay, normal Studio integration,
  causal rejection checks, exact applicable unit coverage, packages, public boundary
  and independent external review before publication.
- [x] Human acceptance of Terra 6.1 OpenRouter Responses puzzle completion on
  November 3, 2026. This does not infer acceptance of diagnostic failure scenarios.
- [ ] Human acceptance of actionable retained diagnostics.

Manual DEBUG requests remain outside finalized Allure results. This repair does
not create a manual tracing owner or promise token streaming or readable reasoning.
Broader trace semantics remain in [complete execution traces](implementation_plan_k1a1.md).

Public contract: [OpenRouter reasoning tokens](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Common native reasoning contract

- [x] Admit the documented native reasoning schema on canonical OpenRouter GPT
  family routes independently of effort controls and readable-summary capability.
  Native admission uses the shared gateway carrier contract rather than an effort
  version floor. Retain canonical numeric identifiers and known family boundaries;
  reject other backends, malformed aliases and unsupported native fields.
- [x] Preserve exact ordered Responses items and Completions assistant reasoning
  blocks for within-turn continuation, including tool results after native state.
  Keep ciphertext and signatures out of evidence and browser content; validate
  known fields, sizes, output correspondence and combined credential carriers.
- [x] Qualify available Luna, Terra, Sol and Astra 5.6/6/6.1 and GPT-OSS routes on
  both APIs with bounded live requests, separately recording unsupported or
  unavailable combinations and distinguishing native replay from ordinary replay.
- [ ] Close causal rejection, applicable coverage, package, boundary and
  independent review gates before publication.

The [OpenRouter preserving-reasoning contract](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens#preserving-reasoning)
defines ordered Responses item preservation and standardized Completions
`reasoning_details` across supported reasoning models. This establishes the wire
contract, independently of which versions accept effort controls. Measured exact
IDs and unavailable combinations remain in the testing guide. Unknown fields or
malformed carriers fail validation even on a catalog-admitted family route.

- [x] Expose OpenRouter tool support only when the canonical native route's
  catalog explicitly lists both tools and tool selection in valid bounded
  parameter metadata. Require parameter-support routing without fallbacks for
  tool requests. Verify real tool continuation through the normal provider path.

- [x] Validate complete native item identities, fields and statuses on completed,
  truncated and refused outcomes. Withhold malformed known native carriers from
  rejected response evidence, including non-list envelopes, serialized detail
  fields and unsupported deltas. Preserve valid incurred usage and actual HTTP
  status when credential-safe rejection replaces an upstream error.
- [x] Record the Medium Luna 5.6 Responses pair as ordinary replay only; its
  separate High tool sequence establishes native replay. Normalized `Ok`
  assertions do not establish literal exact-answer compliance.

- [x] Reuse full native item/message grammar for evidence projection, including
  nullable plaintext aliases. Reject or withhold every malformed known carrier
  shape across incomplete success and HTTP rejection paths; preserve valid
  plaintext and usage. Exercise type/field/outcome combinations through actual
  transport capture, with causal checks on both shared schema gates.

- [ ] Guard every string and object key admitted by response evidence projection,
  jointly with native reasoning, decoded arguments and visible output, before
  successful response evidence or native trace capture. Include tool identities,
  function names and nested metadata; retain bounded checks, complete schema
  admission, request-only authority and exact opaque continuation replay.

Native reasoning/parsed arguments and retained evidence are independently bounded
representations of one response, each limited to 4,096 carriers. Their joint
literal matcher admits at most twice that count and retains its 8 MiB work limit;
this does not expand native schema, replay or per-representation limits. Semantic
pair checks include both groups but omit encoded output argument strings already
represented by their complete parsed arguments. Literal matching retains those
encoded strings, including escapes, alongside all projected keys and values.

The final normalized-result check validates its own complete result grammar and
checks every released key/string against the latest credential snapshot. Its
validated opaque continuation is excluded from the additional diagnostic
representation; the existing native replay/parsed-argument checks remain active.
Late credential registration must reject split visible call identities before
final result publication, without exposing or reinterpreting opaque replay JSON.

- [ ] Apply the combined native/projected checks for every qualified native route,
  including tool-only success, null reasoning aliases and empty native details.
  Empty collected native strings must not disable checking of emitted identities.
  Preserve transport-owned route qualification through the final normalized-result
  check even when no continuation exists. Raw versus normalized representation
  selection at transport call sites is explicit; incoming fields confer no route
  or outgoing-request authority. Prove late credential enrollment rejection for
  tool-only results and retain unchanged positive fixtures and bounded groups.

- [ ] Retain native-route qualification at every normalized result admission and
  final publication commit, including after awaited result validation. Bind raw
  versus normalized authority to transport-owned call sites and route selection,
  not continuation presence or incoming field names. Verify independent late
  registration after raw admission, inner normalized admission and final result
  validation; preserve positive tool-only results and reject split identities or
  argument carriers before the outer publication commit.
