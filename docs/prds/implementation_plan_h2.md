# Shared resources across launches

[Infrastructure](implementation_plan_h.md) · [Reporting](implementation_plan_d.md) · [Cleanup](implementation_plan_l.md).

## Decisions and dependencies

Use fixed configured UI and Django ports so bookmarks remain valid. A conflicting listener must fail clearly; do not silently select another port. An additional launch guard is needed only if existing bind failures cannot explain the conflict clearly.

Reporting storage is a persistent shared service with a canonical owner independent of individual checkouts. Launches publish immutable runs; a separate lifecycle owner stops managed containers after the last session and active operation release, retaining volumes. Select the backend by URL alone; local ownership records are internal and custom/production endpoints grant no container-administration rights. A healthy listener never permits adoption.

Select one persistent accounting cache per computer. Use transactional coordination rather than independent launch copies. Retain the current exclusive suite/accounting lock initially; parallel suite execution and account-wide quota attribution remain a separate unapproved phase.

Build on delivered [indexed accounting history](implementation_plan_f3.md) and [cache connection lifecycle](implementation_plan_f3b.md). Durable S3 results remain authoritative; the SQLite cache is a reusable index. A prediction lookback window does not define data-retention policy.

## Remaining work

- [ ] Implement and independently review [shared storage and fixed ports](implementation_plan_h2a.md), with [session lifecycle, persistent Jaeger storage and management](implementation_plan_h2a2.md) first.
- [ ] Implement and independently review [persistent cache coordination and synchronization](implementation_plan_h2b.md).
- [ ] Plan and verify [other resource ownership and migration](implementation_plan_h2c.md).

Record implementation measurements and acceptance evidence in the relevant child. These decisions establish requirements, not completed runtime behavior or permission to reset existing data.
