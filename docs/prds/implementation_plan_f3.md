# Historical test-cost predictions and immutable pricing

## Contracts and preservation

- [x] Extend optional provider accounting with validated rate snapshots and safe compatibility metadata. Existing providers remain usable with explicit unavailable prices.
- [x] Keep payment amount and quota/payment periods; no allocation multiplier is needed. Provider plan information is optional metadata.
- [x] Attempt one bounded pricing refresh at suite Start without initiating login. Use public catalog prices where supplied; direct API catalog IDs do not supply rates. Parse only unambiguous official standard rates with exact units/token categories/model IDs, distinguishing service tiers. Freeze the suite card; retain dated validated cache or reviewed bundled fallback on refresh failure.
- [x] Attach versioned immutable accounting to finalized Allure manifests: call/test/attempt/user-turn identities, tokens, requested/catalog/actual model identities, explicit unknown serving revision, rate provenance/factors, quota precision/window/reset observations, payment settings and estimator version. Keep raw allocation, list equivalent, advance prediction and stabilized estimate separate. Preserve ten-column CSV and worker/parent credential scrubbing.

## History and estimates

- [x] Add positive TEST_COST_HISTORY_DAYS default 7 with normal precedence. Use transactional idempotent SQLite in the user's standard cache directory, keyed by origin/run UUID/immutable hashes. Import finalized verified manifest/accounting only.
- [x] Synchronize configured S3 history in the picker background, reconciling manifests each time for late uploads. Show cached rows immediately; errors never block selection/execution. Fetch no screenshots/videos/diagnostics; start no storage containers or authentication for predictions.
- [x] Match parameterized identity, file-content fingerprint, pack, provider/route/API/auth/model and explicitly safe request-affecting settings/retry policy. Missing compatibility never matches. Reprice original calls with current card; predict the arithmetic mean of compatible successful runs including retry attempts, with sample count and variation. Each newly finalized compatible run contributes immediately on return to selection. Preserve older artifacts; newly recorded results use estimator version 2.
- [x] Fit summed quota deltas / summed list-price use, including zero deltas, across at least three compatible nonoverlapping complete clean suites. Exclude resets, contamination and missing prices/precision. Require positive cumulative delta and rounding bounds within ±25% of factor. Separate policy/pricing groups; convert using current payment/calendar-month settings.
- [x] Freeze calibration at Start. Before execution use qualified calibrated prediction, else compatible token history at current prices. After execution use actual tokens with frozen calibration, else current quota only with qualified rounding bounds, else list prices. API keys always list-priced. Import current run after finalization, never into its own estimate.

## Presentation and acceptance

- [x] Compact Est. $ in integration/E2E selection and after completed Execution time; hide costs during live execution. Known zero-call tests blank; unknown dash. Deduplicate group overlaps in selected total and label incomplete totals.
- [x] Keep prediction and completed accounting evidence in finalized artifacts, including basis/count/freshness/rounding/variation and original versus derived values. Refresh compatible rows without disturbing search/selection/focus/fixed timing. Per user review, omit the cost-details shortcut and popup; i remains ordinary query text during search.
- [x] Reuse shared helpers for future E2E wiring. Unit tests remain outside cost history/Allure.
- [x] Deterministic tests cover zero/spike/threshold/insufficient/calibration bounds, prices/aliases/missing revisions/retries/overlaps/resets/interrupted recording/late duplicate uploads, malformed/stale/bounded pricing, integrity/privacy and worker boundaries. Independent adversarial reviews, all applicable units at exact 100% lines/branches, wheels and public-boundary checks.
- [ ] Human review through deterministic replay and existing Allure history. No additional paid inference is needed for this batch.

Implementation evidence: 4,738 combined unit tests pass with exact per-file 100% line/branch coverage; both applicable wheels, deterministic picker/completed replay and public-boundary checks pass. Independent adversarial reviews and focused mutation controls cover the repaired cases. Human acceptance remains pending; future E2E executor wiring is a separate task.

## Live-history verification follow-up

- [x] Resolve ordinary base/resource-pack test-file fingerprints through trusted collected repository identities, as well as explicit provider suite roots. Use identical resolution for immutable recording, initial predictions and background refresh; unknown repositories remain unmatched.
- [x] Record integration's actual zero-retry policy in accounting compatibility even when the E2E retry setting is inactive. Keep the execution configuration unchanged.
- [x] Verify that Back to selection immediately rereads a newly finalized local history record, preserving selections and updating predictions.
- [x] Verify before/after quota snapshots and zero deltas in finalized usage and accounting artifacts across passed, failed and interrupted runs. Unavailable snapshots stay null, distinct from observed zero.
- [x] Isolate unit recording fixtures from the real user history cache; unit runs must not leave synthetic integration observations there.
- [ ] Verify supported selected-subscription quota retrieval and expose a safe unavailable reason when its optional endpoint rejects access. Do not substitute another account's credentials or infer zero usage from missing snapshots.
- [ ] Expand exact pricing mappings only when official model/context/tier evidence is unambiguous. Missing or ambiguous refreshed rates remain unavailable.

Follow-up verification: 4,750/4,750 combined unit tests pass at exact per-file 100% lines and branches; focused history/recording checks, mutation controls, wheel and public-boundary audit pass. Live quota and exact model pricing remain unverified as described above.


## Completed token totals and individual test details

