# Included generic GraphQL API provider

Implement inside `api-providers/graphql-api-provider/`, with importable package `graphql_api_provider`. Core contracts remain API-type-independent; see [shared provider composition](implementation_plan_m.md).

## Checklist

- [ ] Establish only the needed package, `cfg/config.toml`, source and synthetic unit/contract fixtures under the included provider root. Specify how it is built, loaded and measured by public CI without assuming installation from CWD.
- [ ] Contribute required `UPSTREAM_API_PROVIDER` factory selection only when GraphQL execution needs it. No embedded endpoint, credentials, private module or sibling-path default. Resolve the selected extension and its declarations through the shared loader before starting GraphQL clients/routes.
- [ ] Define GraphQL request/variables, response/data/errors, transport failure and schema-evidence contracts using public specifications and synthetic schemas. Keep these types within this provider; generic core invokes advertised capabilities.
- [ ] Own typed positive transport settings `GRAPHQL_HTTP_TIMEOUT_SECONDS`, `GRAPHQL_MAX_CONNECTIONS` and `GRAPHQL_MAX_RESPONSE_BYTES`, initially defaulting to 30, 10 and 1048576. Merge contributions through the shared schema; extensions may supply compatible defaults without duplicating transport. Verify deadline, connection and decoded-response bounds with synthetic fixtures.
- [ ] Provide optional schema discovery and execution tools with explicit capability errors, bounded deadlines/cancellation, verified TLS, request context and runtime-owned HTTP clients. Do not infer that every GraphQL deployment permits introspection.
- [ ] Define an upstream extension boundary for authentication context, metadata, pre-execution validation and optional route/setup needs. Await required validation before all upstream execution paths, including direct tools. Never interpret product-specific claims or signing rules here.
- [ ] Reuse independently defined generic knowledge/snapshot tooling where appropriate; tag GraphQL evidence by source/freshness and distinguish hints from authoritative live evidence. Other API providers must be able to supply their own evidence without GraphQL types in core.
- [ ] Normalize partial GraphQL errors without reporting a failed operation as success. Avoid blindly replaying mutations after auth/transport failure; keep protocol retries bounded and separate from Layout verification loops.
- [ ] Test synthetic transport/schema/execution, validation rejection before any outbound request, extension configuration/routes, unavailable introspection, partial results, privacy, timeout/cancellation, client disposal and concurrent pack isolation. Build the included distribution and verify package/declaration discovery.
- [ ] Select a small synthetic REST provider at an external path through the same core interface, proving the included GraphQL provider is replaceable without core changes. This proof does not require a production REST implementation.
