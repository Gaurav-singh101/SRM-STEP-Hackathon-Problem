# ADR-0005: Stream-processing technology

- **Status:** Accepted (amended 2026-09-29 with measured throughput)
- **Date:** 2026-09-29
- **Resolves:** D5
- **Classification:** Approved project decision, now supported by measured throughput.

## Context

The NFR is 100,000 events/s sustained with a 3x burst for five minutes and no
loss. The processor must validate, deduplicate, run the per-vehicle idle state
machine, aggregate, and write to three stores.

CLAUDE.md D5 explicitly forbids choosing this silently.

## Options considered

1. **Python consumer group** with `confluent-kafka` (librdkafka), micro-batched,
   scaled by processes. One language and one dependency set across simulator,
   processor and API; shares `libs/fleet_common` and the Pydantic contract.
   Scales by partitions and replicas, which is already the mandated strategy.
   *Risk: per-event Python overhead is the ceiling, and the GIL forces processes
   rather than threads.*
2. **Apache Flink** (PyFlink or Java). Event-time windows, watermarks, RocksDB
   keyed state and checkpointed exactly-once come as framework features, which
   would solve out-of-order handling and idle state for us. *Cost: a
   JobManager/TaskManager cluster to operate locally, a second runtime, hard
   debugging, and PyFlink serialization overhead that claws back much of the win.*
3. **Kafka Streams** (Java). Excellent tool, but it puts a JVM service in an
   otherwise all-Python repository and breaks the shared-contract reuse model
   that CLAUDE.md S7 is built on.

## Decision

**Option 1.**

The deciding argument is reuse, not raw speed: the single-event-contract model
only holds if the processor imports the same Pydantic models as the simulator and
the API. Event-time and lateness handling is a modest amount of explicit code -
per-vehicle state in Redis, allowed lateness from config, overflow to
`telemetry.late` - rather than a framework dependency.

A four-day schedule also removes Flink as a realistic option for a single
developer, and that constraint is recorded here honestly rather than dressed up
as a purely technical preference.

## Risk and mitigation

The throughput risk is real and **unmeasured as of this ADR**. Mitigation:
benchmark a single consumer process as a Phase 1 exit gate, before any scaling
decision hardens. If it cannot sustain roughly 25K msg/s:

1. **Batch envelopes** - the simulator packs N events per Kafka message, which is
   what real telematics gateways do anyway, collapsing per-message overhead.
2. Move the decode and aggregate hot loop to a vectorised path.

This ADR will be updated with the measured numbers. Until then, no throughput
claim may appear anywhere in the repository or the solution document.

## Consequences

- Scaling is operational (partitions and replicas), never a code change.
- Back-pressure must be explicit: bounded queues, pause/resume on partitions.
- Offsets commit only after a batch persists, giving at-least-once delivery with
  idempotent writes.
- **Windows note (resolved):** `confluent-kafka` imports a native library that
  Windows Application Control blocks on the development machine. Confirmed working
  inside the Linux service image (librdkafka 2.15.1, running as uid 10001), and the
  Kafka adapter is imported lazily so unit tests and `--dry-run` still work
  natively. Blocker B2 in `docs/PROGRESS.md`.
- **First measurement (producer side).** One producer process sustains 23,697
  events/s including generation, 44,929 events/s generating alone, with 305,745 of
  305,745 messages acknowledged and zero loss. Reaching 100K events/s on the
  producer side therefore needs about 4-5 processes, which is ordinary horizontal
  scaling. Evidence: `docs/evidence/load/simulator-produce-local.md`. The consumer
  side remains unmeasured, so this ADR stays **provisional**.


---

## Amendment, 2026-09-29: throughput measured, partition count raised to 96

The risk this ADR recorded as unmeasured has now been measured end to end.

| Stage | Rate | Scope |
|---|---|---|
| Generate only | 44,929 events/s | no broker |
| Generate + produce | 23,697 events/s | 1 producer process |
| **Full consumer pipeline** | **4,404 events/s** | **1 consumer process** |

The consumer figure is the one that matters, and it is far below the producer's because
it does far more: decode, schema and VIN validation, dedup against a sliding replay
window, the per-vehicle idle state machine, cost estimation, then batched writes to
three stores plus side topics. Evidence:
`docs/evidence/load/pipeline-end-to-end-local.md` (LOCAL, not the NFR).

**Consequence: 24 partitions was not enough.** A Kafka partition is read by exactly one
consumer in a group, so the partition count is a hard ceiling on parallelism - a 25th
consumer against 24 partitions simply idles. At 4,404 events/s the 100K events/s NFR
needs about **23 consumers**, which against 24 partitions left effectively no headroom:
one failed consumer, or a rolling deploy, and throughput drops below target. Cloud will
make this worse rather than better, because replication factor 3 with
`min.insync.replicas=2` costs latency on every write.

**Decision: raise `telemetry.raw` to 96 partitions.**

| Partitions | Max consumers | Headroom over the ~23 needed | Vehicles/partition | Hottest/mean |
|---|---|---|---|---|
| 24 | 24 | 1.0x | 4,167 | 1.042x |
| 48 | 48 | 2.1x | 2,083 | 1.058x |
| **96** | **96** | **4.2x** | **1,042** | **1.074x** |
| 192 | 192 | 8.3x | 521 | 1.144x |

(Computed over 100,000 VINs with librdkafka's `crc32 % partitions` partitioner, using
the model validated against a real broker in `docs/evidence/load/partition-balance.md`.)

More partitions means fewer vehicles each, so random variation shows up more and the
busiest partition drifts further from the mean. At 192 that reaches 14%, which starts to
matter; at 96 it is 7%, which does not. 96 also divides by 2, 3, 4, 6, 8, 12, 16, 24, 32
and 48, so almost any consumer count divides evenly.

**Why now rather than at the benchmark.** Kafka cannot reduce a topic's partition count
at all, and *growing* one is not safe here either: the partition is chosen by
`crc32(vin) % partition_count`, so changing the count remaps every VIN. A vehicle could
move to a new partition while its half-finished idle segment sits in a consumer still
reading the old one, splitting the state the idle state machine depends on. The
asymmetry settles it: choosing too high costs a little broker metadata, choosing too low
cannot be undone.

The batch-envelope mitigation this ADR originally proposed is **not** adopted. It
attacks per-message overhead, whereas the consumer's cost is dominated by per-event
work, so it would help the producer far more than the half that needs help.
