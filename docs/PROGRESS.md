# Progress log

Session handoff record (CLAUDE.md S17). Newest first.

---

## 2026-09-30 - Completion pass: the remaining deliverables

Audited the repo against the PDF deliverables checklist and found five empty directories
where deliverables were supposed to be. Filled them.

### Done

| Deliverable | State |
|---|---|
| `reconcile-late` CLI | Wired: drain -> group -> re-segment -> replace vehicle-day -> recompute rollups -> audit row |
| Architecture diagram | `docs/architecture/architecture.md` - C4 L1/L2, three sequence diagrams, layering, scaling, cloud boundary |
| ER diagram | `docs/architecture/er-diagram.md` - 3NF, every rollup labelled with why it is denormalised |
| Helm charts | `infra/helm/fleet-platform/` - shared `_helpers.tpl`, Deployments, CronJobs, HPA, PDB, non-root, read-only rootfs |
| Terraform (AWS) | `infra/terraform/aws/` - VPC, EKS, MSK, RDS, ElastiCache, S3, Secrets Manager, budget alarm |
| Load scenario | `tests/load/run_scenario.py` - producer throughput with per-second percentiles |
| Chaos scenario | `tests/chaos/run.py` - kills broker/Postgres/Redis, **reconciles counts**, fails on a shortfall |
| Solution Document | `docs/solution-document/SOLUTION.md` - all 17 template sections |
| ADR-0008 | Closes D9, D10 and the rest of D11 |

D1-D3, D5-D7, D9-D11 are now all DECIDED with an ADR. D4 (auth vendor) and D8 (MSK vs
Strimzi) remain open by design - both are deployment choices the code is deliberately
agnostic to.

### Findings worth keeping

- **Five deliverable directories existed but were empty.** `infra/terraform`, `infra/helm`,
  `docs/architecture`, `docs/solution-document` and `docs/evidence/chaos` had been created
  early and never filled. An empty directory in a tree reads as "started" at a glance, which
  is worse than absent - it is why this pass began with a checklist audit rather than with
  code.
- **Three Makefile targets pointed at modules that did not exist**: `tests.load.run_scenario`,
  `tests.chaos.run` and `tests.tools.pin_digests`. The first two now exist. `pin-digests` is
  still a target with no module behind it.
- **The chaos test asserts accounting, not recovery.** "The container came back" is trivial to
  observe and proves nothing about the data. Each scenario now takes a baseline count,
  produces a known number of events, SIGKILLs a service, waits for health, and reconciles.
  A shortfall fails the command. At-least-once delivery means a *surplus* is correct, so the
  assertion is `>=`, not `==`.
- **The load script cannot prove the 100K events/s NFR and says so in the artefact it
  writes.** The caveat is embedded in the JSON, not just the README, so the number cannot be
  quoted without it.
- **Terraform `validate` needs no credentials.** Worth stating plainly because "no AWS
  account" sounds like it blocks the IaC deliverable and does not: `fmt`, `init -backend=false`
  and `validate` are all credential-free. Only `plan` and `apply` need an account, and those
  were never in scope.

### Blocked on the environment, not on design

Docker Desktop is not running (the machine restarted - Windows build 26200 -> 26300), so
these are written but **not yet executed**:

- `make tf-validate` - Terraform fmt/init/validate + config scan
- `make helm-validate` - Helm lint + template for both value sets
- `make load` / `make chaos` - both need the stack
- The live `reconcile-late` run for compliance evidence
- Integration suite (Testcontainers)

Everything that does not need Docker is green: ruff, ruff format, mypy --strict across 56
modules, the unit suite and the 19 BDD scenarios.

### Next

1. Start Docker Desktop, then run the five items above and commit their evidence.
2. Tag `v1.0-submission` at code freeze.
3. Demo video - not produced, and SOLUTION.md S13 says so rather than listing it as done.

---

## 2026-09-29 - Day 3 start: query plans, STRIDE, and a measured NFR defect

### Done

