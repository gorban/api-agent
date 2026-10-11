# Historical test-cost predictions and immutable pricing

## Contracts and preservation

- [x] Extend optional provider accounting with validated rate snapshots and safe compatibility metadata. Existing providers remain usable with explicit unavailable prices.
- [x] Keep payment amount and quota/payment periods; no allocation multiplier is needed. Provider plan information is optional metadata.
- [x] Attempt one bounded pricing refresh at suite Start without initiating login. Use public catalog prices where supplied; direct API catalog IDs do not supply rates. Parse only unambiguous official standard rates with exact units/token categories/model IDs, distinguishing service tiers. Freeze the suite card; retain dated validated cache or reviewed bundled fallback on refresh failure.
- [x] Attach versioned immutable accounting to finalized Allure manifests: call/test/attempt/user-turn identities, tokens, requested/catalog/actual model identities, explicit unknown serving revision, rate provenance/factors, quota precision/window/reset observations, payment settings and estimator version. Keep raw allocation, list equivalent, advance prediction and stabilized estimate separate. Preserve ten-column CSV and worker/parent credential scrubbing.

## History and estimates

- [x] Add positive TEST_COST_HISTORY_DAYS default 7 with normal precedence. Use transactional idempotent SQLite in the user's standard cache directory, keyed by origin/run UUID/immutable hashes. Import finalized verified manifest/accounting only.
- [x] Synchronize configured S3 history in the picker background, reconciling manifests each time for late uploads. Show cached rows immediately; errors never block selection/execution. Fetch no screenshots/videos/diagnostics; start no storage containers or authentication for predictions.
- [x] Match parameterized identity, file-content fingerprint, pack, provider/route/API/auth/model and explicitly safe request-affecting settings/retry policy. Missing compatibility never matches. Reprice original calls with current card; predict the median compatible successful run including retry attempts, with sample count and variation.
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
