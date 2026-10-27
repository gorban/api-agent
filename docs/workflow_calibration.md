# Explicit legacy workflow calibration maintenance

Normal workflow integration coverage no longer requires calibration, packaged
baselines or physical bundled-provider identity. It draws fresh version-4 UUIDs,
filters duplicate route/readiness combinations before inference and executes all
ten reachable cases once. The real LLM must honor the selected UUID decisions;
original schemas, format/guard checks and emitted GRAPH proof remain mandatory.
The whole-test LLM_WORKFLOW_TEST_TIMEOUT_SECONDS default is 600 seconds, starting
before provider setup/catalog. LLM_WORKFLOW_TEST_MAX_OUTPUT_TOKENS is optional;
the Bedrock workflow profile supplies 1024, and explicit test overrides win.

The explicit calibration command and statistical readers below remain available
for historical maintenance. Their original reports retain their original scopes.
They never become a prerequisite for current normal integration coverage, never
silently change the selected provider/model, and perform paid inference only
under separately approved maintenance scope.

## When to refresh

Review a fresh pilot when the selected model or serving revision changes; when executable prompts, schemas, routing/repair logic, original input or structured request semantics change; or when relevant inference/runtime dependencies change. Operational timeout/retry changes need explicit compatibility review or fresh measurement. Edits to unrelated navigation or reporting do not establish a different model distribution.

Read the fingerprint diagnostic rather than copying a different model's budget. Preserve the observed settings, trial records, source/dependency identity and methodology with every reviewed baseline. A wider timeout must not rewrite what an older pilot actually used.

Stale diagnostics list changed categories such as `effective_settings`, `sources` or `dependency_versions`, without disclosing configured values. A first-attempt failure at this preflight makes no graph inference calls; DEBUG does not bypass qualification. Returning settings to an older profile cannot repair changed source bindings. Material sampling changes require a fresh pilot and independent review before replacing the packaged entry; the command below consumes real inference usage. The narrowly reviewed presentation-only carry-forward below does not make new measurements.

## Procedure

1. Freeze the intended fixture, selected provider and authentication, region and configuration. Use the same effective workflow profile as the normal coverage test; [provider-owned test profiles](integration_test_profiles.md) can select a different model/API from ordinary application defaults. Preserve explicit test-specific overrides and model admission. Finish other suites using the same account first.
2. Invoke the explicit maintenance command with a new candidate filename and a preselected sample size, 60 graphs for the expanded policy, fixed before execution and never more than 60. From the checkout, using its installed development environment:

   ```sh
   .venv/bin/python -m api_agent.workflows.calibrate --graphs 60 \
     --candidate artifacts/workflow/pilot-new.json --no-std-in
   ```

   Normal provider settings still come from `.env`, environment and typed command-line overrides. This command owns its maintenance operation; it starts no UI, API or reporting services. It prints graph/call progress and retains a separate structural progress file on failure. Existing candidate, key, reservation or progress files are not overwritten.
3. Inspect progress and retained evidence. Every graph must succeed. Malformed output, exhausted guards, cancellation or provider failure stops the batch and prevents finalized calibration. Keep that failure; do not cherry-pick its successful prefix into a passing pilot.
4. Review contiguous measurements, routing and retry frequencies, actual model identity, configuration/source compatibility and statistical claims. Recompute simultaneous one-sided exact 95% lower bounds using alpha=0.05/target count. Find the minimum runs with sum((1-p_lower)**runs) <=0.001, then add four runs of headroom. Reject a buffered cap exceeding the independently selected `LLM_WORKFLOW_TEST_MAX_GRAPHS` ceiling; never clamp it. The generic/GPT default remains 60, while the included Bedrock workflow profile selects 120. Only integer ceilings from 1 through 120 are supported, and an explicit test override remains authoritative. This changes the legacy bounded-run budget, not the predeclared pilot sample size or statistical targets. Record the minimum, fixed margin and probability bounds at both the minimum and buffered cap. Historical normal coverage stopped as soon as all six nodes, all three root routes (`skip`, `next_a`, `next_b`) and at least one observed internal component retry were hit; the current test additionally requires every reachable route/readiness case.
5. Verify the finalized candidate through `read_calibration`, which checks its private receipt key and returns the report without `receipt`. Recompute its statistical claims. For example, export a new unsigned review file; this does not update the installed baseline:

   ```sh
   .venv/bin/python - artifacts/workflow/pilot-new.json reviewed-entry.json 120 <<'PY'
   import json
   from pathlib import Path
   import sys
   from api_agent.workflows.evidence import read_calibration
   from api_agent.workflows.sampling import DECLARED, coverage_ceiling, validate_calibration

   report = read_calibration(Path(sys.argv[1]))
   assert 'receipt' not in report
   # Independently supply the resolved/operator-approved ceiling, never a report field.
   trusted_ceiling = coverage_ceiling(int(sys.argv[3]))
   validate_calibration(report, DECLARED, {}, ceiling=trusted_ceiling)
   with Path(sys.argv[2]).open('x') as output:
       json.dump(report, output, sort_keys=True, indent=2)
       output.write('\n')
   PY
   ```

   This example explicitly supplies 120 for the included Bedrock workflow profile. Supply 60 for the generic/GPT default, or the same supported explicit ceiling resolved before your pilot; never derive this argument from saved report metadata. `store_calibration` likewise requires an explicit trusted ceiling when it differs from 60, and the maintenance command supplies its resolved value. This verifies provenance and arithmetic, not current compatibility or independent approval. Compare every current fingerprint field using `fingerprint(selection, resolved_config, root=context.base_root, owner_root=context.owner_root)` with the same standard configuration/context. A changed sampling source or setting requires fresh qualification, not silently replacing its digest. Independently established presentation-only equivalence follows the separate exception below.