| Area | State | Evidence |
|---|---|---|
| SQL before/after query plans | `tests/tools/sql_plans.py` builds a throwaway database, loads it, measures with `EXPLAIN (ANALYZE, BUFFERS)`, drops it | `docs/evidence/sql/query-plans.md` |
| STRIDE threat model | 6 trust boundaries, every category, 5 open items ranked by what to fix first | `docs/STRIDE.md` |
| Leaderboard reads the rollup | was aggregating raw `idle_events` per request | measured below |

### NOT YET VERIFIED at the time of writing

The shell tooling became unavailable mid-change. The following are written but **not run**:
`uv run ruff check`, `mypy`, `pytest services/api` (two new leaderboard freshness tests),
`npm run typecheck`/`build` for the web changes, and a re-run of `sql_plans.py` with the
rollup comparison added. Do not treat the leaderboard change as done until they pass.

### Findings worth keeping

- **No index rescues the leaderboard.** It aggregated raw `idle_events` over a 30-day
  window on every request, and the `(tenant_id, started_at DESC)` index bought almost
  nothing (1.07x at 200K rows, 1.29x at 2M) because a month of one tenant's events is a
  large fraction of the table, so PostgreSQL correctly chose a sequential scan. The
  docstring claimed the query ran "over rows already narrowed by the index", which
  measurement showed was not what happened.
- **Reading the daily rollup instead is a clear win: ~3x, and under the 200 ms target.**
  Comparing both variants as they ship, rollup 91 ms (89-117) against raw 282 ms (275-313),
  ranges non-overlapping. `vehicle_daily_leaderboard_idx` earns its keep too, a genuine 2x
  on the rollup query.
- **Getting to that number took four passes and I was wrong twice on the way.** With a
  5-sample median the ordering of the two variants reversed between runs: pass A said rollup
  faster, pass B said raw faster and even showed the rollup's index making it *slower*
  (0.45x, planner trading a parallel seq scan for a bitmap heap scan). I had already written
  "1.8x faster" into the docstring off pass A, then over-corrected to "not a measured win"
  off pass B. Both were artefacts of five samples on a machine contended by my own
  concurrent benchmark runs.
- **What fixed it:** nine samples instead of five, the full min-max range printed beside
  every median, and a refusal to print a ratio when the ranges overlap. The `no measurable
  difference` verdict now shown for the raw leaderboard is that rule firing correctly. The
  conclusion was only accepted once two independent nine-sample runs agreed with
  non-overlapping ranges. **The lesson is the deliverable here, not the ratio:** a
  single-number benchmark on a contended laptop produced three different publishable
  answers.
- **The scaling argument still carries more weight than the 3x.** The rollup path scales
  with vehicle-days, bounded by fleet size times window length; raw aggregation scales with
  events, which is unbounded as telemetry frequency rises. Meeting 200 ms at 2M events on
  one laptop is not the same as meeting it for 100K vehicles - a window-level pre-aggregate
  is recorded as the next step, not built.
- **Correctness was verified independently of performance.** The rollup ranking and the raw
  ranking agree exactly on the demo data: 92 vehicles, 38,085.70 total either way, no
  vehicle differing.
- **The evidence file reported "Table size: 0 bytes".** `pg_total_relation_size` on a
  partitioned parent returns 0 because the parent stores no rows, which reads as "the table
  is empty" rather than "wrong function". Now summed over the partitions.
- **Three queries did benefit enormously**: keyset pagination 37x, the partial excessive
  index 15x, one-vehicle history 16x. So the index set is right; one query was reading the
  wrong table.
- **The demo database cannot produce useful query plans.** At ~2,600 idle events
  PostgreSQL sequential-scans everything, so index choice makes no measurable difference.
  Hence a throwaway database with generated rows, and the evidence file says plainly that
  the rows are generated and that no platform throughput claim is derived from them.
- **A genuine "before" needs the index absent, not disabled.** `SET enable_indexscan = off`
  makes the planner fall back to a bitmap scan, so the comparison stops meaning what it
  appears to mean.
- **Three Makefile targets point at modules that do not exist**: `tests.load.run_scenario`,
  `tests.chaos.run` and `tests.tools.pin_digests`. `tests/tools/` did not exist until this
  change. Day 3 work will create the first two; the digest pinner needs writing or the
  target needs removing.

