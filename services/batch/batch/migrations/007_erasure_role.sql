-- ===========================================================================
-- 007_erasure_role
--
-- Two requirements pull against each other here, and both are real:
--
--   PDF S11 wants an audit trail for every data access. A trail the application can
--   rewrite is not evidence of anything, so migration 003 revoked UPDATE and DELETE on
--   audit_log from every application role.
--
--   PDF S11 also wants a right-to-erasure flow. The audit trail names the subject
--   ("who read driver 42"), so erasing a subject means reaching into it.
--
-- Resolving it by granting the batch role UPDATE on audit_log would undo the first
-- requirement to satisfy the second: any bug or compromise in the batch job could then
-- rewrite history.
--
-- Instead, a dedicated NOLOGIN role gets a COLUMN-LEVEL grant on exactly one column.
-- `fleet_erasure` can blank the subject reference and nothing else - it cannot change
-- who did what, when, from where, or under which correlation id. The append-only
-- guarantee survives for every column that carries the evidence.
--
-- Considered and rejected: treating the audit log as exempt under GDPR Art 17(3)(b)
-- (processing necessary for a legal obligation). That is defensible and is what many
-- systems do, but it leaves an identifier in storage indefinitely, and a narrowly
-- scoped grant costs little and removes the argument entirely.
-- ===========================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_erasure') THEN
        -- NOLOGIN, like the other roles: reached only via SET ROLE from an already
        -- authenticated connection, so no new credential exists to manage.
        CREATE ROLE fleet_erasure NOLOGIN BYPASSRLS;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO fleet_erasure;

-- The subject's own records: full access, because that is what is being erased.
GRANT SELECT, UPDATE ON drivers TO fleet_erasure;
GRANT SELECT, UPDATE ON vehicle_assignments TO fleet_erasure;

-- The audit trail: SELECT, plus UPDATE on the subject reference ALONE. Every other
-- column - actor, action, timestamp, correlation id, masked address - stays immutable.
GRANT SELECT ON audit_log TO fleet_erasure;
GRANT UPDATE (resource_id) ON audit_log TO fleet_erasure;

-- The erasure record itself is append-and-complete: inserted when the request opens,
-- updated once to record the outcome. No delete, ever - evidence of compliance that
-- can be deleted is not evidence.
GRANT SELECT, INSERT, UPDATE ON erasure_log TO fleet_erasure;
GRANT USAGE, SELECT ON SEQUENCE erasure_log_erasure_id_seq TO fleet_erasure;

-- Read-only on the operational record. The erasure job counts what it retains so the
-- audit entry can state it, but it must never be able to delete a trip or an idle
-- event: that would erase the fleet's own business record, silently change historical
-- fuel totals, and make a past finance report irreproducible.
GRANT SELECT ON trips TO fleet_erasure;
GRANT SELECT ON idle_events TO fleet_erasure;

COMMENT ON TABLE erasure_log IS
    'Audit of erasure requests. Written by fleet_erasure, which can insert and complete '
    'a record but never delete one. The record that data was removed must survive the '
    'removal, or the obligation cannot be demonstrated afterwards.';
