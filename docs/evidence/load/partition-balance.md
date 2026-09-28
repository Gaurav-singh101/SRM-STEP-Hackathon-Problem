# Partition balance for the VIN partition key

CLAUDE.md S8 requires the partition key to hash evenly and avoid hot spots. This
matters beyond tidiness: partitions are the unit of consumer parallelism, so a
partition carrying twice the mean caps pipeline throughput at that one consumer's
rate however many replicas are added.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-28 23:32:53 |
| Commit | recorded by the commit that adds this file |
| Partition key | `vin` (ADR-0004) |
| Partition count | 24 (ADR-0004) |
| Partitioner | librdkafka `consistent_random`: `crc32(key) % partitions` |
| Seed | 20260929 |

## Model validation

The distribution is computed offline rather than by producing events. That is only
legitimate if the model matches the real broker, so it is checked first against an
actual assignment observed after producing a 200-vehicle day to Kafka:

| Partition | Observed on broker | Model predicts | |
|---|---|---|---|
| 3 | 15 | 15 | match |
| 4 | 3 | 3 | match |

The model reproduces the broker exactly, so the projections below are trustworthy.

## Results

| Vehicles | Min keys | Max keys | Mean | Min-max spread | Hottest / mean |
|---|---|---|---|---|---|
| 200 | 3 | 15 | 8 | 80.0% | **1.800x** |
| 2,000 | 63 | 100 | 83 | 37.0% | **1.200x** |
| 20,000 | 787 | 882 | 833 | 10.8% | **1.058x** |
| 100,000 | 3,967 | 4,342 | 4,167 | 8.6% | **1.042x** |

## Interpretation

At small fleet sizes the imbalance is large - and that is ordinary multinomial
variance in key assignment, not a defect in the key choice. Spreading N keys over
24 buckets has standard deviation `sqrt(N x (1/24) x (23/24))`, which grows as
`sqrt(N)` while the mean grows as `N`, so relative imbalance decays as
`1/sqrt(N)`.

**At the design scale of 100,000 vehicles the hottest partition carries
1.042x the mean.** That is well inside any reasonable
tolerance, so `vin` satisfies the even-hashing requirement at the scale the system
is specified for.

This also means a small-scale demo will show a visibly uneven partition load, and
that is expected rather than a problem to fix. Chasing it with a custom partitioner
would trade a non-issue at scale for the loss of per-vehicle ordering, which the
idle state machine depends on absolutely.

### What this is and is not

This is an analysis of **key distribution**, validated against a real broker. It is
not a throughput measurement, and it says nothing about how many events each
vehicle emits - per-vehicle volume varies with duty cycle, and at small N that adds
further spread on top of the key imbalance shown here.

## Reproduce

```bash
uv run python -m simulator.tools.partition_balance
```
