"""Tenant isolation, proven against a real database.

CLAUDE.md S10 requires isolation "enforced in the data-access layer, not only in
routes", and S12 requires explicit tenant-leakage attempts as test cases. These
tests attack the database directly, with no application code in the way: if they
pass, a missing WHERE clause in a repository cannot leak across tenants, because
the database itself refuses.

Marked `integration`: they need a container runtime. Run with `-m integration`.
"""

from __future__ import annotations

import psycopg
import pytest

from .conftest import TENANT_A, TENANT_B, TENANT_VINS

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[tuple[object, ...]]


def set_tenant(conn: Conn, tenant: str | None) -> None:
    """Set the session tenant context, as the API does from a validated claim.

    Uses ``set_config`` rather than ``SET``: ``SET`` takes no bind parameters, so
    applying a tenant with it would mean interpolating a value into SQL - which is
    exactly the injection that CLAUDE.md S10 forbids.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant or "",))


def count_vehicles(conn: Conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM vehicles")
        row = cur.fetchone()
        assert row is not None
        return int(row[0])  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def test_each_tenant_sees_only_its_own_vehicles(app_conn: Conn, seeded: None) -> None:
    set_tenant(app_conn, TENANT_A)
    assert count_vehicles(app_conn) == 1

    set_tenant(app_conn, TENANT_B)
    assert count_vehicles(app_conn) == 1


def test_a_tenant_cannot_read_another_tenants_vehicle_by_primary_key(
    app_conn: Conn, seeded: None
) -> None:
    """The sharpest test: knowing the exact VIN must not be enough.

    An attacker who learns another tenant's identifiers gains nothing, because the
    policy filters before the primary-key lookup resolves.
    """
    set_tenant(app_conn, TENANT_A)
    with app_conn.cursor() as cur:
        cur.execute("SELECT vin FROM vehicles WHERE vin = %s", (TENANT_VINS[TENANT_B],))
        assert cur.fetchone() is None, "tenant A read tenant B's vehicle by VIN"


def test_unset_tenant_context_sees_nothing(app_conn: Conn, seeded: None) -> None:
    """Fail closed. A forgotten context must yield no rows, never all rows -
    the difference between a broken page and a data breach."""
    set_tenant(app_conn, None)
    assert count_vehicles(app_conn) == 0


def test_empty_tenant_context_sees_nothing(app_conn: Conn, seeded: None) -> None:
    """An empty string is normalised to NULL, which matches no rows."""
    set_tenant(app_conn, "")
    assert count_vehicles(app_conn) == 0


def test_unknown_tenant_sees_nothing(app_conn: Conn, seeded: None) -> None:
    set_tenant(app_conn, "tenant-does-not-exist")
    assert count_vehicles(app_conn) == 0


def test_isolation_holds_across_every_tenant_scoped_table(app_conn: Conn, seeded: None) -> None:
    """A policy missing from one table is the gap that matters, so check them all."""
    tables = [
        "fleets",
        "vehicles",
        "drivers",
        "vehicle_assignments",
        "trips",
        "idle_events",
        "alerts",
        "audit_log",
        "erasure_log",
        "vehicle_daily",
        "fleet_daily",
        "vehicle_utilisation",
        "class_baselines",
        "subscriptions",
        "app_users",
    ]
    set_tenant(app_conn, "tenant-does-not-exist")
    with app_conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
            assert row is not None
            assert int(row[0]) == 0, f"{table} leaked rows to an unknown tenant"  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def test_cannot_insert_a_row_for_another_tenant(app_conn: Conn, seeded: None) -> None:
    """WITH CHECK stops forging the tenant_id column on write.

    Without it, a caller could write rows into another tenant's data even while
    being unable to read them.
    """
    set_tenant(app_conn, TENANT_A)
    with app_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute(
            "INSERT INTO fleets (tenant_id, name) VALUES (%s, 'Smuggled depot')",
            (TENANT_B,),
        )


def test_cannot_update_a_row_into_another_tenant(app_conn: Conn, seeded: None) -> None:
    """Re-tenanting an existing row must fail just as an insert would."""
    set_tenant(app_conn, TENANT_A)
    with app_conn.cursor() as cur, pytest.raises(psycopg.errors.InsufficientPrivilege):
        cur.execute("UPDATE fleets SET tenant_id = %s", (TENANT_B,))


def test_update_cannot_reach_another_tenants_rows(app_conn: Conn, seeded: None) -> None:
    """An unqualified UPDATE affects only the caller's own rows.

    This is the missing-WHERE-clause bug that RLS is defence against: the query is
    wrong, but the damage is contained to one tenant.
    """
    set_tenant(app_conn, TENANT_A)
    with app_conn.cursor() as cur:
        cur.execute("UPDATE fleets SET name = 'Renamed'")
        assert cur.rowcount == 1

    set_tenant(app_conn, TENANT_B)
    with app_conn.cursor() as cur:
        cur.execute("SELECT name FROM fleets")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "Depot 1", "tenant B's fleet was renamed by tenant A"


def test_delete_cannot_reach_another_tenants_rows(app_conn: Conn, seeded: None) -> None:
    set_tenant(app_conn, TENANT_A)
    with app_conn.cursor() as cur:
        cur.execute("DELETE FROM vehicles")
        assert cur.rowcount == 1

    set_tenant(app_conn, TENANT_B)
    assert count_vehicles(app_conn) == 1, "tenant B's vehicle was deleted by tenant A"


# --------------------------------------------------------------------------
# The mechanism itself
# --------------------------------------------------------------------------


def test_row_level_security_is_forced_not_merely_enabled(admin_conn: Conn) -> None:
    """ENABLE alone exempts the table owner, and the application may well connect
    as the owner. FORCE is what closes that gap, so assert it explicitly."""
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT relname FROM pg_class"
            " WHERE relrowsecurity AND NOT relforcerowsecurity"
            "   AND relnamespace = 'public'::regnamespace"
        )
        assert cur.fetchall() == [], "some tables have RLS enabled but not forced"


def test_every_tenant_scoped_table_has_a_policy(admin_conn: Conn) -> None:
    """Guards against a future table being added without a policy."""
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
            WHERE c.relkind IN ('r', 'p')
              AND c.relnamespace = 'public'::regnamespace
              -- Partitions inherit the parent's policies and correctly have none
              -- of their own, so only parents and plain tables are checked.
              AND NOT c.relispartition
              AND NOT EXISTS (
                  SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid
              )
            """
        )
        missing = [r[0] for r in cur.fetchall()]
        assert missing == [], f"tables with tenant_id but no RLS policy: {missing}"


