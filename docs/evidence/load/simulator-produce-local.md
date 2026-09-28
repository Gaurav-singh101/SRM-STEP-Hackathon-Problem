# Simulator produce run — LOCAL

**Measured on a developer laptop.** This is *not* the 100,000 events/s NFR and must
never be cited as it. Recorded because it is the first end-to-end datapoint for the
throughput question ADR-0005 leaves open.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-28 |
| Commit | recorded by the commit that adds this file |
| Host | Windows 10 Home, Docker Desktop, Linux containers |
| Docker VM memory | ~3.9 GB total, shared by four datastores |
| Producer | 1 container, 1 process (`fleet/simulator:dev`) |
| Broker | Kafka 3.9.0, KRaft, single broker, replication factor 1 |
| Topic | `telemetry.raw`, 24 partitions, key `vin` |
| Producer config | idempotent, `acks=all`, lz4, `linger.ms=20` |
| Fleet | 200 vehicles, seed 20260929, one simulated day |

## Result

| Metric | Value |
|---|---|
| Events enqueued | 305,745 |
| Events **acknowledged by broker** | **305,745** |
| Events failed | **0** |
| Queue-full back-pressure waits | 0 |
| Wall time | 12.90 s |
| Rate (generate **+** produce) | **23,697 events/s** |
| Rate (generate only, no broker) | 44,929 events/s |

### Independent confirmation of zero loss

The producer's own count is not sufficient evidence — it could be wrong in the same
direction as the bug it would hide. The topic was therefore queried directly:

```
kafka-get-offsets.sh --topic telemetry.raw
  partitions: 24
  total messages: 305745
```

Broker-side total equals producer-side acknowledgements exactly. Note also that
delivery is counted from broker acknowledgement callbacks, not from `produce()`
calls, so this figure means the broker durably accepted every message.

## Data quality injected (PDF S7)

Faults are features here: each one exercises a specific processor path.

| Injected | Count | Exercises |
|---|---|---|
| Duplicates | 2,910 | Dedup high-water mark and the unique sink index |
| Out-of-order | 6,073 | Late-event handling and `telemetry.late` |
| Malformed | 1,535 | DLQ with a reason code, without crashing the consumer |
| Dropped samples | 4,596 | Gap tolerance and segment splitting (ADR-0001) |

Ground truth for correctness checking: 1,201 trips, 475 idle periods, of which 260
billable (≥180 s) and 71 excessive (≥600 s).

## What this tells us about ADR-0005

ADR-0005 chose a Python consumer group and recorded the throughput risk as
unmeasured, with batch envelopes as the mitigation if a single process could not
reach roughly 25K msg/s.

One producer process reaches **23,697 events/s** including generation. Generation
alone is 44,929 events/s, so on this hardware the broker round trip roughly halves
it. Reaching 100K events/s would therefore need about 4–5 producer processes, which
is ordinary horizontal scaling and needs no code change.

This is a **producer-side** figure. The consumer side — validation, dedup, the idle
state machine and batched writes to three stores — is the harder half and has not
been measured yet. ADR-0005 stays provisional until it is.

## Caveats

- Single broker, replication factor 1. Replication factor 3 with
  `min.insync.replicas=2` will cost latency and therefore throughput.
- The whole stack, broker and four datastores included, shares ~3.9 GB on one
  laptop. Not a clean measurement environment, and not intended as one.
- A transient `Coordinator load in progress` warning appeared while the broker
  acquired an idempotence PID, and the producer retried successfully. Worth noting
  as evidence that the idempotent-producer configuration recovers on its own rather
  than losing messages.

## Reproduce

```bash
docker compose --profile core up -d --wait
```

```bash
docker build -f infra/docker/python-service.Dockerfile --build-arg SERVICE=simulator -t fleet/simulator:dev .
```

```bash
docker run --rm --network fleet-intel_default -e KAFKA_BOOTSTRAP_SERVERS=kafka:19092 fleet/simulator:dev --vehicles 200
```
