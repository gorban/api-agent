# Workflow conversion contract freeze

[Resource format](implementation_plan_b1a.md) · [Conversion and equivalence](implementation_plan_b1b.md)

These generic contracts must be reviewed and versioned before dependent format conversion. They preserve existing accepted behavior; they do not authorize inference, qualification spending or application acceptance.

- [ ] Select one complete graph or one explicitly composed graph through an immutable selection record containing graph identity, declared owner/root, entry and ordered stable-ID replacements. Reject ambiguity, conflicting replacements, unknown IDs and incompatible types before effects. Omitted replacements retain base behavior; setting overrides and directory collisions never merge graphs.
- [ ] Resolve entry-relative resources through an immutable declared-root map and explicit resource kinds. Capture the complete declaration, prompt, schema, configuration, callable-source, asset and license closure independently of cwd. Reject traversal, symlink escapes, missing or ambiguous links and undeclared roots.
- [ ] Freeze a versioned plain-data compiler representation with stable graph/component/node/edge IDs, typed state/reducers and bindings, normal/resume entries, ordered prompts, conditions, scoped retries/deadlines and streaming. Diagram metadata remains nonexecuting. Reject unsupported executable semantics rather than silently dropping them or evaluating document expressions.
- [ ] Validate typed callable roles, inputs/outputs, owning module and source identity before invocation. Preserve async/sync cancellation, owned draining and shared enclosing timeout scopes. Native code changes require a managed restart when safe replacement is unavailable.
- [ ] Capture one coherent immutable definition per invocation, including resource contents, resolved replacements, request-affecting configuration, parser/compiler/binding versions and dependencies. Pin retries and resumed executions to that definition. Distinguish execution identity from sampling qualification identity; reject mixed or stale input and renew reviewed qualification when bound sampling inputs change.
- [ ] Map every existing runtime/CLI/resource/diagram consumer and graph integration test to its replacement and deterministic equivalence assertion. Each distinct integration graph owns a root flow.md.html fixture hierarchy and exercises the real loader; dynamic updates use isolated complete copies. Preserve branch/loop/join, prompts, state, approval/resumption, retry/error/cancellation, streaming and timeout assertions.
- [ ] Prove wheel and deployment-artifact relocation from unrelated directories with declared dependencies, no original development checkout, correct callable ownership, navigation/assets/licenses and complete fingerprints.
- [ ] Reserve versioned origin-side definition/turn/invocation/visit/attempt/call identity, parent/cause/branch identity and every actual transition, including implicit/error/nested edges. Preserve privacy before queues and artifacts and explicit missing/partial evidence. This reserves later inspection seams without implementing its UI.

Independent contract review precedes implementation. Conversion completion still requires the parent equivalence, packaging, qualification, staged-check, independent review and human acceptance gates.

- [ ] Freeze the [immutable production profile](implementation_plan_h4.md) alongside mutable development behavior: startup-validated definition identity, once-per-process plan reuse, fresh execution state and no implicit definition reload. Preserve the same compiled semantics in source, installed and image execution; build-time generation remains a later measured choice.

- [ ] Apply the [first-release shared application graph and scenario coverage](implementation_plan_b1b3.md) contract, keeping the shipped Run local/E2E graph separate from qualification fixtures.

- [ ] Apply the [versioned nested declaration grammar](implementation_plan_b1b2.md) and its immutable runtime adapter; unsupported richer executable semantics remain explicit dependent work.

- [ ] The inference-free workflow CLI must distinguish raw declaration checks from selected-pack checks. `--resource-pack-dir` uses the same explicit graph root, ordered replacements and immutable definition as pack loading for both validation and diagram projection; reject combinations with raw entry/demo/root selectors. `--base-root` selects the corresponding application or demo entry independently of working directory. Verify source and relocated-wheel behavior, including graph identity and a replacement that changes the selected definition.