6. Independently review the unsigned entry. Keep credentials, absolute/local paths, signing keys, receipt fields and raw producer content outside the public artifact. The candidate and `.receipt-key` remain private. After approval, manually replace or add the appropriate entry in `src/api_agent/workflows/fixture_baselines.json` under `{"schema": 1, "reports": [...]}`. Preserve another model's entry; reject duplicate entries for the same reviewed selection. Neither the maintenance command nor this export overwrites checked-in bounds. Retain old evidence separately when replacing an entry.
7. Current normal node-coverage integration is separate from this statistical maintenance. It requires all ten reachable cases and actual structural targets under its whole-test deadline; it does not read the promoted baseline or statistical cap. Execution defects fail immediately without redrawing to hide failure. Preserve actual stopping reason and usage.

The 99.9% target is conditional coverage within a cap under independent, unchanged graph trials. It does not promise 99.9% end-to-end availability. A failed maintenance batch can contain useful diagnostic measurements without qualifying as finalized evidence.

[Owning requirements](prds/implementation_plan_b2b1.md).

## Presentation-only source equivalence

Changing only the inert browser envelope or saved diagram/navigation can change raw declaration hashes without changing executable Markdown. Maintainers may retain reviewed observations through an explicitly authorized, independently reviewed carry-forward. Preserve the original payload, fingerprint and measurements at their original commit; prove canonical executable declarations, native/prompt/schema/configuration bytes, graph behavior, loader ownership, sampling code and effective profile are unchanged. Record exact old/current identities and promote only the reviewed current source binding. Do not imply a fresh pilot occurred or edit the observed records, bounds, minimum or margin.

This is not an automatic fingerprint bypass or an end-user repair command. Strict current-fingerprint and statistical validation remain in force. Unproven drift stays stale; model family/version, reasoning, effective requests, graph execution or relevant dependency changes require fresh qualification. See the [current reviewed carry-forward](prds/implementation_plan_b2b1a5.md).


## Expanded coverage qualification

Fingerprint version 4 and `nodes_routes_internal_retry_v2` require ten targets: six successful nodes, three root routes and an actual internal component attempt greater than one. Pilot records retain ordered component attempts and minimal emitted structural proof: successful GRAPH/component/node scope lifecycles, bound node visits and actual root edges. Scope identities cannot repeat across independent trials. The route must agree with taken edges and successful nodes, and each attempt must belong to its observed component and graph; repeated independent graphs do not constitute a retry. Every record must agree with its route and successful node set. The simultaneous exact binomial calculation and union bound cover all ten targets with the same 95% confidence, 99.9% target, fixed four-run margin. Historical normal coverage used the independently configured ceiling (default 60, supported up to 120); explicit calibration pilots still contain at most 60 independent graphs. The current normal test uses finite case coverage instead.

Earlier node-only reports and their cap of 22 remain historical evidence. They cannot qualify this policy, even with unchanged settings or a presentation-only equivalence receipt. A 20-graph pilot cannot qualify all three route targets within the default 60-graph operating ceiling and fixed margin, even under the best route balance. Use a preselected 60-graph pilot; it can still fail to qualify, especially for a rare retry or route. Do not extend a pilot after inspecting its outcome. Retain its progress measurements, report the qualification failure and do not promote or silently enlarge its cap. A separately approved larger operating ceiling requires a fresh predeclared pilot under the new fingerprint; earlier failed measurements are retained unchanged and are not pooled or relabeled. Do not change prompts or model reasoning merely to obtain a qualifying distribution.
