-- ===========================================================================
-- 002_partitions_and_rollups
--
-- Part 1: automated monthly partition management for the time-series tables.
-- Part 2: rollup tables. These are the ONLY deliberate denormalisation in the
--         schema, and the reason is recorded against each one (PDF S8 requires
--         documenting any departure from 3NF).
-- ===========================================================================

-- --------------------------------------------------------------------------
-- Part 1: partition management
--
-- Monthly range partitions on event time. Every dashboard query is time-bounded,
-- so the planner prunes to one or two partitions instead of scanning billions of
-- rows; and retention becomes DROP TABLE rather than DELETE, which on a table
-- this size is the difference between seconds and hours.
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ensure_monthly_partition(
    parent_table text,
    month_start  date
) RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
    partition_name text;
    range_end      date;
BEGIN
    range_end := (month_start + interval '1 month')::date;
    partition_name := format('%s_%s', parent_table, to_char(month_start, 'YYYYMM'));

    IF NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relname = partition_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            partition_name, parent_table, month_start, range_end
        );
    END IF;

    RETURN partition_name;
END;
$$;

COMMENT ON FUNCTION ensure_monthly_partition IS
    'Idempotently create one monthly partition. Called by the batch job ahead of '
    'time so an insert never arrives before its partition exists.';

CREATE OR REPLACE FUNCTION ensure_partitions_around(
    parent_table   text,
    months_back    integer DEFAULT 1,
    months_forward integer DEFAULT 2
) RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    offset_months integer;
    created       integer := 0;
BEGIN
    FOR offset_months IN -months_back..months_forward LOOP
        PERFORM ensure_monthly_partition(
            parent_table,
            (date_trunc('month', now()) + make_interval(months => offset_months))::date
        );
        created := created + 1;
    END LOOP;
    RETURN created;
END;
$$;

-- Create the current window for every partitioned table.
SELECT ensure_partitions_around('idle_events');
SELECT ensure_partitions_around('trips');
-- alerts is intentionally not partitioned; see the rationale in 001_core.sql.
SELECT ensure_partitions_around('audit_log');

-- DEFAULT partitions as a safety net: an event whose timestamp falls outside the
-- prepared window must never fail the insert and stall a consumer. The trade-off
-- is that rows landing here block later creation of an overlapping partition, so
-- the batch job monitors them and the operator moves them out. An unmonitored
-- default partition is how these schemas silently rot.
CREATE TABLE idle_events_default PARTITION OF idle_events DEFAULT;
CREATE TABLE trips_default       PARTITION OF trips       DEFAULT;
CREATE TABLE audit_log_default   PARTITION OF audit_log   DEFAULT;

-- --------------------------------------------------------------------------
-- Part 2: rollups (DELIBERATE DENORMALISATION)
--
-- Why: the fleet overview and leaderboard aggregate idle cost across whole
-- fleets over 30-day windows. Computing that from raw idle_events on every page
-- load is an unbounded scan whose cost grows with history, which the API's
-- p95 < 200 ms target cannot absorb.
--
-- Why tables and not materialised views: a materialised view refreshes wholly.
-- These are refreshed incrementally per (vehicle, day) key, which is what makes
-- late-event reconciliation cheap - a late event recomputes one day for one
-- vehicle rather than rebuilding the fleet's history (D6).
--
-- Correctness: the rollups are derived, never authoritative. idle_events remains
-- the source of truth, and the batch job can rebuild any key from it.
-- --------------------------------------------------------------------------

CREATE TABLE vehicle_daily (
    tenant_id             text     NOT NULL,
    vin                   char(17) NOT NULL,
    -- Local calendar day for the tenant's timezone, not UTC: "which day did this
    -- cost fall on" is a local-time question and billing periods are local.
    day                   date     NOT NULL,

    idle_seconds          double precision NOT NULL DEFAULT 0,
    idle_event_count      integer          NOT NULL DEFAULT 0,
    excessive_count       integer          NOT NULL DEFAULT 0,
    fuel_litres_est       double precision NOT NULL DEFAULT 0,
    cost_est              numeric(14,4)    NOT NULL DEFAULT 0,
    currency              char(3),

    moving_seconds        double precision NOT NULL DEFAULT 0,
    distance_km           double precision NOT NULL DEFAULT 0,
    -- Feeds the active_day_ratio component of the utilisation score (D3).
    was_active            boolean          NOT NULL DEFAULT false,

    formula_version       text     NOT NULL,
    -- When this key was last recomputed. A late event bumps it, so a stale
    -- rollup is detectable rather than silently wrong.
    recomputed_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, vin, day)
);
CREATE INDEX vehicle_daily_leaderboard_idx
    ON vehicle_daily (tenant_id, day DESC, cost_est DESC);
