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
- [x] Add i Cost details using selectable-text/copy popup; search still treats i as query text. Include basis/count/age/freshness/rounding/variation and completed original versus derived values. Refresh compatible rows without disturbing search/selection/focus/fixed timing.
- [x] Reuse shared helpers for future E2E wiring. Unit tests remain outside cost history/Allure.
- [x] Deterministic tests cover zero/spike/threshold/insufficient/calibration bounds, prices/aliases/missing revisions/retries/overlaps/resets/interrupted recording/late duplicate uploads, malformed/stale/bounded pricing, integrity/privacy and worker boundaries. Independent adversarial reviews, all applicable units at exact 100% lines/branches, wheels and public-boundary checks.
- [ ] Human review through deterministic replay and existing Allure history. No additional paid inference is needed for this batch.

Implementation evidence: 4,738 combined unit tests pass with exact per-file 100% line/branch coverage; both applicable wheels, deterministic picker/completed replay and public-boundary checks pass. Independent adversarial reviews and focused mutation controls cover the repaired cases. Human acceptance remains pending; future E2E executor wiring is a separate task.
