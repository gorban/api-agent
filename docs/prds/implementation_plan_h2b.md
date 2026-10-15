# One persistent accounting cache per computer

[Shared resources](implementation_plan_h2.md) · [Indexed history](implementation_plan_f3.md) · [Connection lifecycle](implementation_plan_f3b.md).

## Selected cache and coordination

One persistent machine-local SQLite cache serves launches using the same authorized data scope. Keep immutable reporting storage authoritative and reuse delivered indexed history queries and connection ownership; do not regress to arbitrary row-count sampling or rebuild the cache on every launch.

- [ ] Specify a stable machine-local cache location and access permissions, with an explicit migration from existing user-local caches. Do not pool unrelated private account evidence or broaden access merely to share a file.
- [ ] Preserve the current exclusive suite/accounting lock during this phase. Cache transactions do not establish that stateful suites or account quota measurements can execute concurrently.
- [ ] Use short atomic write transactions, idempotent ingestion and conflicting immutable-run rejection. Keep network synchronization, inference and browser waits outside database write transactions.
- [ ] Coordinate schema initialization/migration across processes; handle busy contention with bounded waits/retries and clear diagnostics. Cancellation and process death must release owned connections and transactions without corrupting data.
- [ ] Assess WAL readiness against the actual patched SQLite runtime and filesystem. Verify runtime version, journal behavior, checkpoint ownership, reader/writer contention and backup compatibility before enabling WAL; do not assume enabling a pragma alone makes coordination safe.

## Synchronization and scale

- [ ] Reconcile finalized manifests and accounting incrementally, including late uploads and interrupted synchronization. Reuse a valid cache on startup; reserve full rebuild for explicit recovery or verified invalidation.
- [ ] Download only manifests and accounting for estimates, never screenshots, recordings or other media. Preserve digest verification and source attribution.
- [ ] Keep indexed time-window queries complete at realistic history sizes. Separate the prediction lookback window, currently defaulting to seven days, from durable/cache retention policy; expanding a window must recover eligible history rather than invent missing observations.
- [ ] Define retention, compaction and synchronization cursors separately. Cursor advancement must not lose late evidence; source endpoint/project changes must not mix incompatible histories.
- [ ] Benchmark startup, incremental synchronization, migrations and concurrent readers/writers at representative multi-developer scale. Record bounded memory, query latency, contention and recovery measurements in a linked detail if evidence outgrows this file.
- [ ] Independently review synthetic contention, interruption, stale cursors, schema upgrades and restore failures. Human-review reuse of existing history without unnecessary inference or downloads.
