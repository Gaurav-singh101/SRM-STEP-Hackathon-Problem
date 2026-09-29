"""The utilisation window must account for every active vehicle exactly once.

These are tests of an arithmetic invariant rather than of a query, which is deliberate:
the reason the table exists is that a plausible-looking wrong total on a finance
dashboard is worse than a failed batch run, because nobody would know to distrust it.
"""

from __future__ import annotations

from datetime import date

import pytest

from batch.rollups import UtilisationWindowSummary

WINDOW_END = date(2026, 9, 29)


def _summary(**overrides: object) -> UtilisationWindowSummary:
    defaults: dict[str, object] = {
        "tenant_id": "tenant-demo",
        "window_end": WINDOW_END,
        "window_days": 30,
        "vehicles_active": 100,
        "scored": 80,
        "unscored_no_telemetry": 15,
        "unscored_class_too_small": 5,
        "classes_skipped": 1,
    }
    defaults.update(overrides)
    return UtilisationWindowSummary(**defaults)  # type: ignore[arg-type]


def test_a_window_where_every_vehicle_is_accounted_for_is_accepted() -> None:
    summary = _summary()
    assert summary.scored + summary.unscored_no_telemetry + summary.unscored_class_too_small == 100


def test_a_window_that_loses_vehicles_is_rejected() -> None:
    """The failure mode this guards: a dashboard showing 80 of 100 with no explanation."""
    with pytest.raises(ValueError, match="does not add up"):
        _summary(scored=80, unscored_no_telemetry=0, unscored_class_too_small=0)


def test_a_window_that_double_counts_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not add up"):
        _summary(scored=80, unscored_no_telemetry=15, unscored_class_too_small=10)


def test_the_error_names_every_count_so_the_miscount_is_locatable() -> None:
    with pytest.raises(ValueError) as caught:
        _summary(scored=1)
    message = str(caught.value)
    assert "tenant-demo" in message
    assert "2026-09-29" in message
    assert "1 scored" in message
    assert "15 no-telemetry" in message
    assert "5 class-too-small" in message
    assert "100 vehicles are active" in message


def test_an_empty_fleet_is_valid() -> None:
    """A tenant with no active vehicles is not an error, and must not raise."""
    summary = _summary(
        vehicles_active=0,
        scored=0,
        unscored_no_telemetry=0,
        unscored_class_too_small=0,
        classes_skipped=0,
    )
    assert summary.vehicles_active == 0


def test_classes_skipped_counts_classes_and_is_not_part_of_the_identity() -> None:
    """One skipped class can hold many vehicles, so the two counts cannot be the same.

    Pinned by a test because using ``classes_skipped`` in the vehicle accounting is the
    obvious mistake to make when reading the table later.
    """
    summary = _summary(classes_skipped=2, unscored_class_too_small=5)
    assert summary.classes_skipped == 2
    assert summary.unscored_class_too_small == 5


def test_the_formula_version_defaults_to_the_domain_constant() -> None:
    """So a score and the window that describes it can never claim different formulas."""
    from fleet_common.domain.utilisation import UTILISATION_FORMULA_VERSION

    assert _summary().formula_version == UTILISATION_FORMULA_VERSION
