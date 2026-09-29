-- ===========================================================================
-- 009_reconciliation_log
--
-- CLAUDE.md S8: late events are "reconciled by the batch job. Never silently
-- drop." The processor held up its end - events past the lateness window go to
-- telemetry.late with a reason rather than into the bin. Nothing consumed that
-- topic, so the guarantee was not actually met: the data was not dropped, but it
-- reached no store either, which is the same thing to anyone reading a cost
-- figure.
--
-- This table is what makes the guarantee demonstrable rather than asserted
-- (ADR-0007). Every reconciliation run records what it consumed, what it fixed,
-- and - the important part - what it could NOT fix and why.
--
-- The unreconcilable count exists because reconciliation has a hard boundary.
-- Re-segmenting a vehicle-day needs that day's complete raw sample set, and raw
-- telemetry lives in MongoDB under a TTL (48h by default) sized by cost: at the
-- measured 479 B/event, 100K events/s is 4.14 TB/day. An event later than the TTL
-- cannot be reconciled, and recomputing a segment from half its samples would be
-- worse than leaving it stale, because the result would look current.
--
-- So the count is the deliverable. "412 events could not be reconciled because
-- their raw samples had expired" is something an operator can act on - raise the
-- TTL, or accept it. "Reconciliation complete" over the same data is false. This
-- table makes the first available and the second impossible to write honestly.
-- ===========================================================================

CREATE TABLE reconciliation_log (
    run_id                  bigserial PRIMARY KEY,
    started_at              timestamptz NOT NULL DEFAULT now(),
    finished_at             timestamptz,

    -- What arrived.
    events_consumed         integer NOT NULL DEFAULT 0
                              CHECK (events_consumed >= 0),
    -- What was put right. A vehicle-day is the unit of work: re-running the state
    -- machine over one day's complete samples is the only way to get the same
    -- answer the stream would have produced had the event arrived on time.
    vehicle_days_reconciled integer NOT NULL DEFAULT 0
                              CHECK (vehicle_days_reconciled >= 0),
    idle_events_written     integer NOT NULL DEFAULT 0
                              CHECK (idle_events_written >= 0),
    trips_written           integer NOT NULL DEFAULT 0
                              CHECK (trips_written >= 0),
    rollup_days_recomputed  integer NOT NULL DEFAULT 0
                              CHECK (rollup_days_recomputed >= 0),

    -- What could not be put right, and why. Never zero by omission.
    events_unreconcilable   integer NOT NULL DEFAULT 0
                              CHECK (events_unreconcilable >= 0),
    -- Reason code -> count, e.g. {"raw_samples_expired": 412}. A JSON map rather
    -- than one row per event: at 100K events/s the per-event detail belongs in the
    -- DLQ, and what an operator needs here is the shape of the problem.
    unreconcilable_reasons  jsonb   NOT NULL DEFAULT '{}'::jsonb,

    -- Errors are recorded, not raised, for the same reason the erasure job records
    -- them: a run that half-succeeded must leave a trace of which half.
    errors                  jsonb   NOT NULL DEFAULT '[]'::jsonb,

    -- Deliberately weaker than utilisation_window's constraint, and worth saying why.
    -- Many events map to one vehicle-day, so consumed = reconciled + unreconcilable
    -- does not hold and a sum check would be wrong rather than strict. What can be
    -- enforced is that a run never reports more failures than it had inputs, which
    -- catches a double-count or a miswired counter.
    CONSTRAINT reconciliation_log_failures_within_inputs CHECK (
        finished_at IS NULL
        OR events_unreconcilable <= events_consumed
    )
);

CREATE INDEX reconciliation_log_recent_idx
    ON reconciliation_log (started_at DESC);

COMMENT ON TABLE reconciliation_log IS
    'One row per late-event reconciliation run. Exists so "never silently drop" is '
    'evidenced rather than claimed: the unreconcilable count and its reasons are '
    'recorded on every run, including when they are zero.';

COMMENT ON COLUMN reconciliation_log.events_unreconcilable IS
    'Late events whose vehicle-day could not be recomputed, almost always because the '
    'raw samples had passed the MongoDB TTL. Counted and reported, never discarded '
    'quietly. A non-zero value here is a prompt to reconsider the TTL, not a failure.';

-- Cross-tenant by nature: one run reconciles whatever arrived on the topic, which
-- may span tenants. It carries no tenant_id and therefore gets no RLS policy -
-- like the schema_migrations table, it is operational metadata rather than
-- business data. Readable by the app for an operations view; written only by batch.
GRANT SELECT ON reconciliation_log TO fleet_app;
GRANT SELECT, INSERT, UPDATE ON reconciliation_log TO fleet_batch;
GRANT USAGE, SELECT ON SEQUENCE reconciliation_log_run_id_seq TO fleet_batch;