### Next

1. Late-event reconciliation from `telemetry.late` - **needs a D6 decision first**, see
   OPEN_DECISIONS. Nothing consumes that topic today, so "never silently drop" is not yet
   true in practice.
2. Security scans: Semgrep, Trivy, ZAP into `docs/evidence/security/`.
3. Load and soak scenario, chaos tests.
4. Terraform for AWS (written and validated, not applied) and Helm on local k3d.
5. ADRs for D6, D9, D10, D11.

---

## 2026-09-29 - Day 2: acceptance scenarios, and a field that was lying

Two things: the BDD suite PDF S12 requires, and a coverage figure that was reported as
zero on every response.

### Done and verified

| Area | State | Evidence |
|---|---|---|
| BDD acceptance | 4 features, 19 scenarios, 105 steps, one feature per product question | `docs/evidence/acceptance/` |
| behave wired in | `behave.ini`, `make test-acceptance`, `./make.ps1 test-acceptance`, CI job, JUnit artefact | runs in 0.19 s, no infrastructure |
| Utilisation coverage | `utilisation_window` table written by the batch job, read by the API, rendered on the page | migration 008, 7 unit + 7 integration cases |
| Utilisation and alerts endpoints | Had **no HTTP-level tests at all**; now 19 | `services/api/tests/test_utilisation_and_alerts.py` |
| Dead code removed | 5 duplicate helpers in `api/testing.py` | `ruff`, 60 API tests still green |

### Findings worth keeping

- **`unscored_no_telemetry` was declared and never populated.** It sat in the API schema
  and the TypeScript types, defaulting to 0, so every response said no vehicles were
  unscored while the batch job had just failed to score 1,808. A field that states a
  reassuring number as a fact is worse than no field, because a reader has no reason to
  doubt it.
- **The API could not have derived the number honestly.** "Not scored" has two causes -
  no telemetry reached us, and the vehicle's class had too few peers - and they lead to
  opposite actions. Counting "active vehicles without a score row" would have collapsed
  them and answered neither, so the batch job records what it observed and the API reads
  it back.
- **The CHECK constraint is the useful part.** `scored + no_telemetry + class_too_small =
  vehicles_active` means a miscount fails the write instead of showing a plausible wrong
  total. The dataclass raises too, but a future backfill or manual correction would not
  go through the dataclass.
- **An inner join was hiding vehicles.** `load_window_activity` joined from
  `vehicle_daily`, so a vehicle with no rows at all was invisible rather than counted as
  unscored - the fleet total would quietly shrink to whatever reported. Now a LEFT JOIN
  from `vehicles`.
- **behave exec()s `environment.py` and the steps outside any package**, so they cannot
  import from each other; shared vocabulary has to live in the steps module or an
  installed package.
- **Two step patterns were ambiguous** because `{view}` matched a longer phrase. Fixed by
  giving each step a distinct literal prefix rather than by loosening the parser.
- **The suite was mutation-checked.** Forcing the route to return `coverage=None` failed
  exactly one scenario with the right message; reverting restored 19 passed. A green BDD
  suite that cannot fail is decoration.
- **One assertion I wrote was a tautology** (`Decimal(raw) == Decimal(raw)`). Replaced
  with a check that the money string kept its 4-decimal scale, which is the property that
  actually matters on the wire.
- **400 vs 422 is a real split in the API** and is now pinned by tests rather than left to
  surprise a client: the band allow-list is an application check (400), query constraints
  are enforced by FastAPI from the route signature (422).

### Next

1. Vehicle Detail view, the fifth view in CLAUDE.md S14.
2. Late-event reconciliation from `telemetry.late` (D6).
3. ADRs for D6, D9, D10, D11.
4. Day 3: load and soak evidence, security scans, chaos tests, SQL query plans,
   Terraform written and validated but not applied (Option A), Helm on local k3d, STRIDE.

---

## 2026-09-29 - Day 2: right to erasure across every store

PDF S11 requires a right-to-erasure flow and S12 requires it tested. Built it as a batch
subcommand and ran it against the live stack.

### Done and verified against the live stack

