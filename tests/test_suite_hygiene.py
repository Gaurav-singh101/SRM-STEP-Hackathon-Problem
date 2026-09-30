"""Structural guards on the test suite itself.

These exist because of a specific failure. `tests/integration/test_utilisation_window.py`
was written without `pytestmark = pytest.mark.integration`, so `-m "not integration"`
did not deselect it, so the unit suite tried to start a PostgreSQL container. With a Docker
daemon running that was merely slow; with the daemon stopped it blocked for minutes and
printed nothing at all.

The marker is the *only* thing separating the two suites, which makes omitting it a silent
opt-in to the wrong one. A file-level check is cheap and catches it at the point the file is
added rather than the next time Docker happens to be down.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INTEGRATION_DIR = REPO_ROOT / "tests" / "integration"

MARKER = re.compile(r"^pytestmark\s*=\s*pytest\.mark\.integration\s*$", re.MULTILINE)

# An import statement, not any mention of the word. The first version of this check matched
# the bare string and flagged this very file for discussing it in a docstring - a guard that
# fails on its own explanation is not a useful guard.
TESTCONTAINERS_IMPORT = re.compile(
    r"^\s*(?:from\s+testcontainers|import\s+testcontainers)", re.MULTILINE
)


def _integration_modules() -> list[Path]:
    return sorted(p for p in INTEGRATION_DIR.glob("test_*.py"))


def test_there_are_integration_modules_to_check() -> None:
    """Guards the guard: a glob that matches nothing would make the next test vacuous."""
    assert _integration_modules(), f"no test modules found under {INTEGRATION_DIR}"


def test_every_integration_module_declares_the_marker() -> None:
    """Otherwise it runs in the unit suite and needs infrastructure that is not there.

    The symptom is not a clear failure. It is a hang: Testcontainers waits on a Docker
    connection while pytest prints nothing, which reads as a slow test rather than a
    misconfigured one.
    """
    missing = [
        module.relative_to(REPO_ROOT).as_posix()
        for module in _integration_modules()
        if not MARKER.search(module.read_text(encoding="utf-8"))
    ]
    assert not missing, (
        "these modules need `pytestmark = pytest.mark.integration` at module level, or "
        f"they will be collected by the unit suite and block on Docker: {missing}"
    )


def test_the_unit_suite_does_not_import_testcontainers_outside_integration() -> None:
    """A module-level import is enough to make collection depend on the package.

    Importing it is harmless on its own; the point is that a *test* module outside
    tests/integration/ reaching for a container runtime is a design smell worth catching
    early, because that is how the unit suite acquires an infrastructure dependency.
    """
    offenders = []
    for path in REPO_ROOT.glob("tests/**/test_*.py"):
        if INTEGRATION_DIR in path.parents or path == Path(__file__):
            continue
        if TESTCONTAINERS_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert not offenders, f"unit-suite modules importing testcontainers: {offenders}"
