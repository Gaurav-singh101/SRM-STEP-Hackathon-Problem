"""Integration fixtures: real infrastructure via Testcontainers (CLAUDE.md S12).

A fresh container per session, with migrations applied from the repository, so
these tests verify the schema that actually ships rather than one hand-built for
testing. Slower than mocking, and worth it: row-level security, partitioning and
partial indexes are exactly the things a mock cannot tell you anything about.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from fleet_common.vin import repair_check_digit

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "services" / "batch" / "batch" / "migrations"

# Must match docker-compose.yml, so the tests exercise the shipped image.
POSTGRES_IMAGE = "postgres:16-alpine"

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-bravo"

#: One vehicle per tenant, each with a corrected check digit so it passes both the
#: schema's shape constraint and the domain's checksum validation.
TENANT_VINS = {
    TENANT_A: repair_check_digit("1HGCM82633A004352"),
    TENANT_B: repair_check_digit("5GZCZ43D13S812715"),
}


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """A throwaway Postgres with the real migrations applied."""
    container = (
        PostgresContainer(POSTGRES_IMAGE, dbname="fleet", username="fleet", password="test-only")
        # Deterministic collation, matching compose, so ORDER BY is stable and
        # keyset pagination behaves identically to production.
        .with_env("POSTGRES_INITDB_ARGS", "--encoding=UTF8 --locale=C")
    )
    with container as running:
        yield running


@pytest.fixture(scope="session")
def migrated_dsn(postgres_container: PostgresContainer) -> str:
    """Apply every migration once, then hand back the DSN."""
    dsn = (
        f"host={postgres_container.get_container_host_ip()} "
        f"port={postgres_container.get_exposed_port(5432)} "
        f"dbname=fleet user=fleet password=test-only"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
    return dsn


@pytest.fixture
def admin_conn(migrated_dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """Superuser connection, for schema assertions and seeding.

    This connection BYPASSES row-level security, and that is not a flaw in the
    policies: PostgreSQL exempts superusers and BYPASSRLS roles from RLS
    unconditionally. FORCE ROW LEVEL SECURITY closes the *table owner* gap, not
    the superuser one.

    The operational consequence is recorded in ADR-0006 and asserted by
    ``test_a_superuser_connection_bypasses_rls``: the application must never
    connect as a superuser. It connects as an ordinary role and switches to
    ``fleet_app`` - see the ``app_conn`` fixture, which is what the isolation
    tests actually use.
    """
    with psycopg.connect(migrated_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_conn(migrated_dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A connection in the application's real security context.

    Mirrors what the API does on checkout from the pool: SET ROLE to the
    least-privilege application role, then set the tenant from the validated
    token claim. Every isolation test runs through this, because testing the
    policies from a superuser session would prove nothing.
    """
    with psycopg.connect(migrated_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SET ROLE fleet_app")
        yield conn


@pytest.fixture
def seeded(admin_conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Two tenants, each with a fleet and a vehicle.

    Two tenants, not one: a single-tenant fixture cannot demonstrate isolation,
    which is the whole point of these tests.
    """
    with admin_conn.cursor() as cur:
        # BYPASSRLS on the owner role is not in play here, so set a context to
        # write; the seed writes each tenant under its own context.
        for tenant in (TENANT_A, TENANT_B):
            # SET cannot take a bind parameter; set_config() can. This is also
            # the only injection-safe way to apply a tenant from a token claim.
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
            cur.execute(
                "INSERT INTO tenants (tenant_id, name, currency, timezone)"
                " VALUES (%s, %s, 'INR', 'Asia/Kolkata')"
                " ON CONFLICT (tenant_id) DO NOTHING",
                (tenant, f"Fleet {tenant}"),
            )
            cur.execute(
                "INSERT INTO fleets (tenant_id, name) VALUES (%s, 'Depot 1')"
                " ON CONFLICT (tenant_id, name) DO NOTHING RETURNING fleet_id",
                (tenant,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "SELECT fleet_id FROM fleets WHERE tenant_id = %s AND name = 'Depot 1'",
                    (tenant,),
                )
                row = cur.fetchone()
            assert row is not None
            fleet_id = row[0]
            cur.execute(
                "INSERT INTO vehicles (vin, tenant_id, fleet_id, powertrain_code,"
                " class_code, fuel_type, registration, model_year)"
                " VALUES (%s, %s, %s, 'diesel', 'medium_truck', 'diesel', %s, 2022)"
                " ON CONFLICT (vin) DO NOTHING",
                (TENANT_VINS[tenant], tenant, fleet_id, f"REG-{tenant[-5:]}"),
            )
        cur.execute("SELECT set_config('app.tenant_id', '', false)")


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    """Skip integration tests when no container runtime is available."""
    if os.environ.get("SKIP_INTEGRATION"):
        pytest.skip("SKIP_INTEGRATION is set")