CREATE INDEX vehicle_daily_vin_idx ON vehicle_daily (vin, day DESC);

CREATE TABLE fleet_daily (
    tenant_id          text   NOT NULL,
    fleet_id           bigint NOT NULL,
    day                date   NOT NULL,

    idle_seconds       double precision NOT NULL DEFAULT 0,
    idle_event_count   integer          NOT NULL DEFAULT 0,
    excessive_count    integer          NOT NULL DEFAULT 0,
    fuel_litres_est    double precision NOT NULL DEFAULT 0,
    cost_est           numeric(14,4)    NOT NULL DEFAULT 0,
    currency           char(3),

    vehicle_count      integer NOT NULL DEFAULT 0,
    active_vehicle_count integer NOT NULL DEFAULT 0,
    -- So the UI can state "excludes N BEVs" beside a fleet total. A fleet figure
    -- that silently omits vehicles is a wrong figure, and a finance user would
    -- under-read cost without ever knowing (D9).
    excluded_bev_count integer NOT NULL DEFAULT 0,

    formula_version    text NOT NULL,
    recomputed_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, fleet_id, day)
);
CREATE INDEX fleet_daily_trend_idx ON fleet_daily (tenant_id, day DESC);

-- Utilisation is scored over a rolling window, not a day, so it gets its own
-- table keyed by window end. Components are stored beside the score because a
-- single opaque number does not survive "why is this 31?" (D3).
CREATE TABLE vehicle_utilisation (
    tenant_id              text     NOT NULL,
    vin                    char(17) NOT NULL,
    window_end             date     NOT NULL,
    window_days            integer  NOT NULL CHECK (window_days > 0),

    score                  double precision NOT NULL CHECK (score BETWEEN 0 AND 100),
    band                   text     NOT NULL,
    active_day_component   double precision NOT NULL,
    moving_hours_component double precision NOT NULL,
    distance_component     double precision NOT NULL,
    idle_share             double precision NOT NULL,
    max_abs_z              double precision NOT NULL,
    abnormal_reason        text,
    -- Peer count behind the baseline. A score from 3 peers is far weaker than one
    -- from 300, and the UI says so rather than presenting both identically.
    baseline_sample_size   integer  NOT NULL,

    formula_version        text     NOT NULL,
    recomputed_at          timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, vin, window_end),
    CONSTRAINT vehicle_utilisation_band CHECK (
        band IN ('low', 'normal', 'high', 'abnormal')
    )
);
CREATE INDEX vehicle_utilisation_band_idx
    ON vehicle_utilisation (tenant_id, window_end DESC, score ASC);
-- Partial index for the "low or abnormal utilisation" view, which is the only
-- one operations actually opens.
CREATE INDEX vehicle_utilisation_attention_idx
    ON vehicle_utilisation (tenant_id, window_end DESC, score ASC)
    WHERE band IN ('low', 'abnormal');

-- Baseline per vehicle class per window, so a score can be explained after the
-- fact ("scored against these peers, this median") rather than only recomputed.
CREATE TABLE class_baselines (
    tenant_id                    text    NOT NULL,
    class_code                   text    NOT NULL REFERENCES vehicle_classes,
    window_end                   date    NOT NULL,
    window_days                  integer NOT NULL,
    median_moving_hours_per_day  double precision NOT NULL,
    median_distance_km_per_day   double precision NOT NULL,
    median_active_day_ratio      double precision NOT NULL,
    mad_moving_hours_per_day     double precision NOT NULL,
    mad_distance_km_per_day      double precision NOT NULL,
    mad_active_day_ratio         double precision NOT NULL,
    sample_size                  integer NOT NULL,
    formula_version              text    NOT NULL,
    recomputed_at                timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, class_code, window_end)
);
