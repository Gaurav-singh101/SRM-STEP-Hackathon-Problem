# ER diagram — PostgreSQL relational core

Generated from the migrations in `services/batch/batch/migrations/`, which are the source
of truth. Mermaid rather than an image so it stays reviewable in a diff.

The core is in **3NF**. Every deliberate denormalisation is a rollup table, and each is
labelled below with why it exists.

## Reference data (shared, not tenant-scoped)

```mermaid
erDiagram
    powertrains {
        text powertrain_code PK
        boolean burns_fuel
        text description
    }
    vehicle_classes {
        text class_code PK
        text description
    }
    fuel_types {
        text fuel_type PK
        text description
    }
    idle_burn_rates {
        text powertrain_code PK_FK
        text class_code PK_FK
        text formula_version PK
        double litres_per_hour
    }
    powertrains ||--o{ idle_burn_rates : "rate per powertrain"
    vehicle_classes ||--o{ idle_burn_rates : "rate per class"
    powertrains ||--o{ vehicles : classifies
    vehicle_classes ||--o{ vehicles : classifies
    fuel_types ||--o{ vehicles : fuels
    vehicles {
        char vin PK
    }
```

`idle_burn_rates` is keyed on `(powertrain_code, class_code, formula_version)`. The
`formula_version` in the key is what makes a past cost figure reproducible: changing a burn
rate adds a row under a new version rather than overwriting the value a report was
calculated with.

## Tenant core

```mermaid
erDiagram
    tenants {
        text tenant_id PK
        text name
        char currency
        text timezone
    }
    subscriptions {
        bigint subscription_id PK
        text tenant_id FK
        text plan
        timestamptz valid_from
        timestamptz valid_to
    }
    app_users {
        bigint user_id PK
        text tenant_id FK
        text subject
        text role
    }
    fleets {
        bigint fleet_id PK
        text tenant_id FK
        text name
    }
    vehicles {
        char vin PK
        text tenant_id FK
        bigint fleet_id FK
        text powertrain_code FK
        text class_code FK
        text fuel_type FK
        text registration
        integer model_year
        boolean is_active
    }
    drivers {
        bigint driver_id PK
        text tenant_id FK
        text full_name
        text licence_ref
        timestamptz erased_at
    }
    vehicle_assignments {
        bigint assignment_id PK
        text tenant_id FK
        char vin FK
        bigint driver_id FK
        timestamptz assigned_from
        timestamptz assigned_to
    }
    fuel_prices {
        text price_id PK
        text tenant_id FK
        text fuel_type FK
        numeric price_per_litre
        timestamptz effective_from
        timestamptz effective_to
    }

    tenants ||--o{ subscriptions : has
    tenants ||--o{ app_users : has
    tenants ||--o{ fleets : owns
    tenants ||--o{ vehicles : owns
    tenants ||--o{ drivers : employs
    tenants ||--o{ fuel_prices : "prices fuel"
    fleets ||--o{ vehicles : contains
    vehicles ||--o{ vehicle_assignments : "assigned in"
    drivers ||--o{ vehicle_assignments : "assigned to"
```

Three things here are load-bearing rather than incidental:

- **`vehicle_assignments` is a table, not a column on `vehicles`.** A driver-vehicle link is
  temporal: "who was driving this van on Tuesday" is a different question from "who drives it
  now". A `current_driver_id` column would destroy the history that answers the first, and
  that history is also personal data the erasure flow has to reach.
- **`fuel_prices` is temporal, keyed by `effective_from`/`effective_to`.** A cost computed
  last month must stay reproducible after a price change, so prices are appended and closed
  rather than updated (D11).