- [x] Show actual input, output, cached-input, cache-write, reasoning and total token counts after integration/E2E completion in shared interactive and noninteractive output. Cached/reasoning counts are subsets, not additions to input/output totals. Distinguish missing measurements from zero, retaining reported lower bounds when other calls lack usage.
- [x] Put finalized per-test estimated cost, actual tokens and execution time before the original failure details. Enter also opens passing-test details when the current run or compatible history contains any tokens; show a green PASSED status when no error details apply. Reuse selectable text, copying, scrolling and return-to-results behavior without adding a shortcut.
- [x] Retain validated upstream usage on inference failures through worker/parent error transport, preserving the original safe rejection and bounds. Retain completed-call usage when later assertion or accounting work fails. Final cost attribution still uses the frozen suite/history estimator; unavailable upstream usage or prices stay explicit.
- [x] Keep immutable accounting, ten-column CSV, credential scrubbing and unit-test exclusion unchanged. Use controlled serial/parallel/DEBUG recording and headless TUI proofs; reuse presentation when the future E2E runner is connected.
- [x] Append an em dash and the scoped dollar contribution to each displayed token count, using the finalized test/suite estimate scaled by frozen per-call category prices. Input includes cached input; output includes reasoning. Preserve unknown categories as unavailable, record the derived category allocations separately in Allure, and include retries and measured failures without altering original evidence or CSV columns.
- [x] Present tokens as newline-separated indented rows, after Cost basis in suite output and last before status/diagnostics in test details. Label the total as input + output for this run. Label the completed execution This test cost / This suite cost, calculated from measured usage, from an indented Historical average comparison using frozen advance predictions; missing or incomplete historical prices remain unavailable.
- [ ] Human acceptance: inspect the completed suite totals and Enter on a token-bearing passed test or failure, including copy and return navigation. No additional paid inference is needed for deterministic acceptance.

Token/details verification: **4,813/4,813** combined unit tests (4,627 base + 186 pack) pass with exact per-file **100% line and branch coverage**. Controlled serial/parallel/DEBUG recording, passing/failing/skipped headless result views, lower-bound accounting and three isolated mutation controls pass. Both wheels, whitespace and the hash-bound public audit pass. Human acceptance remains pending.

Category-cost follow-up: **4,840/4,840** combined unit tests (4,654 base + 186 pack) pass with exact **100% per-file line/branch coverage** across 132 measured files. Frozen prices, calibrated allocations, long-context factors, multiple models/retries, known zero versus unavailable categories, scope-specific TUI output and serial/parallel/DEBUG Allure preservation are verified. Two isolated mutations detect lost estimate scaling and double-counted token subsets. Both wheels and the public-boundary audit pass; human acceptance remains pending.

Layout/comparison follow-up: **4,842/4,842** combined unit tests (4,656 base + 186 pack), exact **100% per-file line/branch coverage**, both wheels and public-boundary audit pass. Shared suite/detail layout, current execution versus frozen historical comparison, missing/partial/zero history and offline replay navigation are verified. An isolated mutation detects substituting current costs for historical predictions. No paid inference or real history changes; human acceptance remains pending.


## Account attribution and history-window follow-up

Current quota calibration separates account windows; compatible token histories may already span accounts. Shared cross-account quota calibration is not implemented. These follow-ups preserve original measurements and require focused acceptance before enabling pooling.

- [ ] Extend the versioned accounting contract with a provider-scoped, stable pseudonymous quota-allocation identity, separate from reset/window observations. It must survive developer-machine, password-manager-path and user-index changes without exporting credentials, email addresses or raw account identifiers. Unknown allocation identity cannot establish a like-for-like quota match.
- [ ] Detect overlapping quota observations within the same allocation and reset window. Independent allocations may run concurrently. Preserve exclusion of reset-crossing, incomplete, contaminated and unknown-precision observations.
- [ ] Handle coarse quota measurements across verified continuous usage spans within one allocation/reset window. Do not add independent endpoint uncertainty for every short run when observations can be proven shared; never bridge unmeasured activity, resets or gaps by assumption. Test many zero-delta runs followed by quota ticks, shared versus independent boundaries and ±25% qualification. Preserve raw observations and separately derived fits.
- [ ] Qualify each allocation's history before pooling compatible contributions. Weight by list-priced token usage, preserving input/output/cache/model differences, rather than raw token totals or equal weight per developer. Combine only compatible pricing and payment policies; retain per-allocation evidence and aggregate rounding bounds.
- [ ] Remove the cache's arbitrary pre-filter record cap. Retain older immutable records and select the requested history window before any result bound, using normalized timestamps and an index where appropriate. Expanding TEST_COST_HISTORY_DAYS must recover older cached and remote observations without rebuilding the cache.
- [ ] Verify changing TEST_COST_HISTORY_DAYS through configuration rebuilds predictions before display, including Back to selection, late background synchronization and stale-result suppression. Picker refresh uses a current clock; suite Start continues freezing its estimator. Storage reconciliation must report incomplete retrieval rather than silently claiming complete history.


## Quota reset during execution

- [x] Detect changed allocation/reset windows, reset-boundary crossings and decreasing used-quota observations. Preserve before/after observations and the unavailable reason; quota deltas and raw quota-derived suite/test costs remain null rather than negative.
- [x] Continue displaying token-based completed estimates using measured calls and the frozen valid historical calibration, or list prices when calibration is unavailable. Keep the historical average separate. Show a reset/change notice in shared suite output and individual test details.
- [x] Exclude the entire reset-crossing run from future token predictions, account binding, quota calibration and overlap-based effects on other runs. Retain immutable Allure and SQLite evidence; reopening, synchronization and cache re-import cannot re-enable it. Missing quota observations alone remain distinct from a detected reset.
- [x] Verify changed windows with both lower and higher ending percentages, unchanged metadata with a decreasing percentage, exact reset boundaries, null raw allocations, positive token estimates, preserved Allure attachments and deterministic cache reopening. Use isolated controls to prove the prediction and raw-allocation guards reject their specific cases.
- [ ] Human acceptance: inspect the reset notice, null quota delta/raw costs and retained estimated costs through deterministic replay; no additional paid inference is required.
