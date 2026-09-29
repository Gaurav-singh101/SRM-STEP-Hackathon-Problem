-- ===========================================================================
-- 005_utilisation_component_spread
--
-- The ABNORMAL band now measures a vehicle against itself rather than against its
-- peers: the gap between its highest and lowest utilisation component. See the
-- 2026-09-29 amendment to ADR-0003 for why peer comparison was the wrong test here.
--
-- max_abs_z is retained. It is still useful context - how far from the class median -
-- it simply no longer decides the band.
-- ===========================================================================

ALTER TABLE vehicle_utilisation
    ADD COLUMN IF NOT EXISTS component_spread double precision NOT NULL DEFAULT 0;

COMMENT ON COLUMN vehicle_utilisation.component_spread IS
    'Gap between the highest and lowest normalised component. Drives the ABNORMAL '
    'band: a vehicle whose own signals disagree (active every day but barely moving) '
    'is a data-quality or misuse signal, not a level of utilisation.';

-- Serves the attention view, which lists inconsistent vehicles worst-first.
CREATE INDEX IF NOT EXISTS vehicle_utilisation_spread_idx
    ON vehicle_utilisation (tenant_id, window_end DESC, component_spread DESC)
    WHERE band = 'abnormal';
