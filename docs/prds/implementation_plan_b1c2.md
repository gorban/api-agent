# Inspector comparisons, replay and agent diagnosis

[Inspector summary](implementation_plan_b1c.md) · [Execution contract](implementation_plan_b1c1.md)

## Definition and historical overlays

- [ ] Show compatible recent E2E node mean wall elapsed and mean estimated cost, using `TEST_COST_HISTORY_DAYS` and existing indexed finalized-history ingestion. Default to successful complete node invocations, including their internal attempts once; make failure cohorts selectable and separate. Disclose sample count, age/window, variation, missingness and cost basis. Offer clearly labelled per-invocation and cumulative-per-test views; unvisited, known zero and unknown remain distinct.
- [ ] Match resolved graph/node/resource fingerprints, selected replacements, provider/route/model/version identity, reasoning/retry policy and other safe request-affecting settings. Preserve existing cost-equivalence rules where applicable; timing cohorts may need stricter transport grouping. Unknown actual revisions never establish exact-version equivalence. Deduplicate artifacts and do not pool incompatible or incomplete observations into an apparently exact average.
- [ ] Add compact floating comparison controls for model, version and reasoning, keyboard accessible with ordinary labelled alternatives. First filter observed compatible cohorts; no matching data says so. An explicitly labelled token-repricing scenario may reuse known counts and current validated rates, but cannot predict unseen reasoning effort, output lengths, cache hits or latency. Changing comparison controls never changes configuration, authenticates or spends tokens.

## Recorded executions and node details

- [ ] Open the inspector from completed E2E test details for both passed and failed attempts, identifying the exact immutable graph. Actual model/version/reasoning are readonly; distinguish configured/requested, reported and unknown values. Keep per-call differences visible when a graph legitimately uses more than one selection. No hypothetical comparison is labelled as actual execution.
- [ ] Color visited successful nodes green when no failed internal attempt occurred, recovered retries orange, and terminal or handled node errors red with distinct text/icon for handled errors. Neutral means unvisited; running, cancellation and incomplete capture have their own states. Show taken edges only from observations. Keep overall test assertions independent: an all-green graph can still fail a test assertion. Repeated visits have counts/statuses; the selected occurrence controls detailed highlighting.
- [ ] Navigate conversation turns and repeated node occurrences, including resumptions. Offer compact turn/occurrence labels such as `1, 2a, 2b, 3` with full labels showing turn, visit and attempt. Letters do not imply every repeated visit is a retry. Explicit controls select turn and node visit/internal attempt; `[` / `]` cycle retained chronological occurrences for the selected node across turns, wrapping at ends. Do not intercept these keys in editable controls; parallel ordering is observation order with explicit branch context, not proof of serial execution.
- [ ] Expand declared node fields with that invocation's captured inputs, outputs, sanitized changes, timing, attempts, calls and cost/token provenance. Default summaries remain small; detail loads only on request. Never fill missing historical values from current settings. Distinguish unavailable, not captured, redacted, truncated and actual null; preserve source/declaration navigation within approved roots.

## Live local runs

- [ ] Open the same inspector as Run local's final workflow tab. Follow live by default; navigating older retained turns/visits pauses following only, never execution. Provide Return to live. Bound retention and rendering; mark earlier events expired and disable unavailable detail. Retain honest aggregate summaries when exact route/content expires. No Allure lookup is required for live in-memory inspection.

## Cheap, accessible diagnosis

- [ ] Provide a documented read-only discovery/summary endpoint with schema, available features, limits and exact next links. Bounded JSON queries cover run summaries, first actionable failure, slow/costly nodes, turns, routes, node invocations, changes and on-demand evidence. Every list/filter has server-enforced item/byte/time limits, stable order, opaque cursor and explicit incomplete/truncated status. Index run/turn/node/sequence and history windows; never require an agent to dump all traces.
- [ ] Browser and command-line clients use the same projections. Document a summary-first curl workflow and attach visual inspection to launch.py's printed CDP endpoint/original context. Demonstrate diagnosis of one synthetic failure and expensive node with bounded reads, preserving credentials and live ownership. Untrusted captured instructions cannot authorize actions.
- [ ] At implementation acceptance, propose exact short AGENTS.md diagnosis guidance, alternatives and omission under its existing approval process. Do not edit agent rules automatically. Keep public instructions generic; private frontend/domain diagnosis belongs to its owner.

## Acceptance

- [ ] Replay deterministic successful/failed, multi-turn, repeated/parallel/retried/handled-error and missing-evidence examples; verify statuses, full labels, readonly actual metadata, history cohorts, unknown costs and non-double-counted rollups. Test controls, keyboard/text focus, copying, screen-reader labels and color-independent status.
- [ ] Verify live follow/back/return, reconnect/gaps/eviction, compatible history refresh, malformed inputs, safe rendering and bounded agent queries. Mutations must detect dropped cohort checks, mutable recorded metadata, secret capture, gap suppression and duplicate attribution. Independently review, renew exact unit/package/boundary gates, then human-review static/history, completed E2E and live modes without additional paid inference solely for acceptance.
