# ADR-0004: Event contract, topics and partitioning

- **Status:** Accepted
- **Date:** 2026-09-29
- **Resolves:** D7, and the partition key/count question
- **Classification:** Approved project decision, extending a documented gap in the hackathon PDF.

## Context

The PDF's illustrative event is
`{vin, ts, lat, lon, speed_kmh, soc_pct, odo_km, dtc[], evt, seq}`. It carries **no
ignition, engine, RPM or fuel signal**, so idle detection is impossible from it
as given. CLAUDE.md D7 anticipated this; reading the PDF confirmed it.

## Decision

`schemas/telemetry.raw.v1.json` is the **single source of truth** for the wire
contract. The Pydantic model in `fleet_common.events` is validated against it by
contract tests, so neither side can drift.

Fields added beyond the PDF example: `ignition` (required), `engine_state`, `rpm`,
`engine_hours`, `fuel_level_pct`, `fuel_rate_lph`, `gps_valid`, `hdop`,
`pto_active`, `heading_deg`, plus `schema_version`, `event_id`, `tenant_id`,
`device_id`.

Three choices worth recording:

- **`powertrain` is deliberately NOT on the event.** It belongs on the vehicle
  master in Postgres. On every event it would cost bytes at 100K events/s and
  create a second source of truth that can disagree with itself.
- **`engine_state` is a hint, not truth.** OEMs compute it inconsistently, so idle
  detection derives its own state from `ignition` + `speed_kph`. The field is kept
  for cross-checking and DLQ reason codes.
- **`ingest_ts` is assigned by the processor, never read from the device.** A
  device clock is bug- and attacker-controlled, so latency measured against it
  would be meaningless.

**Both `event_id` and `seq` are required** (ruling G10, a tightening of CLAUDE.md
S8's "or"). They serve different jobs: `seq` gives an O(1) per-vehicle high-water
mark, which is the only dedup that scales to 100K events/s without one key per
event; `event_id` (UUIDv7, time-ordered so it indexes well) is the unique key in
the sink and the final safety net.

**Topics and partitioning:**

| Topic | Purpose |
|---|---|
| `telemetry.raw` | Ingest |
| `telemetry.dlq` | Schema, VIN or range failures, with a reason code |
| `telemetry.late` | Events beyond allowed lateness, reconciled by the batch job |
| `idle.events` | Closed idle segments |
| `alerts` | Real-time alerts (ADR-0010) |

**Partition key is `vin`**, which guarantees per-vehicle ordering - required,
because the idle state machine is a per-vehicle sequential fold. **24 partitions**:
divisible by 2, 3, 4, 6, 8 and 12, so consumer count can scale without
repartitioning. Locally replication factor 1; in cloud 3 with
`min.insync.replicas=2` and an idempotent producer at `acks=all`.

**Capacity.** The PDF's S4 reference model assumes ~1 KB/event, giving ~8.8 TB/day
at 100K events/s.

Our own wire format has now been **measured**, not estimated: 479 bytes/event mean
over 275,202 generated events (median 480, p95 483 - the distribution is tight
because the field set is fixed). At 100K events/s that is **47.9 MB/s, or 4.14
TB/day**.

> An earlier draft of this ADR estimated 250-300 B/event and 2.2-2.6 TB/day. That
> estimate was wrong by roughly 60%, and is corrected here from the measurement.
> Evidence: `docs/evidence/load/event-size.md`.

Both figures belong in the solution document: the PDF's for framing the problem,
ours for describing this system. Either way the conclusion is the same and it is
the one that matters - the hot tier needs aggressive TTL (48 h in Mongo by
default), not week-long retention.

## Consequences

- Adding or removing a field requires a schema change, a model change and a
  contract-test update in the same commit.
- Invalid events go to the DLQ with a reason code; they are never dropped and
  never crash the consumer.
- JSON now, not Avro. Avro would roughly halve the bytes but adds a schema
  registry to operate; recorded as a future step rather than done.
- Implementation: `schemas/telemetry.raw.v1.json`, `libs/fleet_common/fleet_common/events.py`.
  Tests: `libs/fleet_common/tests/test_contract_schema.py` (32 cases).
