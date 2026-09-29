"""Right to erasure across every store (PDF S11, CLAUDE.md S11).

GDPR Article 17 and India's DPDP Act both give a data subject the right to have their
personal data erased. For a telematics platform that is harder than a DELETE, because
the personal data is a driver's identity while the operational record - trips, idle
events, cost - is the fleet's own business record and must survive.

**What is erased:** the driver's identifying data, wherever it is held.
**What is kept:** the operational record, with the driver reference severed.

That split is the point. Deleting the idle events a driver was assigned to would erase
the *fleet's* record of its own fuel cost, silently change historical totals, and make
last month's report irreproducible. Anonymising the link instead satisfies the subject's
right while leaving the business record intact - and it is what the regulation actually
requires, since an anonymised record is no longer personal data.

**The erasure is itself audited.** The record that a subject's data was removed must
survive the removal, or the obligation cannot be demonstrated afterwards.

Runs against Postgres, MongoDB and Redis, because a right to erasure honoured in only
one store is not honoured at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
import psycopg.rows
from psycopg.rows import TupleRow

logger = logging.getLogger(__name__)

#: Replaces a name once erased. Deliberately not a realistic-looking placeholder: an
#: obviously synthetic value cannot be mistaken for surviving personal data.
ERASED_NAME = "[ERASED]"


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """What an erasure actually touched, per store.

    Reported per store rather than as one total, so a partial erasure is visible. "It
    worked" across three systems is a claim that needs evidence for each.
    """

    driver_id: int
    tenant_id: str
    erasure_id: int
    requested_by: str
    drivers_anonymised: int = 0
    assignments_severed: int = 0
    audit_rows_anonymised: int = 0
    mongo_documents_updated: int = 0
    redis_keys_removed: int = 0
    #: Operational rows deliberately retained, with the driver link severed.
    trips_retained: int = 0
    idle_events_retained: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, Any]:
        """Per-store outcome, stored on the audit record."""
        return {
            "drivers_anonymised": self.drivers_anonymised,
            "assignments_severed": self.assignments_severed,
            "audit_rows_anonymised": self.audit_rows_anonymised,
            "mongo_documents_updated": self.mongo_documents_updated,
            "redis_keys_removed": self.redis_keys_removed,
            "trips_retained": self.trips_retained,
            "idle_events_retained": self.idle_events_retained,
            "errors": self.errors,
        }


def _set_tenant(cur: psycopg.Cursor[TupleRow], tenant_id: str) -> None:
    cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))


def record_erasure_request(
    conn: psycopg.Connection[TupleRow],
    *,
    tenant_id: str,
    driver_id: int,
    requested_by: str,
) -> int:
    """Open the audit record *before* erasing anything.

    Before, not after, deliberately: if the erasure fails halfway there must still be a
    record that it was attempted, or a partial deletion leaves no trace of why data went
    missing.
    """
    with conn.cursor() as cur:
        cur.execute("SET ROLE fleet_erasure")
        cur.execute(
            "INSERT INTO erasure_log (tenant_id, subject_type, subject_ref,"
            " requested_at, requested_by) VALUES (%s, 'driver', %s, %s, %s)"
            " RETURNING erasure_id",
            (tenant_id, str(driver_id), datetime.now(UTC), requested_by),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("failed to open an erasure audit record")
        erasure_id = int(row[0])
    conn.commit()
    return erasure_id


def erase_driver_postgres(
    conn: psycopg.Connection[TupleRow], *, tenant_id: str, driver_id: int
) -> tuple[int, int, int, int, int]:
    """Erase a driver's identity from Postgres, keeping the operational record.

    Returns (drivers, assignments, audit rows, trips retained, idle events retained).
    """
    with conn.cursor() as cur:
        cur.execute("SET ROLE fleet_erasure")
        _set_tenant(cur, tenant_id)

        # Count what will be retained, so the audit record can state it. A deletion that
        # does not say what it left behind invites the question later.
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM trips t
                  WHERE t.vin IN (SELECT vin FROM vehicle_assignments
                                   WHERE driver_id = %(driver_id)s)),
                (SELECT count(*) FROM idle_events i
                  WHERE i.vin IN (SELECT vin FROM vehicle_assignments
                                   WHERE driver_id = %(driver_id)s))
            """,
            {"driver_id": driver_id},
        )
        retained = cur.fetchone() or (0, 0)
        trips_retained, idle_retained = int(retained[0]), int(retained[1])

        # 1. The identity itself. Anonymised rather than deleted so foreign keys and
        #    historical aggregates stay intact; an anonymised row is no longer personal
        #    data, which is what the regulation asks for.
        cur.execute(
            """
            UPDATE drivers
               SET full_name = %(name)s,
                   licence_ref = 'ERASED-' || driver_id::text,
                   erased_at = now()
             WHERE driver_id = %(driver_id)s
               AND erased_at IS NULL
            """,
            {"driver_id": driver_id, "name": ERASED_NAME},
        )
        drivers = max(cur.rowcount, 0)

        # 2. Close open assignments. The link between person and vehicle is itself
        #    personal data: "who was driving this van" identifies someone.
        cur.execute(
            "UPDATE vehicle_assignments SET assigned_to = now()"
            " WHERE driver_id = %s AND assigned_to IS NULL",
            (driver_id,),
        )
        assignments = max(cur.rowcount, 0)

        # 3. The audit trail names the subject. It must be retained as evidence of
        #    access, but the identifier within it is erasable.
        cur.execute("SET ROLE fleet_erasure")
        cur.execute(
            "UPDATE audit_log SET resource_id = 'ERASED'"
            " WHERE tenant_id = %s AND resource_type = 'driver' AND resource_id = %s",
            (tenant_id, str(driver_id)),
        )
        audit_rows = max(cur.rowcount, 0)

    conn.commit()
    return drivers, assignments, audit_rows, trips_retained, idle_retained