| Area | State | Evidence |
|---|---|---|
| Erasure across four stores | `batch erase-driver`, per-store reporting, verified by reading back | `docs/evidence/compliance/erasure-local.md` |
| Identity erased, business record kept | driver anonymised; 100 trips and 13 idle events retained with the link severed | run 1, read back through a different role |
| MongoDB and Redis branches | 1 document unset, 2 cached keys removed | run 2 |
| Erasure is itself audited | `erasure_log` opened before the first write, completed with a per-store summary | row read back |
| Audit trail stays append-only | dedicated `fleet_erasure` role with a **column-level** `UPDATE (resource_id)` grant | `migrations/007`, privilege-boundary tests |
| Tests | 42 integration cases including the privilege boundary and tenant isolation of the erasure path | `tests/integration/test_erasure.py` |

### Findings worth keeping

- **Two requirements pulled against each other and both were real.** Migration 003
  revoked UPDATE on `audit_log` from every application role, because a trail the
  application can rewrite is not evidence. But the trail names the subject, so erasing a
  subject means reaching into it. Granting the batch role UPDATE would have undone the
  first requirement to satisfy the second. Resolved with a NOLOGIN role holding a
  column-level grant on `resource_id` alone: the subject reference is erasable, and
  actor, action, timestamp and correlation id stay immutable.
- **A failure per store is recorded, not raised.** Aborting on the first failure would
  leave a subject erased from some stores and present in others with no record of which -
  neither compliant nor recoverable. The CLI exits non-zero and says INCOMPLETE.
- **`erase_driver` needed an explicit `conn.rollback()`** in the Postgres error path.
  Postgres refuses every later statement on an aborted transaction, so without it the
  audit write that records the failure would itself fail - defeating the point of
  recording rather than raising.
- **The operational record is read-only to the eraser.** `fleet_erasure` has SELECT and
  no DELETE on `trips` and `idle_events`. Deleting them would erase the fleet's own record
  of its fuel cost and make a past finance report irreproducible.
- **Self-inflicted, worth recording:** while planting test data I ran
  `updateMany({}, {$set: {driver_id: 663}}, {limit: 1})`. `limit` is not an `updateMany`
  option, so the empty filter matched everything and stamped 3.6M telemetry documents
  before it was killed. Undone by unsetting the field (3,621,336 modified, 0 remaining).
  Synthetic data only, and nothing derived from it had been computed - but the lesson is
  that an empty filter plus an ignored option is a silent full-collection write.

### Next

1. BDD acceptance scenarios (`tests/acceptance/`, behave) - explicit PDF S12 requirement.
2. Vehicle Detail view, the fifth view in CLAUDE.md S14.
3. Late-event reconciliation from `telemetry.late` (D6).
4. ADRs for D6, D9, D10, D11.

---

## 2026-09-29 - Day 1 continued: API and dashboard

The Solution Document Template was supplied, closing gap G13. Built the API layer and
the first two dashboard views.

### Done and verified against the live stack

| Area | State | Evidence |
|---|---|---|
| Keyset pagination | Opaque cursors, capped page size, no total count | 32 tests, 4 property-based |
| RFC 7807 errors | Typed errors, correlation id, no leaked detail | verified over HTTP |
| OIDC/JWT (D4) | Local stub issues real RS256; API verifies through the production path | token minted and verified |
| Algorithm allow-list | `none` and HMAC refused at settings load *and* at verification | `test_settings.py` |
| API endpoints | summary, trend, idle-events, leaderboard, vehicle detail, healthz, readyz | all exercised with a real token |
| Tenant isolation over HTTP | tenant-second gets **404** for a tenant-demo VIN, not 403 | verified live |
| Location masking | finance role sees 2 dp; operations sees full precision | verified live |
| Audit trail | an entry per read, with a /24-masked client address | `test_api.py` |
| Money exactness | `Decimal` end to end, serialised as strings | `test_money_is_serialised_as_a_string` |
| Web | Fleet Overview + Idle Cost Leaderboard, with drill-down to individual events | typecheck, lint and build clean; **verified in a browser against live data** |

**Gate 1 is closed.** One telemetry event is traceable through to one cost figure in the
browser, with its assumptions beside it.

