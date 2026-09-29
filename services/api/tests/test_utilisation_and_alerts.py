"""HTTP contract for the utilisation and alerts endpoints.

These two views answer two of the five questions the product exists to answer - which
vehicles are under-used, and what needs attention - and both carry a figure that is easy
to read wrongly. The tests here are mostly about those readings: coverage that must not
appear as full, and a latency figure that must not be quoted without the context that
decides whether it means anything.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from api.testing import (
    NOW,
    TENANT_B,
    FakeRepository,
    make_alert_row,
    make_coverage,
    make_leaderboard_row,
    make_utilisation_row,
    make_utilisation_snapshot,
    make_vehicle_detail,
)

PROBLEM_JSON = "application/problem+json"


# ------------------------------------------------------------------ utilisation


def test_utilisation_lists_the_worst_vehicles_first(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """The point of the view is the bottom of the list, so it belongs on the first page."""
    repo.utilisation_rows = [
        make_utilisation_row(vin="ZZAAAAAAAAAAAAAA1", score=72.0, band="normal"),
        make_utilisation_row(vin="ZZAAAAAAAAAAAAAA2", score=11.5, band="low"),
        make_utilisation_row(vin="ZZAAAAAAAAAAAAAA3", score=41.0, band="normal"),
    ]
    body = client.get("/api/v1/utilisation", headers=auth_header).json()
    assert [item["score"] for item in body["items"]] == [11.5, 41.0, 72.0]


def test_utilisation_reports_how_much_of_the_fleet_it_could_score(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """Without this the table reads as the whole fleet, which it is not."""
    repo.utilisation_rows = [make_utilisation_row()]
    repo.coverage = make_coverage(
        vehicles_active=2000,
        scored=180,
        unscored_no_telemetry=1808,
        unscored_class_too_small=12,
    )
    coverage = client.get("/api/v1/utilisation", headers=auth_header).json()["coverage"]
    assert coverage["vehicles_active"] == 2000
    assert coverage["scored"] == 180
    assert coverage["unscored_no_telemetry"] == 1808
    assert coverage["unscored_class_too_small"] == 12


def test_the_two_unscored_reasons_stay_apart(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """They prompt different actions: chase a silent unit, or accept a method limit.

    A single combined total would answer neither question, so the response must never
    collapse them into one field.
    """
    repo.utilisation_rows = [make_utilisation_row()]
    repo.coverage = make_coverage(
        vehicles_active=100,
        scored=60,
        unscored_no_telemetry=35,
        unscored_class_too_small=5,
    )
    coverage = client.get("/api/v1/utilisation", headers=auth_header).json()["coverage"]
    assert "unscored" not in coverage
    assert coverage["unscored_no_telemetry"] == 35
    assert coverage["unscored_class_too_small"] == 5


def test_coverage_is_null_rather_than_zero_when_nothing_has_been_scored(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """No window scored yet is a different statement from every vehicle scored.

    Reporting zeros for the first would be read as the second, which is the exact
    misreading this field was added to remove.
    """
    repo.utilisation_rows = []
    repo.coverage = None
    body = client.get("/api/v1/utilisation", headers=auth_header).json()
    assert body["coverage"] is None


def test_an_abnormal_vehicle_carries_its_reason_in_words(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """A band name alone does not tell an operations manager what to do about it."""
    repo.utilisation_rows = [
        make_utilisation_row(
            band="abnormal",
            active_day_component=0.95,
            moving_hours_component=0.12,
            distance_component=0.15,
            abnormal_reason="out almost every day but barely moving",
        )
    ]
    item = client.get("/api/v1/utilisation", headers=auth_header).json()["items"][0]
    assert item["band"] == "abnormal"
    assert item["abnormal_reason"] == "out almost every day but barely moving"
    # The spread is what decided the band, so it must be visible beside the reason.
    assert item["component_spread"] > 0.6


def test_utilisation_declares_the_baseline_it_was_scored_against(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """A score from 5 peers and one from 500 must not present identically."""
    repo.utilisation_rows = [make_utilisation_row(baseline_sample_size=7)]
    item = client.get("/api/v1/utilisation", headers=auth_header).json()["items"][0]
    assert item["baseline_sample_size"] == 7
    assert item["formula_version"] == "utilisation-v1"


def test_a_band_filter_is_allow_listed(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """An unknown band must be an error, not an empty page.

    Silently returning nothing for a typo would look identical to no vehicles being in
    that band, and a caller would draw the wrong conclusion from it.

    400, not 422: the band is checked by the application after FastAPI has accepted the
    string, so it surfaces as ``ValidationFailedError``. Constraints FastAPI enforces
    itself - see ``test_the_alert_limit_is_capped`` - answer 422. Both are pinned here so
    the split is a visible property of the API rather than a surprise to a client.
    """
    repo.utilisation_rows = [make_utilisation_row(band="low")]
    response = client.get("/api/v1/utilisation?band=lo", headers=auth_header)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["type"].endswith("validation-failed")

    ok = client.get("/api/v1/utilisation?band=low", headers=auth_header)
    assert ok.status_code == 200
    assert len(ok.json()["items"]) == 1


def test_utilisation_pages_with_a_cursor(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.utilisation_rows = [
        make_utilisation_row(vin=f"ZZAAAAAAAAAAAAA{i:02d}", score=float(i)) for i in range(1, 6)
    ]
    first = client.get("/api/v1/utilisation?page_size=2", headers=auth_header).json()
    assert [i["score"] for i in first["items"]] == [1.0, 2.0]
    assert first["page"]["has_more"] is True

    cursor = first["page"]["next_cursor"]
    second = client.get(
        f"/api/v1/utilisation?page_size=2&cursor={cursor}", headers=auth_header
    ).json()
    assert [i["score"] for i in second["items"]] == [3.0, 4.0]


def test_reading_utilisation_writes_an_audit_entry(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.utilisation_rows = [make_utilisation_row()]
    client.get("/api/v1/utilisation", headers=auth_header)
    assert [c["action"] for c in repo.audit_calls] == ["read_utilisation"]


# ----------------------------------------------------------------------- alerts


def test_alerts_are_newest_first(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.alert_rows = [
        make_alert_row(alert_id=1, triggering_event_ts=NOW - timedelta(hours=3)),
        make_alert_row(alert_id=2, triggering_event_ts=NOW),
        make_alert_row(alert_id=3, triggering_event_ts=NOW - timedelta(hours=1)),
    ]
    items = client.get("/api/v1/alerts", headers=auth_header).json()["items"]
    assert [i["alert_id"] for i in items] == [2, 3, 1]


def test_acknowledged_alerts_are_hidden_by_default(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """The view is a work queue, so something already handled does not belong in it."""
    repo.alert_rows = [
        make_alert_row(alert_id=1, acknowledged=True),
        make_alert_row(alert_id=2, acknowledged=False),
    ]
    default = client.get("/api/v1/alerts", headers=auth_header).json()
    assert [i["alert_id"] for i in default["items"]] == [2]
    assert default["unacknowledged_only"] is True

    everything = client.get("/api/v1/alerts?unacknowledged_only=false", headers=auth_header).json()
    assert {i["alert_id"] for i in everything["items"]} == {1, 2}


def test_a_replayed_alert_is_flagged_and_its_two_latencies_differ(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """Backfilled history produced an end-to-end figure of ~83,000 s once.

    That measured how old the data was, not how fast the system reacted. Both figures are
    returned and the backfill flag says which one means anything, because the alternative
    is quoting a reaction time that was never measured.
    """
    repo.alert_rows = [make_alert_row(ingest_lag_seconds=86_400.0, processing_seconds=2.4)]
    body = client.get("/api/v1/alerts", headers=auth_header).json()
    alert = body["items"][0]

    assert alert["is_backfill"] is True
    assert alert["end_to_end_latency_seconds"] > 86_000
    assert alert["processing_latency_seconds"] == 2.4
    assert body["contains_backfill"] is True
    # The quotable figure is the processing one, so that is what the header reports.
    assert body["max_processing_latency_seconds"] == 2.4


def test_a_live_alert_is_not_flagged_as_backfill(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.alert_rows = [make_alert_row(ingest_lag_seconds=1.2, processing_seconds=0.9)]
    body = client.get("/api/v1/alerts", headers=auth_header).json()
    assert body["items"][0]["is_backfill"] is False
    assert body["contains_backfill"] is False


def test_the_worst_processing_latency_is_reported_not_the_first(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """A header figure that is not the maximum understates the problem it exists to show."""
    repo.alert_rows = [
        make_alert_row(alert_id=1, processing_seconds=0.5),
        make_alert_row(alert_id=2, processing_seconds=4.75),
        make_alert_row(alert_id=3, processing_seconds=1.0),
    ]
    body = client.get("/api/v1/alerts", headers=auth_header).json()
    assert body["max_processing_latency_seconds"] == 4.75


def test_an_empty_alert_queue_reports_no_latency_rather_than_zero(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """Zero seconds would read as a perfect result rather than as no measurement."""
    repo.alert_rows = []
    body = client.get("/api/v1/alerts", headers=auth_header).json()
    assert body["items"] == []
    assert body["max_processing_latency_seconds"] is None


def test_an_alert_carries_the_detail_behind_it(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """An alert a manager cannot act on without opening another view is not finished."""
    repo.alert_rows = [make_alert_row()]
    alert = client.get("/api/v1/alerts", headers=auth_header).json()["items"][0]
    assert alert["detail"]["duration_seconds"] == 780.0
    assert alert["detail"]["threshold_seconds"] == 600
    assert alert["vin"]
    assert alert["registration"]


def test_reading_alerts_writes_an_audit_entry(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.alert_rows = [make_alert_row()]
    client.get("/api/v1/alerts", headers=auth_header)
    assert [c["action"] for c in repo.audit_calls] == ["read_alerts"]


def test_both_endpoints_require_a_token(client: TestClient) -> None:
    for path in ("/api/v1/utilisation", "/api/v1/alerts"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["content-type"].startswith(PROBLEM_JSON)


def test_the_alert_limit_is_capped(client: TestClient, auth_header: dict[str, str]) -> None:
    """An uncapped limit is a denial-of-service knob handed to the caller.

    422 because FastAPI rejects it from the route signature, before any handler runs.
    """
    response = client.get("/api/v1/alerts?limit=5000", headers=auth_header)
    assert response.status_code == 422


# --------------------------------------------------------------- vehicle detail


def test_a_vehicle_carries_its_own_utilisation_score(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """The detail view answers "what is this vehicle doing", which needs both halves.

    Cost without utilisation cannot tell a manager whether a van is expensive because it
    works hard or expensive because it sits with the engine running.
    """

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = make_vehicle_detail(
        vin=vin, utilisation=make_utilisation_snapshot(score=18.4, band="low")
    )
    body = client.get(f"/api/v1/vehicles/{vin}", headers=auth_header).json()

    assert body["utilisation"]["score"] == 18.4
    assert body["utilisation"]["band"] == "low"
    assert body["utilisation"]["baseline_sample_size"] == 240
    assert body["utilisation"]["formula_version"] == "utilisation-v1"


def test_an_unscored_vehicle_reports_null_rather_than_a_zero_score(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """A zero score would read as "barely used" when the truth is "we do not know".

    The vehicle still returns its master data and idle cost, because being unscored is not
    a reason to hide the rest.
    """

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = make_vehicle_detail(vin=vin, utilisation=None)
    body = client.get(f"/api/v1/vehicles/{vin}", headers=auth_header).json()

    assert body["utilisation"] is None
    assert body["idle_event_count"] == 9
    assert body["total_cost"] == "1250.5000"


def test_an_abnormal_vehicle_explains_itself_on_its_own_page(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = make_vehicle_detail(
        vin=vin,
        utilisation=make_utilisation_snapshot(
            band="abnormal", abnormal_reason="out almost every day but barely moving"
        ),
    )
    body = client.get(f"/api/v1/vehicles/{vin}", headers=auth_header).json()
    assert body["utilisation"]["abnormal_reason"] == "out almost every day but barely moving"


def test_an_electric_vehicle_has_no_fuel_cost_but_still_has_a_score(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """D9: BEVs are out of scope for idle fuel cost and in scope for utilisation.

    A page that hid the score because the cost is null would drop the only figure that
    applies to an electric vehicle.
    """

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = make_vehicle_detail(
        vin=vin,
        powertrain="BEV",
        burns_fuel=False,
        total_cost=None,
        utilisation=make_utilisation_snapshot(score=71.2, band="normal"),
    )
    body = client.get(f"/api/v1/vehicles/{vin}", headers=auth_header).json()

    assert body["burns_fuel"] is False
    assert body["total_cost"] is None
    assert body["utilisation"]["score"] == 71.2


def test_another_tenants_vehicle_is_absent_not_forbidden(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """403 would confirm the VIN exists somewhere, which is itself a disclosure."""

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = make_vehicle_detail(vin=vin, tenant_id=TENANT_B)
    response = client.get(f"/api/v1/vehicles/{vin}", headers=auth_header)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


# ------------------------------------------------------------------ leaderboard


def test_the_leaderboard_says_how_current_it_is(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """It is built from a rollup the batch job maintains, so it can be hours old.

    A ranking presented without that has a finance manager acting on yesterday's fleet as
    if it were today's - and the whole reason the query reads the rollup is speed, so the
    staleness is a deliberate trade rather than an accident to hide.
    """
    repo.leaderboard = [make_leaderboard_row()]
    body = client.get("/api/v1/leaderboard/idle-cost", headers=auth_header).json()
    assert body["data_through"] == "2026-09-29"
    assert body["computed_at"] is not None


def test_an_uncomputed_leaderboard_reports_null_not_a_date(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """No rollup yet is a different statement from a rollup covering nothing."""
    repo.leaderboard = []
    repo.leaderboard_data_through = None
    repo.leaderboard_computed_at = None
    body = client.get("/api/v1/leaderboard/idle-cost", headers=auth_header).json()
    assert body["items"] == []
    assert body["data_through"] is None
    assert body["computed_at"] is None