def erase_driver_mongo(mongo_client: Any, database: str, driver_id: int) -> int:
    """Remove driver references from raw telemetry.

    Raw telemetry is keyed by VIN, not by driver, so in this schema there is usually
    nothing to erase. The step runs regardless and reports its count: "we checked and
    found none" is a materially different statement from "we did not check", and only
    the first can be evidenced.
    """
    collection = mongo_client[database]["telemetry_raw"]
    result = collection.update_many(
        {"driver_id": driver_id},
        {"$unset": {"driver_id": ""}, "$set": {"driver_erased": True}},
    )
    return int(result.modified_count)


def erase_driver_redis(redis_client: Any, driver_id: int) -> int:
    """Drop cached driver state.

    Redis is a disposable cache, but a right to erasure is not honoured while a copy of
    the data is still being served from one.
    """
    removed = 0
    for pattern in (f"driver:{driver_id}", f"driver:{driver_id}:*"):
        for key in redis_client.scan_iter(match=pattern, count=1000):
            redis_client.delete(key)
            removed += 1
    return removed


def complete_erasure(
    conn: psycopg.Connection[TupleRow], *, erasure_id: int, result: ErasureResult
) -> None:
    """Close the audit record with the per-store outcome."""
    import orjson

    with conn.cursor() as cur:
        cur.execute("SET ROLE fleet_erasure")
        cur.execute(
            "UPDATE erasure_log SET completed_at = %s, stores_affected = %s WHERE erasure_id = %s",
            (datetime.now(UTC), orjson.dumps(result.summary()).decode(), erasure_id),
        )
    conn.commit()