**Test status:** 478 passed, **85.69% coverage**, gate 80%. `mypy --strict` clean across
47 modules; web typecheck, lint and build clean.

### Pipeline re-run after the fixes below

| Metric | Simulator generated | Processor detected |
|---|---|---|
| Events | 294,316 | 294,316 consumed |
| **Excessive idle (alerts)** | **83** | **83 — exact** |
| Billable idle periods | 272 | 271 |
| Accounting | - | 284,502 + 2,845 + 5,437 + 1,532 = 294,316 |

D2 cross-check across 264 priced segments: **median divergence 2.6%**, 100% within 15%.

### Findings worth keeping

- **A pydantic `computed_field` on the API summary reported the wrong formula version.**
  `max(formula_version)` picked the BEV placeholder `'unpriced'`, which sorts after
  `'idle-fuel-v1'`, so a user-facing figure misreported the very thing that field exists
  to disclose. Now restricted to priced rows.
- **`ON CONFLICT` against a PARTIAL unique index must repeat the index predicate**,
  otherwise Postgres cannot infer the index and the insert fails outright.
- **The simulator's reported fuel rate did not match the configured burn rates.** It
  emitted a flat ~0.9 L/h while a medium truck is configured at 2.4 L/h, so the D2
  cross-check reported roughly 60% divergence on *every* segment - noise rather than a
  signal, and an obvious question for a reviewer. The rate now derives from the same
  burn-rate table, giving a median divergence of **+2.3%** with 100% of segments inside
  ±15%.
- **Docker Desktop stopped mid-session**, and the symptom was `readyz` reporting both
  stores down rather than anything about Docker. Restarted from its per-user install
  path; the volumes kept all data.
- **`readyz` returns *degraded*, not *unready*, without Redis.** Redis backs rate
  limiting and caching, neither of which the API needs to answer correctly, so losing it
  must not pull an instance out of the load balancer.

- **The Dockerfile's workspace list drifted from the uv workspace.** Adding
  `services/oidc_stub` as a member left the image's hand-maintained list behind, and the
  only symptom was an opaque `uv sync --frozen` exit code 2 at image build time -
  discovered while rebuilding for the demo, which is the worst possible moment.
  `tests/test_dockerfile_workspace.py` now asserts the two agree.
- **The simulator's default start date put most events in the future.** It defaulted to
  *today* 05:00Z and generates a working day forward, so a run at 06:59 UTC sent **72%
  of events to the DLQ** as `timestamp_too_far_future`. The guard was right; the default
  was wrong. It now defaults to yesterday and warns when a chosen window reaches into
  the future. Worth noting that the DLQ reason codes made this diagnosable in one query
  rather than a debugging session - which is the argument for having them.
- **CORS was on the API but not the OIDC stub**, so the dashboard's sign-in failed at
  the browser. A real provider would not need it (the authorization-code flow uses
  redirects, which CORS does not apply to), so this is a property of the stub alone.

### Next

1. Remaining three views: Vehicle Detail, Utilisation, Attention/Alerts.
2. Utilisation rollups (D3 wired into `services/batch`), late-event reconciliation (D6).
3. Right-to-erasure flow across all four stores, and BDD acceptance scenarios.

---

## 2026-09-29 - Day 1 continued: end-to-end pipeline working

`simulator -> Kafka -> processor -> PostgreSQL + MongoDB + Redis` runs end to end, and
the detector is checked against the simulator's ground truth rather than trusted.

### Done and verified

| Area | State | Evidence |
|---|---|---|
| Seed job | 100,000 vehicles in 16.6s, COPY per tenant under RLS | 100,000 distinct VINs, 0 missing a burn rate |
| Processor adapters | Postgres/Mongo/Redis sinks, all batched and idempotent | 305,745 events processed |
| Consumer loop | poll -> process -> persist -> flush -> commit | offsets committed last, always |
| Accounting | 295,520 + 2,900 + 5,780 + 1,545 = 305,745 | exact; asserted every run |
| Detection accuracy | **71 of 71 excessive idle periods, exact**; 259 of 260 billable | `docs/evidence/load/pipeline-end-to-end-local.md` |
| BEV exclusion (D9) | 5 of 259 segments unpriced, all 5 BEVs | cost NULL, never zero |
| Cost output | 254 priced rows, each carrying formula/burn rate/threshold/price_id | tenant-demo 3,733.50 INR, tenant-second 3,551.71 INR |

