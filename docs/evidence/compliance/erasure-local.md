# Right to erasure — LOCAL verification

PDF §11 requires a right-to-erasure flow; §12 requires it tested. This file records a
real run of `batch erase-driver` against the running local stack, and what a reader can
still see afterwards.

**LOCAL run on a developer laptop.** The numbers here are counts of rows and keys, not
performance figures.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-29 |
| Commit | recorded by the commit that adds this file |
| Command | `uv run python -m batch erase-driver --tenant tenant-demo --driver-id <id> --requested-by dpo@example.test` |
| Stores | PostgreSQL 16.15, MongoDB 7.0.43, Redis 7.2.16 |
| Subjects | driver 640 and driver 663, tenant `tenant-demo` |

Two runs, because one subject could not exercise every store. Driver **640** is the
natural case: nothing of that subject was cached anywhere, which is the normal state of
this schema. Driver **663** was given a cached Redis entry and a telemetry document
carrying `driver_id` first, so that the MongoDB and Redis branches are shown working
rather than only shown returning zero.

## Run 1 — driver 640, the natural case

```
erasure record     : 1
drivers anonymised : 1
assignments closed : 1
audit rows blanked : 0
mongo documents    : 0
redis keys removed : 0

retained by design (the fleet's operational record, link severed):
  trips            : 100
  idle events      : 13

verified erased    : True
```

### Read back independently

Read back through `fleet_batch` (a different role from the one that wrote), so this is
what a reader sees rather than what the eraser believed it did:

| Query | Result |
|---|---|
| `SELECT full_name, licence_ref, erased_at IS NOT NULL FROM drivers WHERE driver_id=640` | `[ERASED]`, `ERASED-640`, `t` |
| trips reachable via that driver's assignments | **100** — retained |
| idle events reachable via that driver's assignments | **13** — retained |
| `vehicle_assignments` still open for driver 640 | **0** |

The split is the point. The identity is gone; the fleet's own record of its fuel cost is
not. Deleting those 100 trips would have silently changed historical fuel totals and made
last month's finance report irreproducible — and an anonymised record is no longer
personal data, which is what Article 17 actually asks for.

### The erasure is itself audited

`erasure_log` row 1 (driver 640), read back:

| Column | Value |
|---|---|
| `subject_type` / `subject_ref` | `driver` / `640` |
| `requested_by` | `dpo@example.test` |
| `completed_at` | set |
| `stores_affected` | `{"errors": [], "trips_retained": 100, "drivers_anonymised": 1, "redis_keys_removed": 0, "assignments_severed": 1, "idle_events_retained": 13, "audit_rows_anonymised": 0, "mongo_documents_updated": 0}` |

The record is opened **before** anything is erased, so a half-completed erasure still
leaves a trace of why data went missing. It can be inserted and completed by
`fleet_erasure` but never deleted: evidence of compliance that can be deleted is not
evidence.

## Run 2 — driver 663, all three stores

Planted first: `driver:663` and `driver:663:last_trip` in Redis, and `driver_id: 663` on
one `telemetry_raw` document.

```
erasure record     : 2
drivers anonymised : 1
assignments closed : 1
audit rows blanked : 0
mongo documents    : 1
redis keys removed : 2

retained by design (the fleet's operational record, link severed):
  trips            : 100
  idle events      : 12

verified erased    : True
```

### Read back independently


| Check | Result |
|---|---|
| `redis-cli KEYS "driver:663*"` | no keys |
| `db.telemetry_raw.countDocuments({driver_id: 663})` | **0** |
| trips and idle events for that driver's vehicles | 100 and 12 — retained |

Both the exact-key form and the wildcard form are covered: `driver:663` matched the first
pattern and `driver:663:last_trip` the second, which is why the count is 2.

## The zeros that remain are honest zeros, not skipped steps

- **audit_log** — no endpoint in the API reads driver data yet, so no row has
  `resource_type = 'driver'`. The column-level `UPDATE (resource_id)` grant that makes
  this step possible without reopening the audit trail to rewriting is exercised in
  `tests/integration/test_erasure.py`, which inserts such a row first and also asserts
  the boundary: `fleet_erasure` cannot change `actor_id`, `action` or `occurred_at`.
- **MongoDB and Redis in run 1** — raw telemetry is keyed by VIN, not by driver, so in
  this schema there is normally nothing to erase, and no driver state was cached for that
  subject. The steps still run and report their counts: "we checked and found none" is a
  materially different statement from "we did not check", and only the first can be
  evidenced. Run 2 shows the same code paths removing data when there is data to remove.

## Failure behaviour

Each store is attempted independently and a failure is **recorded on the audit record**
rather than raised. Aborting on the first failure would leave the subject erased from some
stores and present in others with no record of which — neither compliant nor recoverable.
When any store fails the CLI prints the errors, says the erasure is INCOMPLETE, and exits
non-zero.

## Tests

`tests/integration/test_erasure.py` (Testcontainers, real PostgreSQL) covers the
anonymisation, the retention of trips and idle events, the audit-column boundary, tenant
isolation of the erasure path, idempotent re-runs, and the incomplete-erasure path.
