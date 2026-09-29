# End-to-end pipeline run — LOCAL

First complete run of `simulator → Kafka → processor → PostgreSQL + MongoDB + Redis`,
checked against the simulator's own ground truth.

**LOCAL measurement on a developer laptop.** Not the 100,000 events/s NFR.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-28 |
| Commit | recorded by the commit that adds this file |
| Host | Windows 10 Home, Docker Desktop, Linux containers, ~3.9 GB VM |
| Producer | 1 container, 200 vehicles, one simulated day, seed 20260929 |
| Consumer | **1** processor container, batch size 5,000 |
| Broker | Kafka 3.9.0 KRaft, single broker, RF 1, 24 partitions |
| Stores | PostgreSQL 16.15, MongoDB 7.0.43, Redis 7.2.16 |
| Seeded fleet | 2,000 vehicles across 2 tenants |

## Accounting: nothing was lost

| Disposition | Count |
|---|---|
| Consumed | **305,745** |
| Accepted | 295,520 |
| Duplicates dropped | 2,900 |
| Late → `telemetry.late` | 5,780 |
| Rejected → DLQ | 1,545 |
| **Sum** | **305,745** |

`295,520 + 2,900 + 5,780 + 1,545 = 305,745`, exactly the number produced. The
processor asserts this identity at the end of every run and logs an error if it ever
fails, so silent event loss cannot go unnoticed.

Independently: MongoDB holds **295,520** raw documents, equal to the accepted count.

## Detection accuracy against ground truth

The simulator reports what it generated, so the detector can be checked rather than
trusted.

| Metric | Simulator generated | Processor detected | Delta |
|---|---|---|---|
| Events | 305,745 | 305,745 consumed | **exact** |
| **Excessive idle periods (≥600 s)** | **71** | **71** | **exact** |
| Billable idle periods (≥180 s) | 260 | 259 | −1 (0.4%) |
| Duplicates injected | 2,910 | 2,900 dropped | −10 |
| Malformed injected | 1,535 | 1,545 rejected | +10 |

The duplicate and malformed deltas reconcile exactly, and the reason is informative:
validation runs *before* dedup, so a duplicated malformed event is rejected twice and
never reaches the dedup stage. Ten of the injected duplicates were duplicates of
malformed events — hence 1,535 + 10 rejected and 2,910 − 10 deduplicated.

The one missing billable segment is a boundary case: a segment still open when the
stream ended, closed by the shutdown flush.

## Alerts: an invariant, not a discrepancy

71 excessive segments produced **69** alerts. Investigated rather than assumed: both
un-alerted segments were **exactly 600 s**, while every alerted segment was **≥660 s**.

They crossed the threshold on the sample that *closed* them. `EXCESSIVE_IDLE_IN_PROGRESS`
asserts a vehicle is idling **right now**, so raising it for a period that has already
ended would send an operator after a driver who is already moving. The invariant is
therefore `alerts ≤ excessive segments`, with the difference being exactly these
boundary cases. Now covered by
`test_segment_crossing_the_threshold_on_its_closing_sample_raises_no_alert`.

## Cost output

| Tenant | Idle events | Litres | Estimated cost |
|---|---|---|---|
| tenant-demo | 124 | 40.1 | 3,733.50 INR |
| tenant-second | 130 | 37.8 | 3,551.71 INR |

Every row carries the assumptions that produced it, e.g.
`formula=idle-fuel-v1 burn_rate=1.2 L/h threshold=180s price_id=tenant-demo-diesel-current`,
so a finance user can reproduce any figure by hand (CLAUDE.md S14).

**BEV handling (D9) confirmed:** 5 of 259 segments have `cost_est IS NULL` — all 5 are
BEVs (2 light vans, 3 medium trucks). The cost is absent, not zero, so it cannot flow
into a fleet total as though it had been measured.

Redis holds 378 keys for 189 tracked vehicles: latest state plus a dedup watermark each.

## Throughput — the consumer-side figure ADR-0005 was missing

| Stage | Rate | Scope |
|---|---|---|
| Generate only | 44,929 events/s | no broker |
| Generate + produce | 23,697 events/s | 1 producer process |
| **Full consumer pipeline** | **4,404 events/s** | **1 consumer process** |

The consumer does far more work than the producer: decode, schema and VIN validation,
dedup against a sliding window, the per-vehicle idle state machine, cost estimation,
then batched writes to three stores plus side topics.

**What this implies for ADR-0005.** Reaching 100,000 events/s on the consumer side
would need roughly **23 consumer processes**, against the 24 partitions ADR-0004
provisions. That is uncomfortably tight — near the partition ceiling, with no room to
lose a consumer — and it is a measured basis for a decision rather than a guess. Two
options follow, to be settled before the cloud benchmark:

1. **Raise the partition count** (48 or 96). Cheap, and it preserves the current design
   exactly; the cost is more open file handles and slightly more broker metadata.
2. **Batch envelopes** — pack N events per Kafka message, as real telematics gateways
   do. This attacks per-message overhead rather than per-event work, so it helps the
   producer more than the consumer, and the consumer's cost here is dominated by
   per-event work.

Option 1 is the straightforward answer on this evidence. ADR-0005 stays **provisional**
until the decision is recorded.

Caveats: a single broker at RF 1 sharing ~3.9 GB with four datastores, on a laptop.
Replication factor 3 with `min.insync.replicas=2` will cost latency, and therefore
throughput.

## Reproduce

```bash
docker compose --profile core up -d --wait
```

```bash
uv run python -m batch.migrate up && uv run python -m batch.seed --vehicles 2000 --truncate
```

```bash
docker build -f infra/docker/python-service.Dockerfile --build-arg SERVICE=simulator -t fleet/simulator:dev . && docker build -f infra/docker/python-service.Dockerfile --build-arg SERVICE=processor -t fleet/processor:dev .
```

```bash
docker run --rm --network fleet-intel_default -e KAFKA_BOOTSTRAP_SERVERS=kafka:19092 fleet/simulator:dev --vehicles 200
```

```bash
docker run --rm --network fleet-intel_default -e KAFKA_BOOTSTRAP_SERVERS=kafka:19092 -e POSTGRES_HOST=postgres -e POSTGRES_PORT=5432 -e POSTGRES_PASSWORD=$POSTGRES_PASSWORD -e MONGO_URI=mongodb://mongo:27017 -e REDIS_URL=redis://redis:6379/0 fleet/processor:dev --idle-exit-seconds 20
```
