# Conditional pack-declared infrastructure prerequisites

[Follow-through](implementation_plan_o.md) · [Existing infrastructure requirements](implementation_plan_h1a.md) · [Lifecycle foundation](implementation_plan_h1.md)

Resource packs can declare selected Compose prerequisites through the existing shared planner and lifecycle manager. Concrete service definitions and conditions remain with their selected pack/provider. The approved v1 technical scope is delivered; its detailed requirements and acceptance remain in the linked children.

## Contract and delivery

- [x] Agree and deliver the [bounded Compose-only contract](implementation_plan_o1a.md), including discovery, typed activation, dependencies, separate host/container endpoints, readiness/seeding and one explicit Docker-compatible Unix engine. Generic host processes remain a separate extension; existing UI process ownership remains separate.
- [x] Capture selected-owner declarations and finite assets without importing provider adapters. Prove source and relocated installed-pack use from unrelated directories, with complete packaged assets and immutable compatibility evidence.
- [x] Build and acquire an explicit dependency plan in readiness order. Validate activation, dependency/profile closure, endpoints and Compose admission before startup; disabled/help/collection paths start no services.
- [x] Deliver safe status and bounded labeled logs through the existing ownership, lease, inventory, rollback and retryable cleanup lifecycle. Preserve reused services, explicit retention and durable volumes; startup never resets data implicitly.
- [x] Prove remote/disabled, local API/external backing and local API/managed backing with an independently authored external synthetic pack. Complete controlled/disposable lifecycle, exact applicable coverage, causal mutation, package/license/public-boundary and independent/external review gates. See [detailed requirements and acceptance](implementation_plan_o1a.md).
- [x] Verify the [concrete declaration format and public runtime interfaces](implementation_plan_o1b.md) against source and installed conformance.

## Deferred human review

The bounded Compose foundation has completed substantive work and independent/external review. Its unchecked human acceptance remains in the child; dependent Run local, full UI/authentication, concrete service providers and broader reporting retain their own gates.

- [Approved contract and acceptance](implementation_plan_o1a.md)
- [Declaration format](implementation_plan_o1b.md)
