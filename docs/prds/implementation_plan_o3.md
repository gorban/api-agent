# Verified API tooling and bundled demonstration

[Design decisions](implementation_plan_o.md) · [API demonstration](implementation_plan_b2c.md)

## Agreed v1 contract

The user approved this bounded contract on October 24, 2026.

- [x] Select published `mcp-graphql@2.0.4` with a locked npm dependency tree and retained license notices. Launch its shipped JavaScript as an owned stdio process with an explicit fixed endpoint and owner-supplied headers. Disable mutations. No MCP HTTP listener or implicit upstream endpoint is admitted.
- [x] Initially support exactly one read-only, variable-free query operation. Reject variables, mutations, subscriptions and unsupported documents before execution. Do not rewrite an already verified query or bypass MCP execution with direct HTTP.
- [x] Obtain a complete schema through a fixed trusted introspection query over MCP; reject unsuccessful or incomplete discovery. Freeze its fingerprint and validate generated queries deterministically before a separate LLM relevance/grounding check. Generation cannot execute candidates.
- [x] Preserve HTTP-success GraphQL responses containing both data and errors, including MCP tool-error envelopes. Distinguish HTTP, MCP protocol, malformed output, timeout and cancellation failures. Never render missing data as successful empty results.
- [x] Keep tool discovery, verification and execution API-independent. Prove that the same consumer works with a synthetic REST tool owner.
- [x] For the bundled demo requiring API execution, omitted and explicitly blank `API_ENDPOINT` select its synthetic read-only API through pack-declared Compose prerequisites. Preserve their configuration provenance. An owner-default nonempty or explicit nonempty endpoint selects that target without starting the synthetic service. External packs retain their own endpoint policy and never inherit this fallback.

## Delivery and acceptance

- [x] Deliver the generic tool contract and content-bound verification order, with unchanged selected-provider/model attribution and no automatic inference replay.
- [x] Deliver bounded schema/query/result limits, nested timeout/progress reporting, owned MCP startup/cancellation/reaping and privacy-aware observations. Credentials remain excluded independently of content settings.
- [x] Evolve only the main demo into planning, optional verified tool execution and response rendering. Correctness and grounding block every pass; presentation preferences may relax only on the final pass. Preserve permanent qualification fixtures and the existing simple chat application contract.
- [x] Deliver the independently authored synthetic GraphQL target without a database, with bounded identity readiness, loopback endpoints, compatible reuse and acknowledged cleanup through the existing infrastructure owner.
- [x] Prove deterministic rejection before execution, no execution on failed relevance, optional no-tool chat, partial/error rendering, REST conformance and endpoint selection/lifecycle.
- [x] Verify locked dependency preparation, installed and relocated artifacts, applicable exact 100% line/branch units, independent lane reviews, public-boundary checks and external PR review.
- [ ] Human review the completed demonstration. Controlled evidence alone does not imply live human acceptance.

Variables are deliberately unsupported by this pinned adapter because its published handler sends them in an unsuitable form. A future capability expansion requires a separately reviewed package/version or maintained upstream repair; other tool owners can advertise different input capabilities behind the generic contract.

## Review hardening acceptance

- [x] Reject remote schema references and keep input/output validation bounded in an owned isolated worker, including expensive local references and cancellation cleanup. Validation must never make implicit schema-network requests or block the event loop past its deadline.
- [x] Exclude raw and normalized header credentials from tool results, render inputs and recorded evidence, including repeated spaces, tab-separated authorization and surrounding header whitespace.
- [x] Require every selected field of the fixed introspection query before freezing a schema; missing directives or nested fields are incomplete, while legitimate null values remain supported.
- [x] Settle child ownership after confirmed reap even when stderr draining fails; retain a diagnostic without making every later cleanup attempt fail.
- [x] Reuse the exact verified installed MCP tree across provider instances without registry access; prepare only a missing or invalid installation.
- [x] Preserve the public 200-character request-ID contract for tool and no-tool turns.
- [x] Traverse actual JSON Schema subschemas when checking references. Reserved property names and literal data in `const`, `enum` or `default` remain valid; remote references in schema-valued positions remain rejected for tool inputs and outputs.

## Demo service navigation follow-through

- [x] Close the [friendly demo API landing page](implementation_plan_o3a.md), preserving the existing execution and readiness contract.
