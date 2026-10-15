# Reasoning effort and conversation model identity

[LLM requirements](implementation_plan_f.md) · [Included provider](implementation_plan_f2.md) · [Frontend integration](implementation_plan_i2.md)

Deliver this contract before integrating the reviewed frontend foundation. Reasoning controls are request semantics, not a promise of hidden reasoning visibility. Existing authentication, inference-spending and acceptance gates remain unchanged.

## Shared selection and configuration

- [x] Declare generic `LLM_REASONING_EFFORT` with normal precedence and enum values empty, `off`, `low`, `medium`, `high`; default empty. Empty omits the control and preserves provider defaults. `off` requests disabled reasoning explicitly; never equate it with omission or silently map it to a positive effort. The enum picker and help describe this distinction.
- [x] Extend the shared request, capability/catalog and worker wire contracts with optional reasoning effort. Validate the selected provider/model/version/API route before transport; an unsupported explicit level fails actionably without inference, account/endpoint/model fallback or silent omission. Provider-specific additional levels remain outside this initial common enum. Capability information can be unknown; do not invent support from a model name.
- [x] Verify documented mappings for each supported transport/model: shared `off` may map to a documented native disabled value, while other explicit levels map only when supported. Configuration/help/discovery do not authenticate merely to enumerate controls. Preserve existing output budgets, cache policy, tools, structured output, continuation and deadline/retry semantics.
- [ ] Define model family and exact catalog version selection through existing admission policy. Preserve requested selection, resolved catalog ID, provider-reported actual model, serving revision when reported and requested effort separately. A mutable alias, unknown version or requested effort is not proof of an actual serving revision/effective effort.
- [ ] Carry this identity unchanged through local/API/stream requests, workflow personas, parallel workers, continuations and retries. Any separately allowed node model override is explicit and recorded; no configuration edit changes an in-flight request. Keep effort in safe request-affecting accounting/history compatibility so unequal effort cohorts do not silently pool.

## Sample conversation UI

- [ ] Add model, version and reasoning pickers to the included frontend in the isolated integration candidate before its source delivery. Populate model/version from the selected provider catalog; distinguish latest/alias from a known exact version. Offer only known supported effort choices or explain unavailable capability. Do not add an alternative discovery loader.
- [ ] Snapshot these choices at Send. Tag every sent user message and its resulting assistant response with model, version and reasoning, distinguishing requested values from actual returned metadata and unknowns. Tags survive history reload, streaming, errors and conversational follow-ups. Changing the pickers affects subsequent sends only; omitted effort is labelled provider default.
- [ ] Extend the shared API/UI envelope compatibly and version it if necessary. Older clients retain omitted-effort behavior; private frontend adapters own their presentation and protocol joins. Do not require a particular external UI to copy the sample implementation.

## Acceptance

- [x] Parameterize omitted/off/low/medium/high across supported synthetic providers, API modes and model versions; verify exact wire mapping, unsupported rejection before calls, explicit precedence, cache/client identity and no fallback. Mutations must detect dropped effort, off-to-omitted coercion and history pooling across incompatible efforts.
- [ ] Verify UI picker validation, stale catalog/version handling, send-time snapshots, per-message tags, concurrent conversations and preserved metadata on partial/failed results. Test source and relocated installed packages, IPC and API streaming with controlled transports.
- [x] Include new request-mapping modules and resolved effort in workflow sampling fingerprints and structural evidence. Preserve prior reviewed measurements unchanged; stale qualification rejects before setup/inference and requires a fresh independently reviewed pilot rather than replacement hashes to retain its cap.
- [ ] Independently review the contract and joined UI extension; require applicable exact unit line/branch coverage, package/license and public tree/index/metadata checks. Human acceptance covers picker choices, default-versus-off and message history. No additional paid inference is needed for the deterministic gate; live capability checks remain separately authorized.

## Base delivery checkpoint

[Implemented core contracts, verification and remaining joins](implementation_plan_f2f1.md). Sample UI pickers/tags and their human acceptance remain open; base completion does not certify the later frontend or application joins.
