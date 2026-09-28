# Event size measurement

**Measured, not estimated.** Reproduce with the command below.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-28 23:11:08 |
| Commit | recorded by the commit that adds this file |
| Platform | local developer laptop, win32 |
| Serialiser | orjson, the same call the producer hot path uses |
| Seed | 11 (deterministic: a rerun gives identical bytes) |
| Sample | 275,202 events from 200 vehicles over one simulated day |

## Result

| Metric | Bytes |
|---|---|
| Mean | **479.0** |
| Median | 480 |
| p95 | 483 |
| Min | 467 |
| Max | 489 |

The distribution is tight because the field set is fixed; only numeric formatting
and the optional `dtc` list vary.

## Projection to the NFR rate

At the hackathon's 100,000 events/s target:

| Basis | Bytes/event | Throughput | Per day |
|---|---|---|---|
| **Measured (this system)** | **479** | **47.9 MB/s** | **4.14 TB** |
| PDF S4 reference model | 1024 | 102.4 MB/s | 8.8 TB |

This is arithmetic on a measured event size, **not** a measured throughput. No
throughput figure exists yet; ADR-0005 states that explicitly.

## Why it matters

It corrects a documented estimate. ADR-0004 originally projected 250-300 B/event
and 2.2-2.6 TB/day from a hand estimate; the real figure is ~60% larger. The
operational conclusion is unchanged - the Mongo hot tier needs a 48 h TTL rather
than week-long retention - but the number itself is now evidence rather than a guess.

## Reproduce

```bash
uv run python -m simulator.tools.measure_event_size
```
