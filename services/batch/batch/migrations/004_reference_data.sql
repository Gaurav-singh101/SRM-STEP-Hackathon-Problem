-- ===========================================================================
-- 004_reference_data: lookup values and business parameters.
--
-- Idempotent (ON CONFLICT DO NOTHING) so re-running migrations is safe.
--
-- IMPORTANT: every burn rate below is a PROPOSED synthetic value (D2, ADR-0002).
-- They are plausible engineering placeholders for a synthetic fleet. They are NOT
-- measured and NOT sourced, and the `source` column says so on every row so the
-- provenance travels with the data rather than living only in a document.
-- ===========================================================================

INSERT INTO powertrains (powertrain_code, description, burns_fuel) VALUES
    ('diesel', 'Diesel internal combustion',      true),
    ('petrol', 'Petrol internal combustion',      true),
    ('hybrid', 'Petrol-electric hybrid',          true),
    -- Burns no fuel at rest, so idle fuel cost does not apply. Tracked for
    -- utilisation only, and never silently summed as zero into a fleet total (D9).
    ('bev',    'Battery electric',                false)
ON CONFLICT (powertrain_code) DO NOTHING;

INSERT INTO vehicle_classes (class_code, description) VALUES
    ('car',           'Passenger car'),
    ('light_van',     'Light commercial van, under 3.5t'),
    ('medium_truck',  'Medium goods vehicle, 3.5t to 12t'),
    ('heavy_truck',   'Heavy goods vehicle, over 12t')
ON CONFLICT (class_code) DO NOTHING;

INSERT INTO fuel_types (fuel_type, description) VALUES
    ('diesel', 'Automotive diesel'),
    ('petrol', 'Unleaded petrol')
ON CONFLICT (fuel_type) DO NOTHING;

-- --------------------------------------------------------------------------
-- Idle burn rates, formula version idle-fuel-v1 (ADR-0002).
--
-- Hybrids are lower because the engine cycles off at rest. BEVs are absent by
-- design: a row of 0.0 would be indistinguishable from a measured zero, and the
-- cost engine must refuse them loudly rather than return a number.
--
-- Deliberately left unconfigured: hybrid medium/heavy trucks. The cost engine
-- raises on an unconfigured pair, which is the intended behaviour - failing
-- loudly beats inventing a default that quietly produces money.
-- --------------------------------------------------------------------------

INSERT INTO idle_burn_rates
    (powertrain_code, class_code, formula_version, burn_rate_lph, source) VALUES
    ('petrol', 'car',          'idle-fuel-v1', 0.9, 'PROPOSED-synthetic'),
    ('petrol', 'light_van',    'idle-fuel-v1', 1.1, 'PROPOSED-synthetic'),
    ('diesel', 'car',          'idle-fuel-v1', 0.8, 'PROPOSED-synthetic'),
    ('diesel', 'light_van',    'idle-fuel-v1', 1.2, 'PROPOSED-synthetic'),
    ('diesel', 'medium_truck', 'idle-fuel-v1', 2.4, 'PROPOSED-synthetic'),
    ('diesel', 'heavy_truck',  'idle-fuel-v1', 3.8, 'PROPOSED-synthetic'),
    ('hybrid', 'car',          'idle-fuel-v1', 0.4, 'PROPOSED-synthetic'),
    ('hybrid', 'light_van',    'idle-fuel-v1', 0.6, 'PROPOSED-synthetic')
ON CONFLICT (powertrain_code, class_code, formula_version) DO NOTHING;

-- --------------------------------------------------------------------------
-- Guard: the burn-rate table in the database and IDLE_BURN_RATES in
-- fleet_common.domain.fuel must agree. Two sources of truth for money would
-- diverge quietly, so a contract test asserts they match. This view gives the
-- test something stable to read instead of parsing SQL.
-- --------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_idle_burn_rates_current AS
SELECT
    r.powertrain_code,
    r.class_code,
    r.burn_rate_lph,
    r.formula_version,
    r.source,
    p.burns_fuel
FROM idle_burn_rates r
JOIN powertrains p USING (powertrain_code)
WHERE r.formula_version = 'idle-fuel-v1';

COMMENT ON VIEW v_idle_burn_rates_current IS
    'Burn rates for the current formula version. Asserted equal to '
    'fleet_common.domain.fuel.IDLE_BURN_RATES by a contract test, so the '
    'database and the domain cannot disagree about money.';
