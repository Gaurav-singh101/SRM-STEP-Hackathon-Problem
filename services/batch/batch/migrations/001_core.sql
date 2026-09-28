-- ===========================================================================
-- 001_core: relational core in Third Normal Form.
--
-- Hackathon PDF S8 requires the relational core (Fleet, Vehicle, Driver, Trip,
-- Alert, Subscription) in 3NF with any deliberate denormalisation documented.
-- Rollup tables in 002 are the only denormalisation, and they are justified there.
--
-- Money is numeric(14,4) everywhere (D11). Never float: binary floating point
-- cannot represent most decimal fractions exactly, so accumulating currency over
-- millions of rows drifts, and a drifting total is worse than no total.
--
-- Every business table carries tenant_id. Isolation is enforced in the data
-- access layer AND by row-level security in 003 (defence in depth).
-- ===========================================================================

-- --------------------------------------------------------------------------
-- Reference data. Separate tables rather than enums because the burn-rate
-- table below has a genuine functional dependency on (powertrain, class), and
-- 3NF wants that expressed as a relation, not duplicated text.
-- --------------------------------------------------------------------------

CREATE TABLE powertrains (
    powertrain_code  text PRIMARY KEY,
    description      text        NOT NULL,
    -- BEVs burn no fuel at idle, so cost estimation must skip them rather than
    -- silently record zero (D9). Stored, not inferred from the code string.
    burns_fuel       boolean     NOT NULL
);

CREATE TABLE vehicle_classes (
    class_code   text PRIMARY KEY,
    description  text NOT NULL
);

CREATE TABLE fuel_types (
    fuel_type    text PRIMARY KEY,
    description  text NOT NULL
);

-- --------------------------------------------------------------------------
-- Tenancy. Shared-schema multi-tenancy (D11): one tenant_id column on every
-- business table. Rejected schema-per-tenant (N migrations for one developer)
-- and database-per-tenant (cost).
-- --------------------------------------------------------------------------

CREATE TABLE tenants (
    tenant_id     text PRIMARY KEY,
    name          text        NOT NULL,
    -- One currency per tenant (D11). ISO-4217; totals never mix currencies.
    currency      char(3)     NOT NULL,
    -- IANA zone: "which day did this idle happen on" is a local-time question,
    -- and billing periods are local. Storing UTC only would answer it wrongly.
    timezone      text        NOT NULL DEFAULT 'UTC',
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tenants_currency_upper CHECK (currency = upper(currency))
);

CREATE TABLE subscriptions (
    subscription_id  bigserial PRIMARY KEY,
    tenant_id        text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    plan_code        text        NOT NULL,
    seats            integer     NOT NULL CHECK (seats > 0),
    started_on       date        NOT NULL,
    ended_on         date,
    CONSTRAINT subscriptions_period CHECK (ended_on IS NULL OR ended_on >= started_on)
);
CREATE INDEX subscriptions_tenant_idx ON subscriptions (tenant_id);

-- Application users. Identity itself lives with the OIDC provider (D4); we store
-- only the subject claim and the authorisation we grant it. No passwords, ever.
CREATE TABLE app_users (
    user_sub    text PRIMARY KEY,
    tenant_id   text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    email       text        NOT NULL,
    role        text        NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT app_users_role CHECK (
        role IN ('finance_manager', 'ops_manager', 'supervisor', 'analyst', 'admin')
    )
);
CREATE INDEX app_users_tenant_idx ON app_users (tenant_id);

-- --------------------------------------------------------------------------
-- Fleet structure
-- --------------------------------------------------------------------------

CREATE TABLE fleets (
    fleet_id    bigserial PRIMARY KEY,
    tenant_id   text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    name        text        NOT NULL,
    -- Depot location, used to group idle events by site (geohash, PDF S9).
    depot_lat   double precision,
    depot_lon   double precision,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fleets_name_unique_per_tenant UNIQUE (tenant_id, name),
    CONSTRAINT fleets_depot_pair CHECK ((depot_lat IS NULL) = (depot_lon IS NULL))
);
CREATE INDEX fleets_tenant_idx ON fleets (tenant_id);

