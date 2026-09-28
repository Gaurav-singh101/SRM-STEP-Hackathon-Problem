"""Forward-only SQL migration runner.

Plain numbered ``.sql`` files with a ledger table, rather than Alembic. The schema
is hand-written for partitioning, row-level security and partial indexes that an
autogenerating tool models poorly, so the ORM-agnostic route removes a dependency
and keeps the SQL reviewable as SQL (PDF S8 expects the schema to be inspectable).

Each file runs inside one transaction: a failing migration leaves no partial
schema behind. Applied files are recorded with a checksum, so editing a migration
that has already run is detected rather than silently ignored.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Final

import psycopg
import psycopg.rows

from fleet_common.settings import postgres_settings

MIGRATIONS_DIR: Final[Path] = Path(__file__).parent / "migrations"

LEDGER_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     text PRIMARY KEY,
    checksum    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer     NOT NULL
);
"""


class MigrationError(RuntimeError):
    """A migration could not be applied, or an applied one was modified."""


def discover() -> list[Path]:
    """Return migration files in lexical order, which is also numeric order."""
    if not MIGRATIONS_DIR.is_dir():
        raise MigrationError(f"migrations directory not found: {MIGRATIONS_DIR}")
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise MigrationError(f"no .sql migrations found in {MIGRATIONS_DIR}")
    return files


def checksum(path: Path) -> str:
    """SHA-256 of the file body, used to detect edits to applied migrations."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def applied(conn: psycopg.Connection[tuple[str, ...]]) -> dict[str, str]:
    """Map of already-applied migration name to the checksum recorded for it."""
    with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
        cur.execute("SELECT version, checksum FROM schema_migrations")
        return {str(version): str(recorded) for version, recorded in cur.fetchall()}


def _verify_unchanged(files: list[Path], already: dict[str, str]) -> None:
    """Refuse to continue if an already-applied migration has been edited.

    Silently ignoring the edit is worse: the database and the repository would
    disagree about the schema with nothing to show it.
    """
    for path in files:
        recorded = already.get(path.name)
        if recorded is not None and recorded != checksum(path):
            raise MigrationError(
                f"{path.name} has changed since it was applied. Migrations are "
                "forward-only: add a new migration instead of editing this one. "
                "(For a local throwaway database, recreate it.)"
            )


def up(*, dry_run: bool = False) -> int:
    """Apply every pending migration. Returns the number applied."""
    settings = postgres_settings()
    count = 0

    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)

        files = discover()
        already = applied(conn)
        _verify_unchanged(files, already)

        pending = [f for f in files if f.name not in already]
        if not pending:
            print(f"Up to date: {len(already)} migration(s) already applied.")
            return 0

        for path in pending:
            if dry_run:
                print(f"  would apply  {path.name}")
                count += 1
                continue

            sql = path.read_text(encoding="utf-8")
            print(f"  applying     {path.name} ... ", end="", flush=True)

            # One transaction per migration: a failure rolls the whole file back
            # rather than leaving the schema half-built.
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '300s'")
                start = cur.connection.execute("SELECT clock_timestamp()").fetchone()
                cur.execute(sql)
                end = cur.connection.execute("SELECT clock_timestamp()").fetchone()
                elapsed_ms = 0
                if start and end:
                    elapsed_ms = int((end[0] - start[0]).total_seconds() * 1000)
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum, duration_ms)"
                    " VALUES (%s, %s, %s)",
                    (path.name, checksum(path), elapsed_ms),
                )

            print(f"ok ({elapsed_ms} ms)")
            count += 1

    return count


def status() -> None:
    """Print applied and pending migrations."""
    settings = postgres_settings()
    with psycopg.connect(settings.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(LEDGER_DDL)
        already = applied(conn)

    print(f"Database: {settings.safe_dsn}")
    for path in discover():
        if path.name in already:
            mark = "ok   " if already[path.name] == checksum(path) else "EDITED"
            print(f"  [{mark}] {path.name}")
        else:
            print(f"  [pending] {path.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply SQL migrations.")
    parser.add_argument("command", choices=["up", "status"])
    parser.add_argument(
        "--dry-run", action="store_true", help="list pending migrations without applying"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "up":
            applied_count = up(dry_run=args.dry_run)
            if applied_count:
                verb = "would apply" if args.dry_run else "applied"
                print(f"{verb} {applied_count} migration(s).")
        else:
            status()
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        # Never echo the DSN here: it carries the password.
        print(f"database error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
