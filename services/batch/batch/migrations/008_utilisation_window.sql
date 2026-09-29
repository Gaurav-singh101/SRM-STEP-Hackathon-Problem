-- ===========================================================================
-- 008_utilisation_window
--
-- `UtilisationPage.unscored_no_telemetry` existed in the API schema and the web
-- types but nothing ever set it, so it serialised as 0 on every response. A field
-- that reports "0 vehicles could not be scored" when the batch job just logged
-- 1,808 of them is worse than no field at all: a finance user reads it as a fact.
--
-- The API cannot derive the number honestly on its own. "Not scored" has two
-- distinct causes and only the batch job knows which applied:
--
--   no telemetry      - nothing reached us for that vehicle, so we do not know
--                       whether it was idle or whether its unit had failed
--   class too small   - the vehicle's class had too few peers for a usable
--                       baseline, so it was skipped rather than scored weakly
--
-- Counting "active vehicles without a score row" would collapse those two into
-- one number and answer neither question. So the batch writes what it actually
-- observed, once per window, and the API reads it back.
--
-- The CHECK is the point of the table: the three counts must add up to the active
-- fleet. A miscount then fails the write instead of quietly showing a plausible
-- wrong total on the dashboard.
-- ===========================================================================

CREATE TABLE utilisation_window (
    tenant_id                 text    NOT NULL,
    window_end                date    NOT NULL,
    window_days               integer NOT NULL CHECK (window_days > 0),

    vehicles_active           integer NOT NULL CHECK (vehicles_active >= 0),
    scored                    integer NOT NULL CHECK (scored >= 0),
    unscored_no_telemetry     integer NOT NULL CHECK (unscored_no_telemetry >= 0),
    unscored_class_too_small  integer NOT NULL CHECK (unscored_class_too_small >= 0),
    -- Classes skipped, for the log line. Not part of the identity below: it counts
    -- classes, while everything else counts vehicles.
    classes_skipped           integer NOT NULL CHECK (classes_skipped >= 0),

    formula_version           text    NOT NULL,
    computed_at               timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant_id, window_end),

    -- Every active vehicle is accounted for exactly once. If this ever fails, the
    -- accounting is wrong and the dashboard would have shown a wrong total.
    CONSTRAINT utilisation_window_accounts_for_every_vehicle CHECK (
        scored + unscored_no_telemetry + unscored_class_too_small = vehicles_active
    )
);

COMMENT ON TABLE utilisation_window IS
    'Per-window utilisation accounting. Exists so the dashboard can say how many '
    'vehicles could not be scored and why, instead of implying every vehicle was '
    'scored. The CHECK constraint makes a silent miscount impossible.';

COMMENT ON COLUMN utilisation_window.unscored_no_telemetry IS
    'Active vehicles with no usable activity in the window. NOT the same as unused: '
    'a failed telematics unit and a genuinely parked vehicle both send nothing.';

-- Tenant-scoped like every other rollup table, so the API reads it under RLS.
ALTER TABLE utilisation_window ENABLE ROW LEVEL SECURITY;
ALTER TABLE utilisation_window FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON utilisation_window
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- Read-only to the application: it is a rollup, written by the batch job alone.
GRANT SELECT ON utilisation_window TO fleet_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON utilisation_window TO fleet_batch;