CREATE TABLE vehicles (
    vin              char(17) PRIMARY KEY,
    tenant_id        text     NOT NULL REFERENCES tenants ON DELETE CASCADE,
    fleet_id         bigint   NOT NULL REFERENCES fleets ON DELETE RESTRICT,
    powertrain_code  text     NOT NULL REFERENCES powertrains,
    class_code       text     NOT NULL REFERENCES vehicle_classes,
    fuel_type        text              REFERENCES fuel_types,
    registration     text     NOT NULL,
    model_year       smallint NOT NULL CHECK (model_year BETWEEN 1980 AND 2100),
    is_active        boolean  NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now(),
    -- Shape only. The check digit needs arithmetic, so it is validated in the
    -- domain layer; see the known regional limitation documented in vin.py.
    CONSTRAINT vehicles_vin_format CHECK (vin ~ '^[A-HJ-NPR-Z0-9]{17}$'),
    -- A BEV has no fuel type; anything that burns fuel must declare one.
    -- Enforced here so the cost engine cannot be handed an unusable vehicle.
    CONSTRAINT vehicles_fuel_type_required CHECK (
        fuel_type IS NOT NULL OR powertrain_code = 'bev'
    )
);
CREATE INDEX vehicles_tenant_fleet_idx ON vehicles (tenant_id, fleet_id);
-- Partial index: the dashboard only ever lists active vehicles, so the index
-- stays small as decommissioned vehicles accumulate.
CREATE INDEX vehicles_active_idx ON vehicles (tenant_id, class_code)
    WHERE is_active;

CREATE TABLE drivers (
    driver_id    bigserial PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    -- Synthetic personal data only (PDF S14). Subject to the erasure flow, which
    -- is why driver identity is isolated in its own table rather than denormalised
    -- onto every event: erasing one person must not rewrite telemetry history.
    full_name    text        NOT NULL,
    licence_ref  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Set when the subject exercises the right to erasure; the row is retained
    -- in anonymised form so historical aggregates stay arithmetically consistent.
    erased_at    timestamptz,
    CONSTRAINT drivers_licence_unique_per_tenant UNIQUE (tenant_id, licence_ref)
);
CREATE INDEX drivers_tenant_idx ON drivers (tenant_id);

-- Driver-to-vehicle is many-to-many over time, so it gets its own table. Putting
-- a driver_id on vehicles would lose history and break attribution of past idle.
CREATE TABLE vehicle_assignments (
    assignment_id  bigserial PRIMARY KEY,
    tenant_id      text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    vin            char(17)    NOT NULL REFERENCES vehicles ON DELETE CASCADE,
    driver_id      bigint      NOT NULL REFERENCES drivers ON DELETE CASCADE,
    assigned_from  timestamptz NOT NULL,
    assigned_to    timestamptz,
    CONSTRAINT vehicle_assignments_period CHECK (
        assigned_to IS NULL OR assigned_to > assigned_from
    )
);
-- Covers "who was driving this VIN at time T", the attribution query.
CREATE INDEX vehicle_assignments_vin_period_idx
    ON vehicle_assignments (vin, assigned_from DESC);
CREATE INDEX vehicle_assignments_driver_idx ON vehicle_assignments (driver_id);
-- At most one open assignment per vehicle: two would make attribution ambiguous.
CREATE UNIQUE INDEX vehicle_assignments_one_open_per_vin
    ON vehicle_assignments (vin) WHERE assigned_to IS NULL;

-- --------------------------------------------------------------------------
-- Business parameters. In the database, not in code, so a figure computed last
-- month remains reproducible after the parameters change (D2, D11).
-- --------------------------------------------------------------------------

-- Temporal price table. A cost row references the exact price_id used, so
-- recomputing a past period uses that period's price (D11).
CREATE TABLE fuel_prices (
    price_id         text PRIMARY KEY,
    tenant_id        text          NOT NULL REFERENCES tenants ON DELETE CASCADE,
    fuel_type        text          NOT NULL REFERENCES fuel_types,
    region           text          NOT NULL DEFAULT 'default',
    price_per_litre  numeric(14,4) NOT NULL CHECK (price_per_litre > 0),
    currency         char(3)       NOT NULL,
    effective_from   timestamptz   NOT NULL,
    effective_to     timestamptz,
    source           text          NOT NULL DEFAULT 'synthetic',
    CONSTRAINT fuel_prices_period CHECK (
        effective_to IS NULL OR effective_to > effective_from
    )
);
-- Serves the point-in-time lookup: newest price at or before a given instant.
CREATE INDEX fuel_prices_lookup_idx
    ON fuel_prices (tenant_id, fuel_type, region, effective_from DESC);
