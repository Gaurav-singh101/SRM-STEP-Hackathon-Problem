"""Right-to-erasure compliance tests (PDF S11, S12).

PDF S12 requires compliance testing that verifies the audit trail and erasure. These run
against a real database, because the whole question is what actually survives in
storage - a mocked erasure proves nothing.

The pair of obligations being tested pull in opposite directions, and both must hold:

1. The subject's identifying data is gone.
2. The fleet's operational record survives, with the link to the subject severed.

Deleting the trips and idle events a driver was assigned to would satisfy (1) while
destroying the fleet's own record of its fuel cost, silently changing historical totals
and making last month's report irreproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from batch.erasure import ERASED_NAME, erase_driver, verify_erasure
from fleet_common.vin import repair_check_digit

from .conftest import TENANT_A

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[tuple[object, ...]]

ERASURE_VIN = repair_check_digit("1HGCM82633A004352")


@pytest.fixture
def driver_with_history(admin_conn: Conn, seeded: None) -> int:
    """A driver with an assignment, trips, idle events and audit entries.

    Every kind of record the erasure has to reason about, so the test exercises the
    retention decision rather than only the deletion.
    """
    now = datetime.now(UTC)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))

        cur.execute(
            "INSERT INTO drivers (tenant_id, full_name, licence_ref)"
            " VALUES (%s, 'SYNTHETIC DRIVER 00001', 'LIC-TEST-00001')"
            " RETURNING driver_id",
            (TENANT_A,),
        )
        row = cur.fetchone()
        assert row is not None
        driver_id = int(row[0])  # type: ignore[arg-type]

        cur.execute(
            "INSERT INTO vehicle_assignments (tenant_id, vin, driver_id, assigned_from)"
            " VALUES (%s, %s, %s, %s)",
            (TENANT_A, ERASURE_VIN, driver_id, now - timedelta(days=30)),
        )

        cur.execute(
            "INSERT INTO trips (tenant_id, vin, started_at, ended_at, distance_km,"
            " moving_seconds, idle_seconds) VALUES (%s, %s, %s, %s, 42.5, 3600, 300)",
            (TENANT_A, ERASURE_VIN, now - timedelta(days=2), now - timedelta(days=2)),
        )

        cur.execute(
            "INSERT INTO idle_events (tenant_id, vin, started_at, ended_at,"
            " duration_seconds, sample_count, formula_version, idle_threshold_seconds,"
            " cost_est, currency)"
            " VALUES (%s, %s, %s, %s, 600, 60, 'idle-fuel-v1', 180, 123.4500, 'INR')",
            (TENANT_A, ERASURE_VIN, now - timedelta(days=2), now - timedelta(days=2)),
        )

        cur.execute(
            "INSERT INTO audit_log (tenant_id, actor_sub, actor_role, action,"
            " resource_type, resource_id, correlation_id)"
            " VALUES (%s, 'tester', 'admin', 'read_driver', 'driver', %s, 'corr-1')",
            (TENANT_A, str(driver_id)),
        )
    return driver_id


def run_erasure(migrated_dsn: str, driver_id: int) -> object:
    return erase_driver(
        dsn=migrated_dsn,
        tenant_id=TENANT_A,
        driver_id=driver_id,
        requested_by="dpo@example.test",
    )


# --------------------------------------------------------------------------
# Obligation 1: the subject's data is gone
# --------------------------------------------------------------------------


def test_the_drivers_name_is_erased(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    run_erasure(migrated_dsn, driver_with_history)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT full_name, licence_ref, erased_at FROM drivers WHERE driver_id = %s",
            (driver_with_history,),
        )
        row = cur.fetchone()

    assert row is not None
    name, licence, erased_at = row
    assert name == ERASED_NAME
    assert "ERASED" in str(licence)
    assert erased_at is not None


def test_the_licence_reference_is_erased(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """A licence number identifies a person as surely as a name does."""
    run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT count(*) FROM drivers WHERE driver_id = %s AND licence_ref LIKE 'LIC-%%'",
            (driver_with_history,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row[0]) == 0  # type: ignore[arg-type]


def test_open_assignments_are_closed(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """The link between person and vehicle is itself personal data: "who was driving
    this van" identifies someone."""
    run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT count(*) FROM vehicle_assignments WHERE driver_id = %s AND assigned_to IS NULL",
            (driver_with_history,),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row[0]) == 0  # type: ignore[arg-type]