**Test status:** 375 passed, **88.31% coverage**, gate 80%. Lint, format and
`mypy --strict` clean.

### Findings worth keeping

- **`SET` accepts no bind parameters in PostgreSQL; `set_config()` does.** Hit this
  twice - once in the isolation tests, once in the Postgres sink. There is now a
  module-level note in `sinks.py` so it does not happen a third time.
- **`ON CONFLICT` against a PARTIAL unique index must repeat the index predicate**, or
  Postgres cannot infer the index and the insert fails outright.
- **71 excessive segments produced 69 alerts, and that is correct.** Both un-alerted
  segments were exactly 600 s while every alerted one was 660 s or more: they crossed
  the threshold on the sample that *closed* them. `EXCESSIVE_IDLE_IN_PROGRESS` asserts
  a vehicle is idling right now, so alerting on an ended period would send an operator
  after a driver who is already moving. Documented as an invariant and tested.
- **Duplicate and malformed counts reconcile exactly once you notice validation runs
  before dedup.** A duplicated malformed event is rejected twice and never reaches the
  dedup stage: 1,535 injected malformed + 10 = 1,545 rejected, and 2,910 injected
  duplicates - 10 = 2,900 deduplicated.
- **The alert suppression key was the triggering event's timestamp, not the segment's
  start.** The identity being suppressed is the idle period, so the key now comes from
  the segment.

### Throughput, scoped

| Stage | Rate | Scope |
|---|---|---|
| Generate only | 44,929 events/s | no broker |
| Generate + produce | 23,697 events/s | 1 producer process |
| **Full consumer pipeline** | **4,404 events/s** | **1 consumer process** |

All LOCAL on a laptop whose Docker VM has ~3.9 GB for four datastores.

### Open decision this raised

At 4,404 events/s per consumer, 100,000 events/s needs roughly **23 consumer
processes** against the 24 partitions ADR-0004 provisions. That is near the partition
ceiling with no room to lose a consumer. Raising the partition count to 48 or 96 is the
straightforward fix - cheap, and it preserves the design. Batch envelopes were the other
candidate but attack per-message overhead, whereas the consumer's cost is dominated by
per-event work. **To settle before the cloud benchmark; ADR-0005 remains provisional.**

### Next

1. FastAPI: `/api/v1/vehicles`, `/idle-events`, `/fleet/summary` with keyset pagination
   and the local OIDC stub.
2. React: Fleet Overview and Idle Cost Leaderboard.
3. Gate 1 closes when one event is visibly traceable to one idle event and one cost
   figure in the UI, with its assumptions shown.

---

## 2026-09-29 - Day 1 continued: live stack, schema and simulator

Docker was installed by the developer, unblocking B1. Everything below runs against
real infrastructure.

### Done and verified

| Area | State | Evidence |
|---|---|---|
| Local stack | All four stores healthy on first run | Kafka 3.9.0, PostgreSQL 16.15, MongoDB 7.0.43, Redis 7.2.16 |
| Topics | 5 topics, partition counts per ADR-0004 | `telemetry.raw` 24, `idle.events` 12, side topics 6 |
| Schema | 37 tables, 4 migrations applied | 3 partitioned parents, 5 partitions each |
| Tenant isolation | RLS on 17 tables, ENABLE + FORCE | 18 integration tests incl. leakage attempts |
| Simulator roster | 100K vehicles in 0.89s, unique valid VINs | benchmarked locally |
| Simulator journeys | Trips, idle, and all five injected fault types | ground-truth `DaySummary` |
| Container image | Multi-stage, non-root uid 10001, schema shipped | `fleet/simulator:dev` builds and runs |
| Produce to Kafka | **305,745 enqueued, 305,745 acknowledged, 0 failed** | `docs/evidence/load/simulator-produce-local.md` |
| Event size | **479 B/event** measured over 275,202 events | `docs/evidence/load/event-size.md` |
| Partition balance | Hottest partition 1.042x mean at 100K vehicles | `docs/evidence/load/partition-balance.md` |

