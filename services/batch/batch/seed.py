"""Seed the reference dataset: tenants, fleets, vehicles, drivers and fuel prices.

The hackathon deliverable (PDF S13) requires a seeded 100,000-vehicle dataset in the
repository's one-command setup. The roster comes from the *same* pure function the
simulator uses, so the two cannot disagree about which vehicles exist - a telemetry
event for a VIN missing from this table has no powertrain, and a vehicle with no
powertrain has no cost.

Writes are batched with ``COPY`` rather than row-by-row inserts: per-row round trips
are forbidden in any bulk path (CLAUDE.md S3/S9), and at 100,000 vehicles the
difference is minutes against seconds.

    uv run python -m batch.seed --vehicles 100000
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import psycopg
import psycopg.rows

from fleet_common.settings import cost_settings, postgres_settings, simulator_settings
from simulator.fleet import DEMO_TENANT_IDS, SyntheticVehicle, build_fleet

#: Rows come back as tuples, so index access is type-checked rather than ignored.
Row = tuple[Any, ...]

#: Display metadata for the tenants the roster assigns vehicles to. The tenant IDs
#: themselves come from the simulator, so the seed and the telemetry cannot disagree
#: about which tenant a VIN belongs to.
_TENANT_NAMES: Final[dict[str, str]] = {
    "tenant-demo": "Northwind Logistics",
    "tenant-second": "Second Fleet Co",
}
DEFAULT_TIMEZONE: Final[str] = "Asia/Kolkata"

DEMO_TENANTS: Final[tuple[tuple[str, str, str], ...]] = tuple(
    (tenant_id, _TENANT_NAMES.get(tenant_id, tenant_id), DEFAULT_TIMEZONE)
    for tenant_id in DEMO_TENANT_IDS
)

FLEETS_PER_TENANT: Final[int] = 4

#: Synthetic prices, labelled as such in the table's `source` column so provenance
#: travels with the data rather than living only in a document (ADR-0002).
SYNTHETIC_PRICES: Final[tuple[tuple[str, str], ...]] = (
    ("diesel", "92.5000"),
    ("petrol", "104.7500"),
)


def _set_tenant(cursor: psycopg.Cursor[Row], tenant_id: str) -> None:
    """Apply the RLS tenant context.

    ``set_config`` rather than ``SET``: ``SET`` takes no bind parameters, so using it
    would mean interpolating a value into SQL (CLAUDE.md S10).
    """
    cursor.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))


def seed_tenants(conn: psycopg.Connection[Row], currency: str) -> None:
    with conn.cursor() as cur:
        for tenant_id, name, timezone in DEMO_TENANTS:
            _set_tenant(cur, tenant_id)
            cur.execute(
                "INSERT INTO tenants (tenant_id, name, currency, timezone)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id) DO NOTHING",
                (tenant_id, name, currency, timezone),
            )
            cur.execute(
                "INSERT INTO subscriptions (tenant_id, plan_code, seats, started_on)"
                " VALUES (%s, 'enterprise', 25, %s)"
                " ON CONFLICT DO NOTHING",
                (tenant_id, datetime.now(UTC).date()),
            )


def seed_fleets(conn: psycopg.Connection[Row]) -> dict[tuple[str, int], int]:
    """Create fleets and return a (tenant_id, fleet_index) -> fleet_id map."""
    mapping: dict[tuple[str, int], int] = {}
    with conn.cursor() as cur:
        for tenant_id, _, _ in DEMO_TENANTS:
            _set_tenant(cur, tenant_id)
            for index in range(FLEETS_PER_TENANT):
                name = f"Depot {index + 1}"
                cur.execute(
                    "INSERT INTO fleets (tenant_id, name) VALUES (%s, %s)"
                    " ON CONFLICT (tenant_id, name) DO NOTHING",
                    (tenant_id, name),
                )
                cur.execute(
                    "SELECT fleet_id FROM fleets WHERE tenant_id = %s AND name = %s",
                    (tenant_id, name),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"fleet {name} for {tenant_id} was not created")
                mapping[(tenant_id, index)] = int(row[0])
    return mapping


def seed_prices(conn: psycopg.Connection[Row], currency: str) -> None:
    """Insert one open-ended current price per fuel type, per tenant.

    Open-ended (``effective_to IS NULL``) and unique per (tenant, fuel, region), so
    the point-in-time lookup is unambiguous - two current prices would let two runs
    legitimately disagree about the same cost (D11).
    """
    effective_from = datetime.now(UTC) - timedelta(days=30)
    with conn.cursor() as cur:
        for tenant_id, _, _ in DEMO_TENANTS:
            _set_tenant(cur, tenant_id)
            for fuel_type, price in SYNTHETIC_PRICES:
                cur.execute(
                    "INSERT INTO fuel_prices (price_id, tenant_id, fuel_type, region,"
                    " price_per_litre, currency, effective_from, source)"
                    " VALUES (%s, %s, %s, 'default', %s, %s, %s, 'synthetic')"
                    " ON CONFLICT (price_id) DO NOTHING",
                    (
                        f"{tenant_id}-{fuel_type}-current",
                        tenant_id,
                        fuel_type,
                        Decimal(price),
                        currency,
                        effective_from,
                    ),
                )


def seed_vehicles(
    conn: psycopg.Connection[Row],
    vehicles: list[SyntheticVehicle],
    fleet_ids: dict[tuple[str, int], int],
) -> int:
    """Bulk-load vehicles with COPY, one tenant at a time.

    One COPY per tenant rather than one overall: the RLS policy is evaluated per row
    against the session tenant, so rows for a different tenant would be refused.
    Batching per tenant keeps both the isolation guarantee and the bulk path.
    """
    written = 0
    by_tenant: dict[str, list[SyntheticVehicle]] = {}
    for vehicle in vehicles:
        by_tenant.setdefault(vehicle.tenant_id, []).append(vehicle)

    with conn.cursor() as cur:
        for tenant_id, group in by_tenant.items():
            _set_tenant(cur, tenant_id)
            # COPY into a TEMP table, then INSERT ... ON CONFLICT DO NOTHING.
            #
            # A bare COPY straight into `vehicles` is not idempotent, and that is not a
            # theoretical objection: `build_vin` is a function of the vehicle index *only*,
            # deliberately, so that "vehicle 7" is the same VIN at every seed and every fleet
            # size. Scaling a 200-vehicle demo up to 100,000 is therefore the intended
            # workflow - and it failed on `duplicate key value violates unique constraint
            # "vehicles_pkey"` at the very first row, because the first 200 VINs are by
            # design identical.
            #
            # The alternative was --truncate, which would have deleted the roster the
            # existing telemetry refers to and left the dashboard with 100,000 vehicles and
            # no idle events. Every other derived write in this system is an upsert keyed on
            # its natural key; the seed job was the exception, and it had no test to notice.
            #
            # The TEMP table keeps the bulk path: still one COPY per tenant, still no
            # per-row round trips (CLAUDE.md S3/S9).
            cur.execute(
                "CREATE TEMP TABLE vehicles_incoming"
                " (LIKE vehicles INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            with cur.copy(
                "COPY vehicles_incoming (vin, tenant_id, fleet_id, powertrain_code,"
                " class_code, fuel_type, registration, model_year, is_active) FROM STDIN"
            ) as copy:
                for vehicle in group:
                    copy.write_row(
                        (
                            vehicle.vin,
                            vehicle.tenant_id,
                            fleet_ids[(vehicle.tenant_id, vehicle.fleet_index)],
                            vehicle.powertrain.value,
                            vehicle.vehicle_class.value,
                            vehicle.fuel_type,
                            vehicle.registration,
                            vehicle.model_year,
                            True,
                        )
                    )
            cur.execute(
                "INSERT INTO vehicles (vin, tenant_id, fleet_id, powertrain_code,"
                " class_code, fuel_type, registration, model_year, is_active)"
                " SELECT vin, tenant_id, fleet_id, powertrain_code, class_code,"
                " fuel_type, registration, model_year, is_active FROM vehicles_incoming"
                " ON CONFLICT (vin) DO NOTHING"
            )
            # rowcount, not len(group): re-running must report what it actually inserted,
            # not restate its input. A seed that claims 100,000 writes on a no-op run is the
            # same class of lie as a coverage figure from an earlier commit.
            written += max(cur.rowcount, 0)
            cur.execute("DROP TABLE IF EXISTS vehicles_incoming")
    return written


def seed_drivers(
    conn: psycopg.Connection[Row], vehicles: list[SyntheticVehicle], per_fleet: int
) -> int:
    """Create synthetic drivers and assign one to each vehicle.

    Drivers exist so the right-to-erasure flow has a subject to erase (PDF S11).
    Names are obviously synthetic: PDF S14 forbids real personal data, and a
    plausible-looking fake name invites someone to mistake it for real.
    """
    written = 0
    with conn.cursor() as cur:
        for tenant_id, _, _ in DEMO_TENANTS:
            _set_tenant(cur, tenant_id)
            tenant_vehicles = [v for v in vehicles if v.tenant_id == tenant_id]
            driver_count = max(1, min(len(tenant_vehicles), per_fleet * FLEETS_PER_TENANT))

            # Same TEMP-table upsert as vehicles, and for the same reason: licence_ref is
            # derived from the driver index, so a re-run regenerates identical references and
            # a bare COPY dies on drivers_licence_unique_per_tenant. Fixing only `vehicles`
            # moved the failure one table along - the whole seed has to be re-runnable, not
            # just its first step.
            cur.execute(
                "CREATE TEMP TABLE drivers_incoming"
                " (LIKE drivers INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            with cur.copy(
                "COPY drivers_incoming (tenant_id, full_name, licence_ref) FROM STDIN"
            ) as copy:
                for index in range(driver_count):
                    copy.write_row(
                        (
                            tenant_id,
                            f"SYNTHETIC DRIVER {index:05d}",
                            f"LIC-{tenant_id[-6:]}-{index:05d}",
                        )
                    )
            cur.execute(
                "INSERT INTO drivers (tenant_id, full_name, licence_ref)"
                " SELECT tenant_id, full_name, licence_ref FROM drivers_incoming"
                " ON CONFLICT (tenant_id, licence_ref) DO NOTHING"
            )
            written += max(cur.rowcount, 0)
            cur.execute("DROP TABLE IF EXISTS drivers_incoming")

            cur.execute(
                "SELECT driver_id FROM drivers WHERE tenant_id = %s ORDER BY driver_id",
                (tenant_id,),
            )
            driver_ids = [int(r[0]) for r in cur.fetchall()]
            assigned_from = datetime.now(UTC) - timedelta(days=90)

            cur.execute(
                "CREATE TEMP TABLE assignments_incoming"
                " (LIKE vehicle_assignments INCLUDING DEFAULTS) ON COMMIT DROP"
            )
            with cur.copy(
                "COPY assignments_incoming (tenant_id, vin, driver_id, assigned_from) FROM STDIN"
            ) as copy:
                for position, vehicle in enumerate(tenant_vehicles):
                    copy.write_row(
                        (
                            tenant_id,
                            vehicle.vin,
                            driver_ids[position % len(driver_ids)],
                            assigned_from,
                        )
                    )
            # The conflict target repeats the index predicate, because
            # vehicle_assignments_one_open_per_vin is a PARTIAL unique index on
            # (vin) WHERE assigned_to IS NULL. Omitting the WHERE makes PostgreSQL refuse
            # the statement with "no unique or exclusion constraint matching the ON CONFLICT
            # specification" - a trap this project has already hit once elsewhere.
            cur.execute(
                "INSERT INTO vehicle_assignments (tenant_id, vin, driver_id, assigned_from)"
                " SELECT tenant_id, vin, driver_id, assigned_from FROM assignments_incoming"
                " ON CONFLICT (vin) WHERE assigned_to IS NULL DO NOTHING"
            )
            cur.execute("DROP TABLE IF EXISTS assignments_incoming")
    return written


def main(argv: list[str] | None = None) -> int:
    sim = simulator_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicles", type=int, default=sim.sim_vehicle_count)
    parser.add_argument("--seed", type=int, default=sim.sim_seed)
    parser.add_argument("--drivers-per-fleet", type=int, default=25)
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="delete existing vehicles and drivers first (local development only)",
    )
    args = parser.parse_args(argv)

    settings = postgres_settings()
    currency = cost_settings().default_currency
    tenant_ids = tuple(t[0] for t in DEMO_TENANTS)

    print(f"Building roster: {args.vehicles:,} vehicles, seed {args.seed}")
    vehicles = build_fleet(
        vehicle_count=args.vehicles,
        seed=args.seed,
        tenant_ids=tenant_ids,
        fleets_per_tenant=FLEETS_PER_TENANT,
    )

    began = time.perf_counter()
    try:
        with psycopg.connect(settings.dsn, row_factory=psycopg.rows.tuple_row) as conn:
            if args.truncate:
                with conn.cursor() as cur:
                    # Ordered by dependency. Explicitly local-only: a truncate against
                    # a shared database would destroy another developer's state.
                    cur.execute("TRUNCATE vehicle_assignments, drivers, vehicles CASCADE")
                conn.commit()
                print("  truncated existing vehicles, drivers and assignments")

            seed_tenants(conn, currency)
            fleet_ids = seed_fleets(conn)
            seed_prices(conn, currency)
            print(f"  tenants: {len(DEMO_TENANTS)}  fleets: {len(fleet_ids)}")

            written = seed_vehicles(conn, vehicles, fleet_ids)
            drivers = seed_drivers(conn, vehicles, args.drivers_per_fleet)
            conn.commit()

            # Inserted-this-run AND the resulting total, because the two diverge on a
            # re-run and only one of them answers "is the dataset there?". An earlier
            # version printed "drivers: 2 (assigned to every vehicle)" after a scale-up,
            # which is a true rowcount that reads as though the fleet had two drivers.
            with conn.cursor() as cur:
                # SET ROLE, because these counts deliberately cross tenants and RLS is
                # FORCEd - which binds the table owner too, so without this the "total"
                # would silently be whichever tenant's context happened to be left set.
                cur.execute("SET ROLE fleet_batch")
                cur.execute("SELECT count(*) FROM vehicles")
                row = cur.fetchone()
                total_vehicles = int(row[0]) if row else 0
                cur.execute("SELECT count(*) FROM drivers")
                row = cur.fetchone()
                total_drivers = int(row[0]) if row else 0
                cur.execute(
                    "SELECT count(*) FROM vehicles v WHERE NOT EXISTS ("
                    " SELECT 1 FROM vehicle_assignments a"
                    " WHERE a.vin = v.vin AND a.assigned_to IS NULL)"
                )
                row = cur.fetchone()
                unassigned = int(row[0]) if row else 0

            print(f"  vehicles: +{written:,} this run, {total_vehicles:,} total")
            print(f"  drivers:  +{drivers:,} this run, {total_drivers:,} total")
            if unassigned:
                # Stated rather than assumed: a vehicle with no driver has no erasure
                # subject, and the right-to-erasure flow silently covers less than it looks.
                print(f"  WARNING: {unassigned:,} vehicle(s) have no open driver assignment")
            else:
                print(f"  every one of {total_vehicles:,} vehicles has an open assignment")
    except psycopg.Error as exc:
        # Never echo the DSN: it carries the password.
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1

    elapsed = time.perf_counter() - began
    rate = args.vehicles / elapsed if elapsed else 0.0
    print(f"\nSeeded in {elapsed:.2f}s ({rate:,.0f} vehicles/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
