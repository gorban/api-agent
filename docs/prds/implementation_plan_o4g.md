# Optional tool settings and service dependencies

[Tool requirements](implementation_plan_o4.md) · [Selection/composition](implementation_plan_o4d.md) · [Ownership](implementation_plan_o4e.md)

Configuration inputs, injected service dependencies and container prerequisites are independent capabilities. A tool is not required to have an endpoint, a Docker image, a storage backend or any environmental setting.

## Declaration and composition requirements

- [ ] Accept an absent/empty local input schema and absent/empty prerequisite declaration as normal valid cases, with equivalent empty-schema/empty-list normalization. Require a default implementation identity, but do not require an environment override or expose an unnecessary selector/configuration prompt.
- [ ] Support tools with configuration only, prerequisites only, both, or neither. Compose active settings and service dependencies independently from selected usages. No globally required ENDPOINT slot, `_ENDPOINT` field name, container or storage declaration may be inferred from the fact that an object is a tool.
- [ ] Allow a tool to borrow an operation-owned in-process service handle or a declared ready service output without inventing a user-facing URL/configuration field. Bind those services through the same admitted named-instance plan; validate capability, identity and lifetime before invoking the tool. This is a bounded dependency join, not a second service scheduler.
- [ ] A tool with no tool-specific settings contributes no Review rows/missing fields. A tool with no container prerequisites does not probe/select an engine or install/start a service on its own behalf. Preserve the selected operation's independent reporting/UI/provider prerequisites; their existence must not be attributed to every tool.
- [ ] Distinguish external endpoint consumption from managed prerequisites: a configurable remote client can have settings without locally owned containers. A managed service may deliver an internal address/handle without requiring any user-supplied environment value. Applicable shared engine settings still belong to infrastructure, not an invented tool endpoint.

## Storage-backed tool boundary

- [ ] Express storage through an explicit typed service capability/owner when a selected tool needs it. Its implementation may be in-process memory, a captured local backend or an externally provisioned object store; enforce the selected implementation's durability, access scope and lifecycle requirements. Do not silently replace a required durable backend with memory.
- [ ] Result listing, sampling, metadata access and retention do not inherently require an object-store endpoint. Determine their concrete prerequisites from the selected storage implementation and bound service, including transitive dependencies, rather than tool names or call arguments. A missing endpoint argument is not proof of a dependency-free implementation.
- [ ] If a selected storage backend uses managed object-storage emulation also used by reporting, join the same explicitly compatible service identity and existing lease/seed/readiness owner. Keep data namespace/access configuration separate; do not start a duplicate emulator merely because there are two consumers. Remote storage or in-process backends add no such managed service unless explicitly declared.

## Independent conformance fixtures

Use independently authored generic fixtures for every row; no inference credentials, private code or corporate data are needed.

| Tool shape | Fixture | Required behavior |
| --- | --- | --- |
| No extra inputs; no prerequisites | Count records in caller-supplied JSON | Discover, bind, verify and run with empty environment and no engine/network/storage imports or acquisition |
| Inputs only | Format supplied data using a typed display setting | Selected field appears/validates; no service/engine activity |
| Prerequisites only | Borrow a controlled operation-owned service handle | Acquire/bind service through its owner; no invented endpoint field or tool-specific required environment setting |
| Inputs and prerequisites | Managed data service with a typed tool option | Compose both closures, await readiness and deliver isolated values/handles |

- [ ] Exercise all four shapes through the same parser, configuration, named ownership and invocation consumer. Switching to a tool with no inputs/prerequisites must remove stale settings/service roots while preserving dormant user values and safe disposal of previous owners.
- [ ] Separately prove a memory-backed result service and a controlled object-store-backed implementation. Assert no emulator for memory/remote cases and one compatible shared emulator for reporting plus local object storage. Do not claim a concrete legacy backend is configuration-free without inspecting/admitting its actual dependency declaration and controlled execution.
- [ ] Keep unknown/invalid declarations, missing injected capabilities, incompatible sharing and partial acquisition as explicit pre-effect failures, retaining ordinary exact coverage, causal mutations, packaging and review/human gates.
