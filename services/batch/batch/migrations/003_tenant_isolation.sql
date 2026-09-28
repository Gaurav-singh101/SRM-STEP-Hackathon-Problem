-- ===========================================================================
-- 003_tenant_isolation: row-level security as defence in depth.
--
-- Tenant isolation is enforced primarily in the data-access layer (CLAUDE.md S10:
-- "enforced in the data-access layer, not only in routes"). This migration adds a
-- second, independent enforcement point inside the database, so a missing WHERE
-- clause in application code is a failed query rather than a cross-tenant leak.
--
-- Design note on roles: the tables are owned by the migration user, and Postgres
-- exempts a table owner from its own RLS policies. Two mechanisms close that:
--   1. A dedicated NOLOGIN role, `fleet_app`, that the application switches to
--      with SET ROLE after connecting. No second password is created, so there is
--      no new secret to manage (CLAUDE.md S3).
--   2. FORCE ROW LEVEL SECURITY, so even the owner is subject to the policies.
-- ===========================================================================

-- --------------------------------------------------------------------------
-- Application role: least privilege, no login of its own.
-- --------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_app') THEN
        -- NOLOGIN on purpose: reached only via SET ROLE from an already
        -- authenticated connection, so it adds no credential to manage.
        CREATE ROLE fleet_app NOLOGIN;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO fleet_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fleet_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fleet_app;

-- The audit log is append-only. PDF S11 requires an audit trail for every data
-- access; a trail the application can rewrite is not evidence of anything.
REVOKE UPDATE, DELETE ON audit_log FROM fleet_app;
REVOKE UPDATE, DELETE ON erasure_log FROM fleet_app;

-- Reference and parameter tables are read-only to the application. Changing a
-- fuel price or a burn rate is a migration or an admin action, never something a
-- request handler can do, because it would silently restate historical money.
REVOKE INSERT, UPDATE, DELETE ON powertrains, vehicle_classes, fuel_types,
    idle_burn_rates, fuel_prices FROM fleet_app;

-- --------------------------------------------------------------------------
-- Tenant context. The application sets this once per connection, from the
-- validated JWT claim - never from a request body or query string
-- (CLAUDE.md S3).
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS text
LANGUAGE sql
STABLE
AS $$
    -- Missing setting yields NULL rather than an error, and every policy below
    -- compares with '=', so an unset context matches no rows. Failing closed is
    -- the only safe default here.
    SELECT nullif(current_setting('app.tenant_id', true), '');
$$;

COMMENT ON FUNCTION current_tenant_id IS
    'Tenant from the session context, set from the validated token claim. '
    'Returns NULL when unset, which matches no rows - fail closed.';

-- --------------------------------------------------------------------------
-- Policies. One per tenant-scoped table.
-- --------------------------------------------------------------------------

DO $$
DECLARE
    target text;
    -- Every table carrying tenant_id. Partitioned parents are listed; policies
    -- and FORCE inherit to their partitions automatically.
    tenant_tables text[] := ARRAY[
        'subscriptions', 'app_users', 'fleets', 'vehicles', 'drivers',
        'vehicle_assignments', 'trips', 'idle_events', 'alerts', 'audit_log',
        'erasure_log', 'vehicle_daily', 'fleet_daily', 'vehicle_utilisation',
        'class_baselines'
    ];
BEGIN
    FOREACH target IN ARRAY tenant_tables LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', target);
        -- Applies the policy to the table owner too, closing the owner bypass.
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);

        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                 USING (tenant_id = current_tenant_id())
                 WITH CHECK (tenant_id = current_tenant_id())',
            target
        );
    END LOOP;
END;
$$;

-- fuel_prices is tenant-scoped but read-only to the application, so it gets a
-- SELECT-only policy rather than the read/write pair above.
ALTER TABLE fuel_prices ENABLE ROW LEVEL SECURITY;
ALTER TABLE fuel_prices FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_read ON fuel_prices
    FOR SELECT USING (tenant_id = current_tenant_id());

-- The tenants table is keyed by tenant_id itself rather than carrying it.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants
    USING (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- --------------------------------------------------------------------------
-- Maintenance role: the batch job needs to cross tenants to rebuild rollups and
-- manage partitions. It is separate and explicit, so "this code can see every
-- tenant" is a visible property of the deployment rather than an accident.
-- --------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_batch') THEN
        CREATE ROLE fleet_batch NOLOGIN BYPASSRLS;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO fleet_batch;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fleet_batch;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fleet_batch;
REVOKE UPDATE, DELETE ON audit_log FROM fleet_batch;

COMMENT ON SCHEMA public IS
    'Tenant isolation: application connections SET ROLE fleet_app and SET '
    'app.tenant_id from the validated token. Cross-tenant maintenance uses '
    'fleet_batch (BYPASSRLS) and is limited to rollups and partition management.';
