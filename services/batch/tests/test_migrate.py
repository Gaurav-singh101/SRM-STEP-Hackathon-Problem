"""Tests for the migration runner's pure logic.

The database-touching paths are covered by the integration suite, which applies
these same files to a real Postgres. What is unit-tested here is the safety
machinery: discovery order, checksums, and the refusal to continue when an
already-applied migration has been edited.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from batch.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    _verify_unchanged,
    checksum,
    discover,
)


def test_migrations_directory_exists() -> None:
    assert MIGRATIONS_DIR.is_dir()


def test_discover_returns_files_in_numeric_order() -> None:
    """Order is correctness, not cosmetics: 003 grants privileges on tables that
    001 creates, so running them out of order fails."""
    names = [p.name for p in discover()]
    assert names == sorted(names)
    assert names[0].startswith("001_")


def test_every_migration_is_numbered() -> None:
    """A file without a numeric prefix would sort unpredictably against the rest."""
    for path in discover():
        prefix = path.name.split("_")[0]
        assert prefix.isdigit(), f"{path.name} has no numeric prefix"
        assert len(prefix) == 3, f"{path.name} prefix should be zero-padded to 3 digits"


def test_migration_numbers_are_unique() -> None:
    """Two files sharing a number make the apply order ambiguous."""
    prefixes = [p.name.split("_")[0] for p in discover()]
    assert len(prefixes) == len(set(prefixes)), f"duplicate migration numbers: {prefixes}"


def test_checksum_is_stable_for_the_same_content(tmp_path: Path) -> None:
    path = tmp_path / "001_x.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    assert checksum(path) == checksum(path)


def test_checksum_changes_when_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "001_x.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    before = checksum(path)
    path.write_text("SELECT 2;", encoding="utf-8")
    assert checksum(path) != before


def test_verify_unchanged_accepts_matching_checksums(tmp_path: Path) -> None:
    path = tmp_path / "001_x.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    _verify_unchanged([path], {"001_x.sql": checksum(path)})


def test_verify_unchanged_ignores_files_not_yet_applied(tmp_path: Path) -> None:
    path = tmp_path / "002_new.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    _verify_unchanged([path], {})


def test_verify_unchanged_rejects_an_edited_applied_migration(tmp_path: Path) -> None:
    """Silently ignoring the edit is worse than failing: the database and the
    repository would disagree about the schema with nothing to show it."""
    path = tmp_path / "001_x.sql"
    path.write_text("SELECT 1;", encoding="utf-8")
    stale = checksum(path)
    path.write_text("DROP TABLE vehicles;", encoding="utf-8")

    with pytest.raises(MigrationError, match="forward-only"):
        _verify_unchanged([path], {"001_x.sql": stale})


def test_discover_fails_clearly_when_there_are_no_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("batch.migrate.MIGRATIONS_DIR", tmp_path)
    with pytest.raises(MigrationError, match=r"no .sql migrations"):
        discover()


def test_discover_fails_clearly_when_the_directory_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("batch.migrate.MIGRATIONS_DIR", tmp_path / "nope")
    with pytest.raises(MigrationError, match="not found"):
        discover()


# --------------------------------------------------------------------------
# Guards on the SQL itself
# --------------------------------------------------------------------------


def test_no_migration_contains_a_hardcoded_password() -> None:
    """CLAUDE.md S3: no credential in code, config or history. The roles created in
    003 are NOLOGIN precisely so no password is ever needed."""
    for path in discover():
        body = path.read_text(encoding="utf-8").lower()
        for marker in ("password '", 'password "', "encrypted password"):
            assert marker not in body, f"{path.name} appears to set a role password"


def test_every_tenant_scoped_table_gets_row_level_security() -> None:
    """A cheap textual guard that the isolation migration was not skipped. The
    behavioural proof is in tests/integration/test_tenant_isolation.py."""
    combined = "\n".join(p.read_text(encoding="utf-8") for p in discover())
    assert "ENABLE ROW LEVEL SECURITY" in combined
    # ENABLE alone exempts the table owner, so FORCE must be present too.
    assert "FORCE ROW LEVEL SECURITY" in combined


def test_money_columns_never_use_floating_point() -> None:
    """D11: float cannot represent most decimal fractions, so accumulating currency
    drifts. Any cost column must be numeric(14,4)."""
    for path in discover():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower().strip()
            if lowered.startswith("--"):
                continue
            is_money_column = any(token in lowered for token in ("cost_est", "price_per_litre"))
            if is_money_column and ("double precision" in lowered or "real" in lowered):
                pytest.fail(f"{path.name}:{lineno} money column uses floating point: {line}")