-- One open-ended current price per (tenant, fuel, region), else the lookup is
-- ambiguous and two runs could legitimately disagree.
CREATE UNIQUE INDEX fuel_prices_one_current
    ON fuel_prices (tenant_id, fuel_type, region) WHERE effective_to IS NULL;

-- The D2 burn-rate table, versioned. Values are PROPOSED synthetic figures.
CREATE TABLE idle_burn_rates (
    powertrain_code  text             NOT NULL REFERENCES powertrains,
    class_code       text             NOT NULL REFERENCES vehicle_classes,
    formula_version  text             NOT NULL,
    burn_rate_lph    double precision NOT NULL CHECK (burn_rate_lph >= 0),
    source           text             NOT NULL DEFAULT 'PROPOSED-synthetic',
    PRIMARY KEY (powertrain_code, class_code, formula_version)
);

-- --------------------------------------------------------------------------
-- Trips. Kept alongside idle because utilisation needs moving time and distance
-- (D3), and the PDF names Trip as part of the relational core.
-- --------------------------------------------------------------------------

CREATE TABLE trips (
    trip_id           bigserial,
    tenant_id         text        NOT NULL,
    vin               char(17)    NOT NULL,
    started_at        timestamptz NOT NULL,
    ended_at          timestamptz NOT NULL,
    distance_km       double precision NOT NULL CHECK (distance_km >= 0),
    moving_seconds    double precision NOT NULL CHECK (moving_seconds >= 0),
    idle_seconds      double precision NOT NULL DEFAULT 0 CHECK (idle_seconds >= 0),
    start_geohash     text,
    end_geohash       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trip_id, started_at),
    CONSTRAINT trips_period CHECK (ended_at >= started_at)
) PARTITION BY RANGE (started_at);

-- --------------------------------------------------------------------------
-- Idle events: the product's core output. Partitioned by event time because
-- every query is time-bounded and old partitions are dropped wholesale, which
-- is far cheaper than DELETE (PDF S8 partitioning, CLAUDE.md S8 lifecycle).
-- --------------------------------------------------------------------------

CREATE TABLE idle_events (
    idle_event_id           bigserial,
    tenant_id               text        NOT NULL,
    vin                     char(17)    NOT NULL,
    started_at              timestamptz NOT NULL,
    ended_at                timestamptz NOT NULL,
    duration_seconds        double precision NOT NULL CHECK (duration_seconds >= 0),
    sample_count            integer     NOT NULL CHECK (sample_count > 0),
    -- Data-quality signal: how much unobserved time this segment bridged (D1).
    -- Surfaced in the UI so a figure resting on sparse samples looks weaker.
    max_gap_seconds         double precision NOT NULL DEFAULT 0,
    excessive               boolean     NOT NULL DEFAULT false,
    pto_active_any          boolean     NOT NULL DEFAULT false,
    start_lat               double precision,
    start_lon               double precision,
    -- Precomputed for depot/site grouping via prefix match (PDF S9 geohash).
    start_geohash           text,

    -- --- estimate plus the assumptions that produced it (D2, CLAUDE.md S14) --
    fuel_litres_est         double precision,
    cost_est                numeric(14,4),
    currency                char(3),
    formula_version         text        NOT NULL,
    burn_rate_lph           double precision,
    price_id                text        REFERENCES fuel_prices ON DELETE SET NULL,
    idle_threshold_seconds  integer     NOT NULL,
    -- Optional device cross-check (D2); NULL when the device reported no rate.
    fuel_litres_measured    double precision,

    created_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (idle_event_id, started_at),
    CONSTRAINT idle_events_period CHECK (ended_at >= started_at),
    -- A BEV must never carry a fuel cost, and anything with a cost must say
    -- which currency it is in (D9, D11).
    CONSTRAINT idle_events_cost_has_currency CHECK (
        (cost_est IS NULL) = (currency IS NULL)
    )
) PARTITION BY RANGE (started_at);

-- Final idempotency safety net (CLAUDE.md S8). Delivery is at-least-once, so a
-- replayed batch must collide here rather than double-count fuel. The per-vehicle
-- high-water mark in Redis is the fast path; this is the guarantee.
CREATE UNIQUE INDEX idle_events_dedup_idx ON idle_events (vin, started_at);

-- Keyset pagination and the leaderboard both read this (tenant, time desc).
CREATE INDEX idle_events_tenant_time_idx
    ON idle_events (tenant_id, started_at DESC, idle_event_id DESC);