**Test status:** 311 passed (293 unit, 18 integration), **96.23% coverage**, gate 80%.
Lint, format and `mypy --strict` clean. Run: `./make.ps1 verify`.

### Findings worth keeping

- **`docker compose up` succeeded while Postgres connections silently failed.** A
  native PostgreSQL 17 service on this machine owns port 5432; on Windows the
  published port still binds, but host connections reach the native server. It
  presents as a wrong password. The container now publishes 55432, and the native
  service was left alone.
- **Superusers bypass RLS unconditionally.** `FORCE ROW LEVEL SECURITY` closes the
  table-owner gap, not the superuser one, so the policies only bite if the app does
  `SET ROLE fleet_app`. Found by a `DELETE` that reached both tenants in a test.
  Recorded in ADR-0006, with two tests asserting the bypass so it cannot surprise us.
- **A pydantic `computed_field` leaked the database password.** `PostgresSettings.dsn`
  was a computed field, which pydantic includes in `repr()` and `model_dump()`, so the
  password reached any log line touching the settings object. Now a plain property.
- **A UNIQUE index on a partitioned table must include the partition key.** That
  would have defeated the alert-suppression constraint, so `alerts` is deliberately
  not partitioned - alerts are low volume and a database-enforced guarantee against
  alert storms is worth more than partition pruning.
- **ADR-0004's event-size estimate was ~60% too low.** Estimated 250-300 B/event;
  measured 479 B. Corrected in the ADR from the measurement.
- **Partition imbalance at small N is expected, not a defect.** At 200 vehicles the
  hottest partition held 5x the keys of the quietest. Modelling librdkafka's
  `crc32 % partitions` reproduced the broker's assignment exactly, and the validated
  model projects 1.042x the mean at 100K vehicles. Relative imbalance decays as
  1/sqrt(N).
- **A "too slow" reading was a measurement artefact.** Fleet generation looked like
  1 ms/vehicle until profiling showed 0.023 s of the timed region was numpy's
  one-time lazy import of `numpy.random`. Real cost is ~9 us/vehicle.

### Blockers

| # | Blocker | State |
|---|---|---|
| B1 | Docker Desktop / WSL2 not installed | **RESOLVED** - Docker 29.8.0 installed by the developer |
| B2 | `confluent-kafka` native library blocked by Windows Application Control | **RESOLVED by design** - services run in Linux containers, where librdkafka 2.15.1 loads normally. The Kafka adapter is imported lazily so unit tests and `--dry-run` still work natively |
| B3 | AWS free learning account cannot host a 100K events/s benchmark | Open. Agreed approach: measure locally with provenance, prove horizontal scaling with a replica ladder, publish a clearly labelled projection, deploy small to AWS only to prove the deployment path |

### Throughput so far, scoped honestly

| Measurement | Rate | Scope |
|---|---|---|
| Generation only, 1 process | 44,929 events/s | No broker |
| Generate + produce, 1 container | 23,697 events/s | Single broker, RF=1 |

Producer-side only. The consumer side - validation, dedup, idle state machine and
batched writes to three stores - is the harder half and is **not yet measured**.
ADR-0005 stays provisional until it is.

### Next

1. Processor: consume, validate to DLQ, per-vehicle high-water-mark dedup, idle state
   machine, batched writes to Postgres/Mongo/Redis, alerts.
2. Seed job: write the roster to Postgres so events have vehicles to join to.
3. FastAPI: three endpoints, keyset pagination, OIDC stub.
4. React: Fleet Overview and Idle Cost Leaderboard.
5. Gate 1: one event visibly becomes one idle event and one cost figure in the UI.

---

## 2026-09-29 - Day 1: foundation and domain core

**Plan approved:** four-day schedule, walking skeleton first. See the approved
plan in the session transcript and `docs/OPEN_DECISIONS.md` for decision status.

### Done and verified

