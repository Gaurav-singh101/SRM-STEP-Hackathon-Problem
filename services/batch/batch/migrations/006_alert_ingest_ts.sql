-- ===========================================================================
-- 006_alert_ingest_ts
--
-- The D10 SLO is "critical alert under 5 seconds". Measured as
-- persisted_at - triggering_event_ts, that is exactly right for live streaming and
-- exactly wrong for replayed history: backfilling ten days of telemetry produced
-- apparent latencies of 83,711 seconds, which is the age of the data, not the speed of
-- the system.
--
-- Recording when the processor received the event separates the two:
--   end-to-end latency = persisted_at - triggering_event_ts  (the SLO, live only)
--   processing latency = persisted_at - ingest_ts            (always meaningful)
--
-- Both are reported. Quoting only the first would be misleading on a backfill, and
-- quoting only the second would quietly weaken the SLO.
-- ===========================================================================

ALTER TABLE alerts
    ADD COLUMN IF NOT EXISTS ingest_ts timestamptz;

COMMENT ON COLUMN alerts.ingest_ts IS
    'When the processor received the triggering event. Assigned by the processor, '
    'never read from the device: a device clock is bug- and attacker-controlled, so a '
    'latency measured against it would be meaningless.';