- **`drivers.erased_at`** exists because the right to erasure anonymises rather than deletes.
  The row survives so the operational record keeps its foreign keys; the identity does not
  (ADR-0007's sibling, migration 007).

## Time-series and derived data

```mermaid
erDiagram
    vehicles {
        char vin PK
    }
    trips {
        bigint trip_id PK
        timestamptz started_at PK
        text tenant_id
        char vin
        timestamptz ended_at
        double distance_km
        double moving_seconds
        text start_geohash
    }
    idle_events {
        bigint idle_event_id PK
        timestamptz started_at PK
        text tenant_id
        char vin
        timestamptz ended_at
        double duration_seconds
        integer sample_count
        double max_gap_seconds
        boolean excessive
        double fuel_litres_est
        numeric cost_est
        text formula_version
        text price_id FK
        integer idle_threshold_seconds
    }
    alerts {
        bigint alert_id PK
        text tenant_id
        char vin
        text alert_type
        text severity
        timestamptz triggering_event_ts
        timestamptz ingest_ts
        timestamptz persisted_at
        jsonb detail
        timestamptz acknowledged_at
    }
    audit_log {
        bigint audit_id PK
        timestamptz at PK
        text tenant_id
        text actor_id
        text action
        text resource_type
        text resource_id
        text correlation_id
        text client_ip_masked
    }
    erasure_log {
        bigint erasure_id PK
        text tenant_id FK
        text subject_type
        text subject_ref
        timestamptz requested_at
        text requested_by
        timestamptz completed_at
        jsonb stores_affected
    }
    reconciliation_log {
        bigint run_id PK
        timestamptz started_at
        integer events_consumed
        integer vehicle_days_reconciled
        integer events_unreconcilable
        jsonb unreconcilable_reasons
    }

    vehicles ||--o{ trips : drives
    vehicles ||--o{ idle_events : idles
    vehicles ||--o{ alerts : raises
    fuel_prices ||--o{ idle_events : "priced at"
```

`trips`, `idle_events` and `audit_log` are **range-partitioned by month** on their time
column, which is why each has a composite primary key including that column — PostgreSQL
requires the partition key in every unique index.

`alerts` is deliberately **not partitioned**. Its suppression guarantee needs a unique index
on `(vin, alert_type, idle_event_started_at)` alone, and adding a timestamp to satisfy the
partitioning rule would permit exactly the duplicate alerts the index exists to prevent.
Alerts are low volume — thousands per day against billions of telemetry rows — so losing
partition pruning costs little, while a database-enforced guarantee that an alert storm is
impossible is worth a great deal.

## Rollups — the deliberate denormalisations

```mermaid
erDiagram
    vehicle_daily {
        text tenant_id PK
        char vin PK
        date day PK
        double idle_seconds
        integer idle_event_count
        integer excessive_count
        double fuel_litres_est
        numeric cost_est
        double moving_seconds
        double distance_km
        boolean was_active
        timestamptz recomputed_at
    }
    fleet_daily {
        text tenant_id PK
        bigint fleet_id PK
        date day PK
    }
    vehicle_utilisation {
        text tenant_id PK
        char vin PK
        date window_end PK
        double score
        text band
        double component_spread
        text abnormal_reason
        integer baseline_sample_size
    }
    class_baselines {
        text tenant_id PK
        text class_code PK
        date window_end PK
        integer window_days
        double median_moving_hours_per_day
        double median_distance_km_per_day
        double median_active_day_ratio
        double mad_moving_hours_per_day
        double mad_distance_km_per_day
        double mad_active_day_ratio
        integer sample_size
    }
    utilisation_window {
        text tenant_id PK
        date window_end PK
        integer vehicles_active
        integer scored
        integer unscored_no_telemetry
        integer unscored_class_too_small
    }
    idle_events ||..o{ vehicle_daily : "aggregated into"
    trips ||..o{ vehicle_daily : "aggregated into"
    vehicle_daily ||..o{ fleet_daily : "rolled up to"
    vehicle_daily ||..o{ vehicle_utilisation : scores
    class_baselines ||..o{ vehicle_utilisation : "scored against"
```

| Rollup | Why it exists |
|---|---|
| `vehicle_daily` | One row per vehicle-day instead of one per idle event. The leaderboard reads this: measured, aggregating raw events over a 30-day window cannot meet p95 < 200 ms, and no index fixes a query that must read most of the table (`docs/evidence/sql/`). |
| `fleet_daily` | Derived from `vehicle_daily`, not from raw events — one derivation step, so the two can never disagree about the same day. |
| `vehicle_utilisation` | Scores are expensive (class baselines, median/MAD) and change daily, not per request. |
| `class_baselines` | Stored so a score is reproducible: you can see what it was compared against. |
| `utilisation_window` | Per-window accounting, with a CHECK that `scored + unscored_no_telemetry + unscored_class_too_small = vehicles_active`. Added because the API reported "0 vehicles unscored" while 1,808 were. |

Every rollup write is an **upsert keyed on the rollup key**, which is what makes re-running
a day correct rather than duplicating it — and therefore what makes late-event
reconciliation cheap (ADR-0007).

## MongoDB and Redis

Not in this diagram because neither is relational, but both are part of the data model:

| Store | Holds | Key |
|---|---|---|
| **MongoDB** `telemetry_raw` | Raw enriched telemetry, TTL on `ingest_ts` (48h default) | unique `event_id`; index `(vin, ts)` |
| **Redis** | Latest vehicle state, per-vehicle idle machine state, dedup replay window, API response cache, rate-limit counters | `vehicle:{vin}`, `wm:{vin}` |

The TTL is on **`ingest_ts`, not event time**, deliberately: a backdated event must not
expire the moment it is written. That choice is also what bounds late-event reconciliation
(ADR-0007).