def erase_driver(
    *,
    dsn: str,
    tenant_id: str,
    driver_id: int,
    requested_by: str,
    mongo_client: Any = None,
    mongo_database: str = "fleet_telemetry",
    redis_client: Any = None,
) -> ErasureResult:
    """Erase one driver's personal data across every store.

    Each store is attempted independently and its failure recorded rather than raised.
    Aborting on the first failure would leave the subject's data erased from some stores
    and present in others, with no record of which - the worst possible outcome, because
    it is neither compliant nor recoverable.
    """
    errors: list[str] = []

    with psycopg.connect(dsn, row_factory=psycopg.rows.tuple_row) as conn:
        erasure_id = record_erasure_request(
            conn, tenant_id=tenant_id, driver_id=driver_id, requested_by=requested_by
        )

        try:
            drivers, assignments, audit_rows, trips, idle = erase_driver_postgres(
                conn, tenant_id=tenant_id, driver_id=driver_id
            )
        except psycopg.Error as exc:
            # Roll back before continuing. Postgres refuses every further statement on
            # an aborted transaction, so without this the audit write below would fail
            # too - and the whole point of recording rather than raising is that the
            # attempt leaves a trace even when it goes wrong.
            conn.rollback()
            errors.append(f"postgres: {exc}")
            drivers = assignments = audit_rows = trips = idle = 0

        mongo_updated = 0
        if mongo_client is not None:
            try:
                mongo_updated = erase_driver_mongo(mongo_client, mongo_database, driver_id)
            except Exception as exc:
                errors.append(f"mongo: {exc}")

        redis_removed = 0
        if redis_client is not None:
            try:
                redis_removed = erase_driver_redis(redis_client, driver_id)
            except Exception as exc:
                errors.append(f"redis: {exc}")

        result = ErasureResult(
            driver_id=driver_id,
            tenant_id=tenant_id,
            erasure_id=erasure_id,
            requested_by=requested_by,
            drivers_anonymised=drivers,
            assignments_severed=assignments,
            audit_rows_anonymised=audit_rows,
            mongo_documents_updated=mongo_updated,
            redis_keys_removed=redis_removed,
            trips_retained=trips,
            idle_events_retained=idle,
            errors=errors,
        )

        complete_erasure(conn, erasure_id=erasure_id, result=result)

    if errors:
        logger.error(
            "erasure %d for driver %d completed with errors: %s",
            erasure_id,
            driver_id,
            errors,
        )
    return result


def verify_erasure(
    conn: psycopg.Connection[TupleRow], *, tenant_id: str, driver_id: int
) -> dict[str, Any]:
    """Check afterwards that no identifying data survived.

    A separate read path rather than trusting the write path's own report: the test that
    matters is what a reader can still see, not what the eraser believed it did.
    """
    with conn.cursor() as cur:
        # Read-only verification, so the general maintenance role is enough; the
        # narrowly-scoped erasure role exists for the writes.
        cur.execute("SET ROLE fleet_batch")
        _set_tenant(cur, tenant_id)
        cur.execute(
            """
            SELECT
                (SELECT count(*) FROM drivers
                  WHERE driver_id = %(driver_id)s AND full_name <> %(name)s),
                (SELECT count(*) FROM drivers
                  WHERE driver_id = %(driver_id)s AND erased_at IS NOT NULL),
                (SELECT count(*) FROM vehicle_assignments
                  WHERE driver_id = %(driver_id)s AND assigned_to IS NULL),
                (SELECT count(*) FROM audit_log
                  WHERE resource_type = 'driver'
                    AND resource_id = %(driver_ref)s),
                (SELECT count(*) FROM erasure_log
                  WHERE subject_ref = %(driver_ref)s AND completed_at IS NOT NULL)
            """,
            {
                "driver_id": driver_id,
                "name": ERASED_NAME,
                "driver_ref": str(driver_id),
            },
        )
        row = cur.fetchone() or (0, 0, 0, 0, 0)

    return {
        "identifying_names_remaining": int(row[0]),
        "marked_erased": int(row[1]),
        "open_assignments_remaining": int(row[2]),
        "audit_rows_naming_subject": int(row[3]),
        "completed_erasure_records": int(row[4]),
        # The whole obligation in one boolean, so a test or an operator has a single
        # thing to assert rather than five.
        "erased": (
            int(row[0]) == 0
            and int(row[1]) == 1
            and int(row[2]) == 0
            and int(row[3]) == 0
            and int(row[4]) >= 1
        ),
    }