def test_the_audit_trail_no_longer_names_the_subject(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """The trail is retained as evidence of access, but the identifier inside it is
    erasable - otherwise the audit log becomes a way to defeat erasure."""
    run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT count(*) FROM audit_log WHERE resource_type = 'driver' AND resource_id = %s",
            (str(driver_with_history),),
        )
        row = cur.fetchone()
    assert row is not None
    assert int(row[0]) == 0  # type: ignore[arg-type]


def test_verification_reports_the_subject_as_erased(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """Checked by reading the data back, not by trusting the eraser's own report."""
    run_erasure(migrated_dsn, driver_with_history)
    verdict = verify_erasure(admin_conn, tenant_id=TENANT_A, driver_id=driver_with_history)
    assert verdict["erased"] is True, verdict


# --------------------------------------------------------------------------
# Obligation 2: the fleet's operational record survives
# --------------------------------------------------------------------------


def test_trips_survive_the_erasure(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """Deleting them would erase the fleet's own record of its operations."""
    run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute("SELECT count(*) FROM trips WHERE vin = %s", (ERASURE_VIN,))
        row = cur.fetchone()
    assert row is not None
    assert int(row[0]) >= 1  # type: ignore[arg-type]


def test_idle_cost_history_survives_the_erasure(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """The decisive retention case.

    Erasing idle events would silently change historical fuel totals and make last
    month's finance report irreproducible - for a subject-access request that asked
    only for a person's identity to be removed.
    """
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT coalesce(sum(cost_est), 0) FROM idle_events WHERE vin = %s",
            (ERASURE_VIN,),
        )
        before_row = cur.fetchone()

    run_erasure(migrated_dsn, driver_with_history)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT coalesce(sum(cost_est), 0) FROM idle_events WHERE vin = %s",
            (ERASURE_VIN,),
        )
        after_row = cur.fetchone()

    assert before_row is not None and after_row is not None
    assert after_row[0] == before_row[0], "erasure changed a historical cost total"


def test_the_result_reports_what_was_retained(migrated_dsn: str, driver_with_history: int) -> None:
    """A deletion that does not say what it left behind invites the question later."""
    result = run_erasure(migrated_dsn, driver_with_history)
    assert result.trips_retained >= 1  # type: ignore[attr-defined]
    assert result.idle_events_retained >= 1  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The erasure is itself audited
# --------------------------------------------------------------------------


def test_the_erasure_is_recorded(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """The record that data was removed must survive the removal, or the obligation
    cannot be demonstrated afterwards."""
    run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT requested_by, completed_at, stores_affected FROM erasure_log"
            " WHERE subject_ref = %s AND subject_type = 'driver'",
            (str(driver_with_history),),
        )
        row = cur.fetchone()

    assert row is not None
    requested_by, completed_at, stores = row
    assert requested_by == "dpo@example.test"
    assert completed_at is not None
    # Per store, so a partial erasure is visible rather than averaged away.
    assert "drivers_anonymised" in dict(stores)  # type: ignore[arg-type]


def test_the_audit_record_is_opened_before_anything_is_erased(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """If the erasure fails halfway there must still be a record that it was attempted,
    or a partial deletion leaves no trace of why data went missing."""
    result = run_erasure(migrated_dsn, driver_with_history)
    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute(
            "SELECT requested_at, completed_at FROM erasure_log WHERE erasure_id = %s",
            (result.erasure_id,),  # type: ignore[attr-defined]
        )
        row = cur.fetchone()
    assert row is not None
    requested_at, completed_at = row
    assert requested_at <= completed_at


def test_the_application_role_cannot_delete_the_erasure_log(admin_conn: Conn) -> None:
    """Evidence of compliance that the application can delete is not evidence."""
    with admin_conn.cursor() as cur:
        for privilege in ("UPDATE", "DELETE"):
            cur.execute(
                "SELECT has_table_privilege('fleet_app', 'erasure_log', %s)",
                (privilege,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is False, f"fleet_app can {privilege} the erasure log"


# --------------------------------------------------------------------------
# Repeat and edge behaviour
# --------------------------------------------------------------------------


def test_erasing_twice_is_safe(
    admin_conn: Conn, migrated_dsn: str, driver_with_history: int
) -> None:
    """A subject may ask twice, or a job may retry. Neither should fail or double-count."""
    first = run_erasure(migrated_dsn, driver_with_history)
    second = run_erasure(migrated_dsn, driver_with_history)

    assert first.drivers_anonymised == 1  # type: ignore[attr-defined]
    # Already erased, so nothing left to anonymise - but still recorded as requested.
    assert second.drivers_anonymised == 0  # type: ignore[attr-defined]
    assert second.complete  # type: ignore[attr-defined]

    verdict = verify_erasure(admin_conn, tenant_id=TENANT_A, driver_id=driver_with_history)
    assert verdict["erased"] is True


def test_erasing_an_unknown_driver_is_recorded_rather_than_failing(
    admin_conn: Conn, migrated_dsn: str, seeded: None
) -> None:
    """A request for someone who does not exist is still a request that was made, and
    the record of having checked is part of the obligation."""
    result = erase_driver(
        dsn=migrated_dsn,
        tenant_id=TENANT_A,
        driver_id=999_999,
        requested_by="dpo@example.test",
    )
    assert result.drivers_anonymised == 0
    assert result.complete

    with admin_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", (TENANT_A,))
        cur.execute("SELECT count(*) FROM erasure_log WHERE subject_ref = '999999'")
        row = cur.fetchone()
    assert row is not None
    assert int(row[0]) == 1  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# The column-level grant: erasure must not become a way to rewrite history
# --------------------------------------------------------------------------


def test_the_erasure_role_can_blank_the_subject_reference(admin_conn: Conn) -> None:
    """The one column it needs."""
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege('fleet_erasure', 'audit_log', 'resource_id', 'UPDATE')"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is True


@pytest.mark.parametrize("column", ["actor_sub", "actor_role", "action", "at", "correlation_id"])
def test_the_erasure_role_cannot_alter_the_evidence_columns(admin_conn: Conn, column: str) -> None:
    """The point of a column-level grant.

    Granting UPDATE on the whole table would let any bug or compromise in the erasure
    path rewrite who did what and when. These columns carry the evidence, so they stay
    immutable even while the subject reference is erasable.
    """
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT has_column_privilege('fleet_erasure', 'audit_log', %s, 'UPDATE')",
            (column,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False, f"fleet_erasure can rewrite audit_log.{column}"


def test_the_erasure_role_cannot_delete_audit_rows(admin_conn: Conn) -> None:
    with admin_conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege('fleet_erasure', 'audit_log', 'DELETE')")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False


@pytest.mark.parametrize("table", ["trips", "idle_events"])
def test_the_erasure_role_cannot_delete_the_operational_record(
    admin_conn: Conn, table: str
) -> None:
    """Erasure must never be able to destroy the fleet's own business record: that
    would silently change historical fuel totals and make a past report
    irreproducible."""
    with admin_conn.cursor() as cur:
        for privilege in ("DELETE", "UPDATE"):
            cur.execute(
                "SELECT has_table_privilege('fleet_erasure', %s, %s)",
                (table, privilege),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] is False, f"fleet_erasure can {privilege} {table}"


def test_the_erasure_role_cannot_delete_its_own_audit_record(admin_conn: Conn) -> None:
    """Evidence of compliance that the erasure job itself can delete is not evidence."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT has_table_privilege('fleet_erasure', 'erasure_log', 'DELETE')")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False


def test_the_erasure_role_has_no_login(admin_conn: Conn) -> None:
    """Reached only via SET ROLE from an authenticated connection, so it adds no new
    credential to manage."""
    with admin_conn.cursor() as cur:
        cur.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'fleet_erasure'")
        row = cur.fetchone()
    assert row is not None
    assert row[0] is False
