# Operation selection and configuration composition

[Tool requirements](implementation_plan_o4.md) · [Binding contract](implementation_plan_o4a.md) · [Ownership and delivery](implementation_plan_o4e.md)

The core selection/configuration implementation is delivered. Human acceptance and linked residual requirements remain separate; the discovery requirements record the accepted authoring contract.

## Graph and test selection

- [x] Describe an immutable operation selection containing the selected owner/roots, application graph or exact selected test identities, graph entries, ordered replacements and finite generated variants. Keep a catalog of available graphs separate from the selected execution closure.
- [x] Declare test-to-graph associations statically, including repository identity, pytest node/case identity, owning root and replacement set. File-level defaults may cover cases only when unambiguous; explicit case overrides must not overlap contradictorily. One test may require several graphs. Do not assume that an integration selection also consumes the application's graph.
- [x] Keep existing non-graph API/authentication/UI/reporting requirements as independent operation inputs. A graphless test may have no workflow tools. Test collection and the picker must work before execution-only tool settings are supplied; explicit graph declarations must be discoverable without importing tests to infer dependencies.
- [x] Derive tooling from selected replacement-expanded v2/v3 declarations, including root/component/node scopes and all structurally declared conditional branches. Retain every graph/scope/port use site while deduplicating the same qualified instance. An entry omitted by replacement contributes neither resources nor tool requirements.
- [x] Produce graph-located catalog diagnostics separately from active-operation validation. A malformed catalog/root or selected graph blocks its dependent operation; decide how an invalid unselected graph is displayed without turning its missing execution settings into application blockers. Collection starts no tools or services.

## Three resolution stages

1. **Discovery.** Resolve only the directory, pack, graph/replacement/catalog and selector-controller settings required to locate declarations. Parse/capture graph and instance descriptions without validating unrelated execution values or importing callable catalogs/factories.
2. **Implementation selection.** Select the operation's instance closure, resolve implementation-selector defaults and explicit values, then load only the selected implementations' inert typed input declarations. A repeated factory path is valid for different instances. Controller/declaration cycles, rather than repeated paths, determine cycle rejection. Bound every discovery loop and capture size.
3. **Execution configuration.** Bind local input slots to owner-declared field names, combine this tooling schema with applicable shared/provider/pack configuration, then validate the original explicit inputs. Preserve hierarchical defaults and base dotenv, pack dotenv, environment, CLI and interactive precedence, path anchors and value provenance. Deferred parsing must not lose duplicates, flag values or unknown-option errors.

- [x] Validate selector controllers from already available declarations. Service outputs, factory imports and later implementation-only settings cannot select the implementation that declares them. Resolve conditional defaults through an acyclic dependency closure; reject unresolved/cyclic selection before effects.
- [x] Keep one local slot schema per implementation and one value mapping per named instance. `ENDPOINT` can bind to `API_ENDPOINT` for primary and `API_2_ENDPOINT` for secondary. These names remain stable as context changes; no endpoint value is split, renamed or inferred by numeric suffix.
- [x] Compose only consumed slots: choosing an implementation needing `PATH` may add the owner's `API_2_PATH`; an unselected implementation cannot add that setting. Require intentional compatible aliases for sharing; reject colliding types, defaults, safety metadata and ambiguous ownership. Separate instances remain separate even with identical values.
- [x] Freeze declaration identities independently of request-affecting operation bindings. Preserve original configured endpoint/source separately from an acquired effective endpoint/source. Do not emit secret values or secret-derived hashes in either identity or diagnostics.

## Review and edit behavior

- [x] Feed the same selected tooling schema into actual configuration Review, editors/default acknowledgement and execution validation. Project the tooling schema before any existing review-across-modes behavior, so that behavior cannot reintroduce unselected test tools. Preserve existing shared/provider review behavior outside this tool scope.
- [x] Main-menu/application Review uses the selected application graph. After integration/E2E selection, Review uses precisely that test union. Before tests are selected, collection remains available; the interface must identify the current context without implying all available tests are active.
- [x] Hide default implementation selectors using existing presentation metadata. Preserve documented explicit configuration and allow owners to expose additional settings deliberately. Active endpoint fields stay visible/editable. Persist user intent only; acquired addresses never replace dotenv values.
- [x] Invalidate and rebuild the plan, pending/default rows, validation and execution admission after a pack, graph, replacement, selected-case, implementation or controller edit. Preserve dormant explicit values for later use/cleanup; never retain stale requirements or execute an earlier plan.
- [x] Distinguish an unknown input from a known inactive owner alias. Decide whether an explicit inactive CLI field is rejected with a contextual diagnostic or retained dormant; it must never implicitly activate its tool. Unrelated saved/environment values must not make the operation require an inactive implementation.

## Observable configuration cases

| Selected operation | Implementation choices | Required Review membership |
| --- | --- | --- |
| Primary application | Primary GraphQL | API_ENDPOINT; no API_2_ENDPOINT |
| One primary-only integration case | Primary GraphQL | API_ENDPOINT; no secondary settings |
| Dual-target integration case | Two GraphQL instances | API_ENDPOINT and API_2_ENDPOINT |
| Same case with secondary custom REST | Secondary declares an additional PATH input | API_ENDPOINT, API_2_ENDPOINT and API_2_PATH |
| Return to primary application | Any saved secondary settings | Primary membership restored; secondary values retained dormant |

Defaults and other applicable non-tool settings retain their normal behavior. This table specifies tooling membership, not a replacement for the full configuration schema.

## Owner-declared nonempty tool inputs

- [x] An implementation may opt a string input into `nonempty = true`; this metadata must be an exact boolean and is invalid on other types. Omission or false preserves existing empty-string behavior. Global required-string semantics remain unchanged, and no slot name is mandatory for every implementation.
- [x] Validate selected active effective inputs before prerequisite manager construction/acquisition or factory execution. Reject empty or whitespace-only remote values with field identities only; retain ordinary missing/type/inactive diagnostics. Optional absent inputs remain absent.
- [x] An exact empty string with an admitted effective local-output binding provisionally satisfies this policy. Whitespace cannot select local provisioning. Evaluate after explicit shared-primary derivation, preserving configured provenance and inherited pending outputs; retain policy through case narrowing and worker transport.

## Collection and binding-conflict recovery

- [x] Keep the integration/E2E picker available when its current selected union has a tool-configuration conflict. Show the conflict as a selection/configuration diagnostic, allow selection changes and rebuild the plan, and block execution until the conflict is resolved. Collection or selection must start no tools or services.
- [x] A tool input may bind an existing provider/shared configuration field when its effective type, defaults, validation, applicability and safety/presentation constraints are compatible. Preserve the original configuration declaration and value provenance. Reject incompatible declarations without exposing configured values; keep distinct instance identities and same-operation alias collision guards.
- [x] Separate exact test cases may explicitly bind compatible tool inputs to the same public setting name when no selected case consumes both instances. Compare effective declarations and input safety policy; reject incompatible settings, same-case overlaps and ambiguous unscoped ownership. Preserve separate instance identities, per-case admission, provenance, local-output bindings and cleanup owners. Shared setting names never merge tools or service targets.
