"""The utilisation window table, against a real PostgreSQL.

Two things here cannot be tested any other way. The CHECK constraint that makes a
miscount impossible is enforced by the database, so a fake would only re-test the
assertion in the Python dataclass. And the tenant policy on the table decides whether one
tenant can read another's fleet size, which is a property of the policy, not of the query.

Marked `integration`: these need a container runtime. Run with `-m integration`.
"""

from __future__ import annotations

from datetime import date

import psycopg
import pytest

from tests.integration.conftest import TENANT_A, TENANT_B

# Without this the module is collected by the unit suite, where Testcontainers blocks on a
# Docker daemon that may not be running - which is exactly what happened: `-m "not
# integration"` appeared to hang for minutes with no output. The marker is the deselection
# mechanism, so a file that omits it silently opts into the wrong suite.
pytestmark = pytest.mark.integration

WINDOW_END = date(2026, 9, 29)

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture(autouse=True)
def _clear_windows(admin_conn: Conn) -> None:
    """Empty the table before each test.

    The container is session-scoped and ``admin_conn`` is autocommit, so without this a
    row written by one test would satisfy or break the next one depending on ordering -
    and a suite whose result depends on order proves nothing.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute("DELETE FROM utilisation_window")
        cur.execute("RESET ROLE")


def _insert_window(
    conn: Conn,
    *,
    tenant_id: str,
    vehicles_active: int,
    scored: int,
    no_telemetry: int,
    class_too_small: int,
    classes_skipped: int = 0,
    window_end: date = WINDOW_END,
    role: str = "fleet_batch",
) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET ROLE {role}")
        cur.execute(
            "INSERT INTO utilisation_window (tenant_id, window_end, window_days,"
            " vehicles_active, scored, unscored_no_telemetry, unscored_class_too_small,"
            " classes_skipped, formula_version)"
            " VALUES (%s, %s, 30, %s, %s, %s, %s, %s, 'utilisation-v1')",
            (
                tenant_id,
                window_end,
                vehicles_active,
                scored,
                no_telemetry,
                class_too_small,
                classes_skipped,
            ),
        )


def test_a_window_that_accounts_for_every_vehicle_is_accepted(
    admin_conn: Conn, seeded: None
) -> None:
    _insert_window(
        admin_conn,
        tenant_id=TENANT_A,
        vehicles_active=100,
        scored=80,
        no_telemetry=15,
        class_too_small=5,
    )
    with admin_conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute(
            "SELECT scored, unscored_no_telemetry, unscored_class_too_small"
            " FROM utilisation_window WHERE tenant_id = %s AND window_end = %s",
            (TENANT_A, WINDOW_END),
        )
        assert cur.fetchone() == (80, 15, 5)


def test_the_database_refuses_a_window_whose_counts_do_not_add_up(
    admin_conn: Conn, seeded: None
) -> None:
    """The last line of defence.

    The dataclass raises too, but a wrong total reaching the dashboard is the failure that
    matters, so the constraint sits where every writer has to pass it - including a future
    backfill script or a manual correction, neither of which goes through the dataclass.
    """
    with pytest.raises(psycopg.errors.CheckViolation, match="accounts_for_every_vehicle"):
        _insert_window(
            admin_conn,
            tenant_id=TENANT_A,
            vehicles_active=100,
            scored=80,
            no_telemetry=0,
            class_too_small=0,
        )
    admin_conn.rollback()


def test_the_database_refuses_negative_counts(admin_conn: Conn, seeded: None) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_window(
            admin_conn,
            tenant_id=TENANT_A,
            vehicles_active=10,
            scored=12,
            no_telemetry=-2,
            class_too_small=0,
        )
    admin_conn.rollback()


def test_a_re_run_corrects_the_window_rather_than_duplicating_it(
    admin_conn: Conn, seeded: None
) -> None:
    """Re-running the batch job must be safe, which is what makes reconciliation cheap."""
    _insert_window(
        admin_conn,
        tenant_id=TENANT_A,
        vehicles_active=100,
        scored=80,
        no_telemetry=20,
        class_too_small=0,
    )
    with admin_conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute(
            "INSERT INTO utilisation_window (tenant_id, window_end, window_days,"
            " vehicles_active, scored, unscored_no_telemetry, unscored_class_too_small,"
            " classes_skipped, formula_version)"
            " VALUES (%s, %s, 30, 100, 95, 5, 0, 0, 'utilisation-v1')"
            " ON CONFLICT (tenant_id, window_end) DO UPDATE SET"
            "   scored = EXCLUDED.scored,"
            "   unscored_no_telemetry = EXCLUDED.unscored_no_telemetry",
            (TENANT_A, WINDOW_END),
        )
        cur.execute(
            "SELECT count(*), max(scored) FROM utilisation_window"
            " WHERE tenant_id = %s AND window_end = %s",
            (TENANT_A, WINDOW_END),
        )
        assert cur.fetchone() == (1, 95)


def test_one_tenant_cannot_read_another_tenants_fleet_size(
    admin_conn: Conn, app_conn: Conn, seeded: None
) -> None:
    """Fleet size is commercially sensitive, so the window row is tenant-scoped too.

    Easy to miss on a rollup table: it holds no driver or location data, so it can look
    like harmless aggregate. It is not - it tells a competitor how many vehicles the other
    tenant runs.
    """
    _insert_window(
        admin_conn,
        tenant_id=TENANT_B,
        vehicles_active=900,
        scored=900,
        no_telemetry=0,
        class_too_small=0,
    )

    with app_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute("SELECT count(*) FROM utilisation_window WHERE tenant_id = %s", (TENANT_B,))
        assert cur.fetchone() == (0,)


def test_the_application_role_cannot_write_a_window(app_conn: Conn, seeded: None) -> None:
    """It is a rollup: the batch job owns it.

    If the API could write here it could also make its own coverage figure look complete,
    which is the one thing this table exists to prevent.
    """
    with app_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO utilisation_window (tenant_id, window_end, window_days,"
                " vehicles_active, scored, unscored_no_telemetry,"
                " unscored_class_too_small, classes_skipped, formula_version)"
                " VALUES (%s, %s, 30, 1, 1, 0, 0, 0, 'utilisation-v1')",
                (TENANT_A, WINDOW_END),
            )
    app_conn.rollback()


def test_the_newest_window_wins_when_several_exist(admin_conn: Conn, seeded: None) -> None:
    """Older windows are kept for history, but mixing them would blend different data.

    The API reads ``ORDER BY window_end DESC LIMIT 1``; this pins that the older row does
    not win by insertion order.
    """
    _insert_window(
        admin_conn,
        tenant_id=TENANT_A,
        vehicles_active=50,
        scored=50,
        no_telemetry=0,
        class_too_small=0,
        window_end=date(2026, 8, 31),
    )
    _insert_window(
        admin_conn,
        tenant_id=TENANT_A,
        vehicles_active=100,
        scored=70,
        no_telemetry=30,
        class_too_small=0,
        window_end=WINDOW_END,
    )
    with admin_conn.cursor() as cur:
        cur.execute("SET ROLE fleet_batch")
        cur.execute(
            "SELECT window_end, vehicles_active FROM utilisation_window"
            " WHERE tenant_id = %s ORDER BY window_end DESC LIMIT 1",
            (TENANT_A,),
        )
        assert cur.fetchone() == (WINDOW_END, 100)
