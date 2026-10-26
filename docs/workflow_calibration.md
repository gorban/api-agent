# Refreshing the permanent workflow's coverage bounds

Calibration is maintenance, not a normal integration test. The mandatory coverage test uses a reviewed model-specific baseline packaged with the fixture. It never starts a pilot, skips an unsupported configuration or silently changes provider/model.

## When to refresh

Review a fresh pilot when the selected model or serving revision changes; when executable prompts, schemas, routing/repair logic, original input or structured request semantics change; or when relevant inference/runtime dependencies change. Operational timeout/retry changes need explicit compatibility review or fresh measurement. Edits to unrelated navigation or reporting do not establish a different model distribution.

Read the fingerprint diagnostic rather than copying a different model's budget. Preserve the observed settings, trial records, source/dependency identity and methodology with every reviewed baseline. A wider timeout must not rewrite what an older pilot actually used.

Stale diagnostics list changed categories such as `effective_settings`, `sources` or `dependency_versions`, without disclosing configured values. A first-attempt failure at this preflight makes no graph inference calls; DEBUG does not bypass qualification. Returning settings to an older profile cannot repair changed source bindings. Material sampling changes require a fresh pilot and independent review before replacing the packaged entry; the command below consumes real inference usage. The narrowly reviewed presentation-only carry-forward below does not make new measurements.

## Procedure

1. Freeze the intended fixture, provider and configuration. Use the standard selected GPT subscription and model policy; finish other suites using the same account first.
2. Invoke the explicit maintenance command with a new candidate filename and a preselected sample size, 60 graphs for the expanded policy, fixed before execution and never more than 60. From the checkout, using its installed development environment:

   ```sh
   .venv/bin/python -m api_agent.workflows.calibrate --graphs 60 \
     --candidate artifacts/workflow/pilot-new.json --no-std-in
   ```

   Normal provider settings still come from `.env`, environment and typed command-line overrides. This command owns its maintenance operation; it starts no UI, API or reporting services. It prints graph/call progress and retains a separate structural progress file on failure. Existing candidate, key, reservation or progress files are not overwritten.
3. Inspect progress and retained evidence. Every graph must succeed. Malformed output, exhausted guards, cancellation or provider failure stops the batch and prevents finalized calibration. Keep that failure; do not cherry-pick its successful prefix into a passing pilot.
4. Review contiguous measurements, routing and retry frequencies, actual model identity, configuration/source compatibility and statistical claims. Recompute simultaneous one-sided exact 95% lower bounds using alpha=0.05/target count. Find the minimum runs with sum((1-p_lower)**runs) <=0.001, then add four runs of headroom. Reject a buffered cap exceeding the operating ceiling of 60; never clamp it. Record the minimum, fixed margin and probability bounds at both the minimum and buffered cap. Normal coverage still stops as soon as all six nodes, all three root routes (`skip`, `next_a`, `next_b`) and at least one observed internal component retry are hit.
5. Verify the finalized candidate through `read_calibration`, which checks its private receipt key and returns the report without `receipt`. Recompute its statistical claims. For example, export a new unsigned review file; this does not update the installed baseline:

   ```sh
   .venv/bin/python - artifacts/workflow/pilot-new.json reviewed-entry.json <<'PY'
   import json
   from pathlib import Path
   import sys
   from api_agent.workflows.evidence import read_calibration
   from api_agent.workflows.sampling import DECLARED, validate_calibration

   report = read_calibration(Path(sys.argv[1]))
   assert 'receipt' not in report
   validate_calibration(report, DECLARED, {})
   with Path(sys.argv[2]).open('x') as output:
       json.dump(report, output, sort_keys=True, indent=2)
       output.write('\n')
   PY
   ```

   This verifies provenance and arithmetic, not current compatibility or independent approval. Compare every current fingerprint field using `fingerprint(selection, resolved_config, root=context.base_root, owner_root=context.owner_root)` with the same standard configuration/context. A changed sampling source or setting requires fresh qualification, not silently replacing its digest. Independently established presentation-only equivalence follows the separate exception below.
6. Independently review the unsigned entry. Keep credentials, absolute/local paths, signing keys, receipt fields and raw producer content outside the public artifact. The candidate and `.receipt-key` remain private. After approval, manually replace or add the appropriate entry in `src/api_agent/workflows/fixture_baselines.json` under `{"schema": 1, "reports": [...]}`. Preserve another model's entry; reject duplicate entries for the same reviewed selection. Neither the maintenance command nor this export overwrites checked-in bounds. Retain old evidence separately when replacing an entry.
7. Run the normal node-coverage integration test with the same supported settings. It stops only when every expanded target has been observed; execution defects fail immediately and missing targets at the cap fail. Preserve its actual stopping reason and usage.

The 99.9% target is conditional coverage within a cap under independent, unchanged graph trials. It does not promise 99.9% end-to-end availability. A failed maintenance batch can contain useful diagnostic measurements without qualifying as finalized evidence.

[Owning requirements](prds/implementation_plan_b2b1.md).

## Presentation-only source equivalence

Changing only the inert browser envelope or saved diagram/navigation can change raw declaration hashes without changing executable Markdown. Maintainers may retain reviewed observations through an explicitly authorized, independently reviewed carry-forward. Preserve the original payload, fingerprint and measurements at their original commit; prove canonical executable declarations, native/prompt/schema/configuration bytes, graph behavior, loader ownership, sampling code and effective profile are unchanged. Record exact old/current identities and promote only the reviewed current source binding. Do not imply a fresh pilot occurred or edit the observed records, bounds, minimum or margin.

This is not an automatic fingerprint bypass or an end-user repair command. Strict current-fingerprint and statistical validation remain in force. Unproven drift stays stale; model family/version, reasoning, effective requests, graph execution or relevant dependency changes require fresh qualification. See the [current reviewed carry-forward](prds/implementation_plan_b2b1a5.md).


## Expanded coverage qualification

Fingerprint version 4 and `nodes_routes_internal_retry_v2` require ten targets: six successful nodes, three root routes and an actual internal component attempt greater than one. Pilot records retain ordered component attempts and minimal emitted structural proof: successful GRAPH/component/node scope lifecycles, bound node visits and actual root edges. Scope identities cannot repeat across independent trials. The route must agree with taken edges and successful nodes, and each attempt must belong to its observed component and graph; repeated independent graphs do not constitute a retry. Every record must agree with its route and successful node set. The simultaneous exact binomial calculation and union bound cover all ten targets with the same 95% confidence, 99.9% target, fixed four-run margin and maximum of 60.

Earlier node-only reports and their cap of 22 remain historical evidence. They cannot qualify this policy, even with unchanged settings or a presentation-only equivalence receipt. A 20-graph pilot cannot qualify all three route targets within this policy’s cap and fixed margin, even under the best route balance. Use a preselected 60-graph pilot; it can still fail to qualify, especially for a rare retry or route. Do not extend a pilot after inspecting its outcome. Retain its progress measurements, report the qualification failure and do not promote or silently enlarge its cap. Do not change prompts or model reasoning merely to obtain a qualifying distribution.