def test_application_role_cannot_modify_the_audit_log(admin_conn: Conn) -> None:
    """PDF S11 wants an audit trail for every access. A trail the application can
    rewrite is not evidence of anything, so UPDATE and DELETE are revoked."""
    with admin_conn.cursor() as cur:
        for privilege in ("UPDATE", "DELETE"):
            cur.execute("SELECT has_table_privilege('fleet_app', 'audit_log', %s)", (privilege,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] is False, f"fleet_app can {privilege} the audit log"


def test_application_role_can_append_to_the_audit_log(admin_conn: Conn) -> None:
    """Append-only means append must still work."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege('fleet_app', 'audit_log', 'INSERT')")
        row = cur.fetchone()
        assert row is not None
        assert row[0] is True


def test_application_role_cannot_rewrite_business_parameters(admin_conn: Conn) -> None:
    """Changing a fuel price or burn rate silently restates historical money, so it
    is a migration or an admin action - never something a request handler can do."""
    with admin_conn.cursor() as cur:
        for table in ("fuel_prices", "idle_burn_rates", "powertrains"):
            for privilege in ("INSERT", "UPDATE", "DELETE"):
                cur.execute("SELECT has_table_privilege('fleet_app', %s, %s)", (table, privilege))
                row = cur.fetchone()
                assert row is not None
                assert row[0] is False, f"fleet_app can {privilege} {table}"


def test_application_role_has_no_login_and_no_rls_bypass(admin_conn: Conn) -> None:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT rolcanlogin, rolbypassrls FROM pg_roles WHERE rolname = 'fleet_app'")
        row = cur.fetchone()
        assert row is not None
        can_login, bypass_rls = row
        assert can_login is False, "fleet_app should be reachable only via SET ROLE"
        assert bypass_rls is False, "fleet_app must never bypass RLS"


def test_a_superuser_connection_bypasses_rls(admin_conn: Conn, seeded: None) -> None:
    """Documents the one way this defence can be defeated, so it cannot surprise us.

    PostgreSQL exempts superusers and BYPASSRLS roles from row-level security
    unconditionally; FORCE ROW LEVEL SECURITY closes the table-owner gap, not this
    one. So the policies are only load-bearing if the application connects as an
    ordinary role and switches to `fleet_app`.

    This test asserts the bypass exists rather than pretending it does not. If a
    future change made a superuser connection respect RLS, this test would fail and
    the deployment note could be relaxed deliberately instead of by accident.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', 'tenant-does-not-exist', false)")
    assert count_vehicles(admin_conn) == 2, (
        "expected a superuser to see all tenants; if this now returns 0, RLS "
        "behaviour has changed and the deployment constraint can be revisited"
    )


def test_set_role_to_the_app_role_is_enough_to_enforce_isolation(
    admin_conn: Conn, seeded: None
) -> None:
    """The mitigation, proven on the same connection that just bypassed RLS.

    Same session, one SET ROLE apart: this is precisely the step the API performs
    on pool checkout, so it shows the mitigation is sufficient and not incidental.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
    assert count_vehicles(admin_conn) == 2, "superuser still sees everything"

    with admin_conn.cursor() as cur:
        cur.execute("SET ROLE fleet_app")
    assert count_vehicles(admin_conn) == 1, "SET ROLE did not bring RLS into force"
