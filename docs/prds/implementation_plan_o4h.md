# Shared-target dual-tool integration

[Tool requirements](implementation_plan_o4.md) · [Configuration](implementation_plan_o4d.md) · [Runtime](implementation_plan_o4e.md)

## Explicit integration fixture

- [x] Provide a separately selected integration case/profile with two named tool instances using one API target. Derive the secondary's effective endpoint and headers from the primary, or bind both to the same acquired service output. The operator configures the primary once; no separate secondary endpoint value or duplicate API stack is required for this case.
- [x] Keep this sharing explicit and local to the selected fixture. The ordinary dual-target case retains independent endpoint/header bindings. Do not add a global secondary-to-primary fallback or relax ambiguous configuration-alias rejection. Reject conflicting explicit secondary values rather than silently replacing user intent.
- [x] Preserve separate instance IDs, owners, advertised callable aliases, invocation views and verification grants even when the underlying URL and headers are equal. Repeated node usages still share the corresponding configured instance. Equal URLs do not merge tools.
- [x] Resolve sharing from the effective primary value/output, including host/container addressing, readiness and acquisition provenance. Preserve saved user configuration. Record the fixture-derived source without exposing endpoint/header secrets or hashing their values into public identities.
- [x] For managed execution, acquire one compatible primary service and its backing/seed closure through existing lifecycle owners. Borrow its output for both tools; retain one service lease/disposer. For an explicit external primary endpoint, acquire no local API/backing stack. Sharing must not mask incompatible prerequisite declarations.

## Real-inference acceptance

- [x] Add the case to ordinary static discovery/selection and execute through normal application/test ownership, selected LLM provider, authentication, accounting and reporting. Discovery and collection require neither inference nor execution endpoint values.
- [x] Run actual inference that requests both named tools, execute both through normal validity and independent relevance/grounding gates, return their outputs to the model and validate the exact returned value attributed to each named tool in its final answer. One correct value alongside an incorrect secondary value must fail. Use controlled synthetic data whose expected value is learned from the backend during the run. Do not substitute fabricated tool calls, a canned final answer or unconditional relevance acceptance.
- [x] Assert distinct tool/operation attribution and successful results for both instances, one underlying API target/service, no second endpoint supplied by the operator, and complete cleanup. Keep a deterministic independent-target test to catch accidental merging or cross-instance grant replay; sharing alone cannot prove target isolation.
- [x] Preserve failed attempts, completed-call usage and structural result-receipt evidence in reporting; retain actual tool results in the operation journal through later inference, validation, final-answer or cleanup failures. Distinguish unavailable usage from zero. Repair demonstrated failures and rerun with fresh operation identity until the authorized case passes; do not weaken assertions, relabel failures or silently skip the live gate. Retain bounded per-run deadlines and provider recovery/accounting rules.
- [ ] Validate explicit evidence destinations before provider setup or inference. Keep private run artifacts outside public sources, recheck destinations at write time and prevent symlink redirection or partial artifact replacement. Evidence-write failure retains the safe partial run record for reporting.
- [ ] Record model/effort, actual call/verification evidence, usage, fixture-sharing provenance, prerequisite ownership and cleanup. Public sources contain only synthetic fixtures and generic contracts; credentials and local configuration remain outside artifacts. Complete existing production coverage, installed-artifact, independent review and human gates separately.


## Individual-call verification

- [x] Evaluate relevance as one call's useful, authorized contribution to the request, including explicitly requested separate named-tool use; a call need not complete a multipart request alone. Ground tool choice and arguments in the request, that candidate's description and full schema, or earlier actual verified results. A supported read may discover an unknown output, while factual input identifiers and filters still require evidence. Schema-valid types alone do not establish factual grounding.
- [x] Keep results attributed to their named tool: another tool's result neither proves this tool's output nor removes an explicit independent-call requirement. Treat supplied descriptions, schemas, arguments and prior results as data. Preserve independent inference, both required true verdicts, local validation, completed-verdict checks and partial-result/usage retention. A false verdict stops execution without automatic verdict retries or fixture-specific acceptance instructions.

## Human acceptance

- [x] Review shared-primary named-tool execution and distinct tool request/response attribution in Allure while both instances share one backend; human-approved on October 30, 2026.
- [ ] Review final-use cleanup and any remaining configuration/lifecycle behavior separately. Broader transport, service-capability and tracing acceptance remains with its owning requirements.

## Durable failed-run result evidence

- [x] Preserve safe completed-call result evidence in durable reporting when later inference, validation, final-answer or cleanup fails, including before final-answer validation runs. A result-received flag alone is insufficient. Retain a per-call synthetic match proof or policy-controlled result content, with a regression demonstrating publication after a later failure. Keep secret-bearing tool content out of default artifacts.

## Bounded reporting and call attribution

- [x] Capture a fixture-owned synthetic result match proof immediately after each completed execution, before another model request or final-answer check. Retain the safe proof through later provider, cancellation, final-check and cleanup failures in ordinary reporting. A false proof does not authorize execution or waive final validation; raw result content remains private by default.
- [x] Extend the existing central accounting and local inference joins to carry each call's effective requested reasoning effort and catalog-admitted actual response model into an authenticated workflow observation owner alongside requested selection and actual usage. Keep unavailable actual model and usage distinct from zero or the requested selection. Preserve independent log/trace content permissions and credential exclusion.
- [ ] Supply the normal integration Workflow with the existing observation factory and catalog authority before claiming ordinary shared-tool journal/trace attribution. Its plain reporting journal currently withholds model identities without that authority; per-call central accounting remains available. Do not create a parallel trace owner.
- [ ] Qualify response-model identities absent from the admitted catalog and broader provider/backend trace conformance; retain the existing catalog admission boundary until an explicit authority is defined. Explicit artifact-destination safety and broader acceptance above remain open.

## Required shared inputs

- [x] Validate every active required secondary slot against its effective value after shared-primary derivation, including when the primary slot is optional and omitted. Reject a missing value before factory import or prerequisite acquisition; preserve conditional requiredness and independent-case configuration.