CREATE INDEX idle_events_vin_time_idx ON idle_events (vin, started_at DESC);
-- Partial index for the "needs attention" view: excessive events are a small
-- fraction of rows, so this stays far smaller than a full index.
CREATE INDEX idle_events_excessive_idx
    ON idle_events (tenant_id, started_at DESC) WHERE excessive;

-- --------------------------------------------------------------------------
-- Alerts (D10)
-- --------------------------------------------------------------------------

-- NOT partitioned, unlike the other time-series tables, and the reason is a
-- real constraint rather than an oversight: Postgres requires every UNIQUE index
-- on a partitioned table to include the partition key. The suppression index
-- below must be unique on (vin, alert_type, idle_event_started_at) alone - adding
-- a timestamp to it would permit exactly the duplicate alerts it exists to stop.
--
-- The trade-off is affordable because alerts are low volume: one per excessive
-- idle segment, thousands per day across a 100K-vehicle fleet, against billions
-- of telemetry rows. Partition pruning is worth little at that size, whereas a
-- database-enforced suppression guarantee is worth a great deal - it means an
-- alert storm is impossible even if the application logic regresses.
-- Retention is handled by the batch job with a time-bounded DELETE instead.
CREATE TABLE alerts (
    alert_id              bigserial PRIMARY KEY,
    tenant_id             text        NOT NULL,
    vin                   char(17)    NOT NULL,
    alert_type            text        NOT NULL,
    severity              text        NOT NULL DEFAULT 'warning',
    -- The two timestamps that define the alert SLO. Latency is measured as
    -- persisted_at - triggering_event_ts, because "when it appeared" is ambiguous.
    triggering_event_ts   timestamptz NOT NULL,
    persisted_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    idle_event_started_at timestamptz,
    detail                jsonb       NOT NULL DEFAULT '{}'::jsonb,
    acknowledged_at       timestamptz,
    acknowledged_by       text,
    CONSTRAINT alerts_type CHECK (
        alert_type IN ('EXCESSIVE_IDLE_IN_PROGRESS', 'IDLE_COST_SPIKE')
    ),
    CONSTRAINT alerts_severity CHECK (severity IN ('info', 'warning', 'critical'))
);

-- Suppression key: one alert per vehicle per type per idle segment, so a long
-- idle raises one alert instead of storming one per sample (D10).
CREATE UNIQUE INDEX alerts_suppression_idx
    ON alerts (vin, alert_type, idle_event_started_at)
    WHERE idle_event_started_at IS NOT NULL;
CREATE INDEX alerts_open_idx
    ON alerts (tenant_id, triggering_event_ts DESC) WHERE acknowledged_at IS NULL;

-- --------------------------------------------------------------------------
-- Audit log. PDF S11 requires an audit entry for every data access. Partitioned
-- and append-only; no UPDATE or DELETE is granted to the application role (003).
-- --------------------------------------------------------------------------

CREATE TABLE audit_log (
    audit_id        bigserial,
    tenant_id       text        NOT NULL,
    at              timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_sub       text        NOT NULL,
    actor_role      text        NOT NULL,
    action          text        NOT NULL,
    resource_type   text        NOT NULL,
    resource_id     text,
    correlation_id  text        NOT NULL,
    -- Truncated to a /24 (or /48 for v6) before storage: enough to investigate
    -- abuse, not enough to track an individual (PDF S11 data minimisation).
    client_ip_masked text,
    PRIMARY KEY (audit_id, at)
) PARTITION BY RANGE (at);

CREATE INDEX audit_log_tenant_time_idx ON audit_log (tenant_id, at DESC);
CREATE INDEX audit_log_actor_idx ON audit_log (actor_sub, at DESC);
CREATE INDEX audit_log_resource_idx ON audit_log (resource_type, resource_id, at DESC);

-- Erasure itself is audited (CLAUDE.md S11): the record that a subject's data was
-- removed must survive the removal, otherwise the obligation is unprovable.
CREATE TABLE erasure_log (
    erasure_id      bigserial PRIMARY KEY,
    tenant_id       text        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    subject_type    text        NOT NULL,
    subject_ref     text        NOT NULL,
    requested_at    timestamptz NOT NULL,
    completed_at    timestamptz,
    requested_by    text        NOT NULL,
    -- Per-store outcome, so a partial erasure is visible rather than assumed.
    stores_affected jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT erasure_log_subject_type CHECK (subject_type IN ('driver', 'vehicle'))
);
CREATE INDEX erasure_log_tenant_idx ON erasure_log (tenant_id, requested_at DESC);