| Area | State | Evidence |
|---|---|---|
| Repo scaffold | Monorepo layout per CLAUDE.md S6, git initialised | `git ls-files` |
| Python workspace | uv workspace, 5 packages, one pinned `uv.lock`, Python 3.12.13 | `uv lock --check` |
| Tooling | ruff (lint+format), mypy `--strict`, pytest, coverage gate at 80% | `./make.ps1 lint` |
| Event contract | `schemas/telemetry.raw.v1.json` + Pydantic model, agreement enforced by tests | 32 contract tests |
| Idle segmentation | Streaming state machine with hysteresis and gap tolerance; batch helper folds over it so the two cannot diverge | 32 tests, 5 property-based |
| Fuel and cost | Config burn rates, `fuel_rate_lph` cross-check, `Decimal` money, BEV excluded loudly | 22 tests |
| Utilisation | Composite score, median/MAD with a MAD floor | 26 tests, 4 property-based |
| VIN validation | ISO 3779 check digit, verified against 3 external oracles including the PDF's own VIN | 19 tests, 4 property-based |
| DTC parsing | SAE J2012 system/subsystem decode, idle-related family flagged | 32 tests, 3 property-based |
| Compose stack | Kafka KRaft, Postgres 16, Mongo 7, Redis 7.2 with healthchecks | YAML validated; **not yet run** |
| Makefile + shim | `Makefile` canonical; `make.ps1` for Windows where `make` is absent | `./make.ps1 verify` green |
| CI | GitHub Actions: quality, contract, security, integration (gated off) | YAML validated |
| ADRs | 0001-0005 written | `docs/adr/` |

**Test status:** 164 passed, **95.63% coverage**, gate 80%. Lint, format and
`mypy --strict` all clean. Run: `./make.ps1 verify`.

### Findings worth keeping

- **The MAD floor in ADR-0003 was found by a failing test, not by design.** On a
  tightly clustered vehicle class the raw MAD is tiny, so an unremarkable vehicle
  computed to -3.6 robust sigma and the `abnormal` band filled with false
  positives. Fixed in the domain, not the test.
- **The VIN check digit validates against the PDF's own example VIN**
  (`1HGCM82633A004352`), which is a strong signal the transliteration and weights
  are right. European VINs legitimately fail this check - documented as a known
  limitation in `vin.py`, not a bug.

### Blockers

| # | Blocker | Impact | Fix |
|---|---|---|---|
| **B1** | **Docker Desktop not installed; WSL registration corrupt** (`REGDB_E_CLASS_NOT_REG`). Windows 10 Home, so Hyper-V is unavailable and Docker requires the WSL2 backend. Virtualization *is* enabled in firmware. | Blocks the compose stack, Testcontainers integration tests, and the end-to-end walking skeleton. Pure domain work is unaffected by design (hexagonal layering). | Elevated PowerShell: `wsl --install`, reboot, install Docker Desktop, `wsl --set-default-version 2`. Needs admin and a reboot. |
| **B2** | `confluent-kafka` native import blocked by Windows Application Control (`An Application Control policy has blocked this file`) | Cannot run the Kafka adapter natively on Windows. Does not affect unit tests or the containerised runtime. | Run the processor in its Linux container. Keep the Kafka adapter import lazy so unit tests never load the DLL. |
| **B3** | AWS account is a 6-month free learning account | **100K events/s cannot be demonstrated on it.** EKS control plane is not free-tier eligible and the rate needs ~100 MB/s sustained. | Agreed approach: measure locally with full provenance, prove horizontal scaling with a replica ladder, publish a clearly labelled projection, and deploy small to AWS only to prove the deployment path. |

### Next: finish Day 1

1. Simulator: seeded, vectorised event generation (pure core, Kafka producer as an adapter).
2. Processor: validate to DLQ, per-vehicle high-water-mark dedup, idle machine, batched writes.
3. Postgres schema and migrations; Redis latest-state; Mongo raw with TTL.
4. FastAPI: three endpoints, keyset pagination, OIDC stub.
5. React: Fleet Overview and Idle Cost Leaderboard.
6. Gate 1: one event visibly becomes one idle event and one cost figure in the UI.

Items 2-5 need B1 fixed to be demonstrated end to end. Their pure logic and unit
tests do not.
