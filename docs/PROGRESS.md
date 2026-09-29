# Progress log

Session handoff record (CLAUDE.md S17). Newest first.

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
