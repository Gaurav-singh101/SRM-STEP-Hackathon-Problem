"""HTTP contract tests for the API.

Covers what the transport layer promises: status codes, RFC 7807 problem documents,
exact money serialisation, location masking by role, the pagination envelope, and an
audit entry for every access.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.testing import (
    NOW,
    PRECISE_LAT,
    TENANT_A,
    TENANT_B,
    FakeRepository,
    make_idle_row,
)
from fleet_common.auth import Role

PROBLEM_JSON = "application/problem+json"


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_healthz_needs_no_token(client: TestClient) -> None:
    """Liveness must not require auth, or a probe cannot reach it."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_does_not_touch_the_database(client: TestClient, repo: FakeRepository) -> None:
    """A liveness probe that depended on Postgres would restart a healthy process
    during a database blip, turning one outage into two."""
    repo.postgres_ready = False
    assert client.get("/healthz").status_code == 200


def test_readyz_reports_degraded_without_redis(client: TestClient) -> None:
    """Degraded, not unready.

    Redis backs rate limiting and caching, neither of which the API needs to answer
    correctly, so losing it must not pull the instance out of the load balancer
    (CLAUDE.md S10 graceful degradation). Postgres is different - see the 503 test.
    """
    body = client.get("/readyz").json()
    assert body["postgres"] is True
    assert body["redis"] is False
    assert body["status"] == "degraded"
    assert body["degraded"] is True


def test_readyz_is_200_while_degraded(client: TestClient) -> None:
    """The instance keeps serving: partial capability beats none."""
    assert client.get("/readyz").status_code == 200


def test_readyz_is_503_when_postgres_is_down(client: TestClient, repo: FakeRepository) -> None:
    repo.postgres_ready = False
    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "unready"


# --------------------------------------------------------------------------
# Authentication (D4)
# --------------------------------------------------------------------------


def test_request_without_a_token_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/fleet/summary")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


def test_problem_document_has_the_rfc_7807_shape(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/summary").json()
    for field in ("type", "title", "status", "detail", "correlation_id"):
        assert field in body, f"problem document is missing {field}"
    assert body["type"].startswith("https://")
    assert body["status"] == 401


@pytest.mark.parametrize(
    "header",
    ["", "Basic abc", "Bearer", "bearer", "Token abc", "Bearer  "],
)
def test_malformed_authorization_headers_are_401(client: TestClient, header: str) -> None:
    response = client.get("/api/v1/fleet/summary", headers={"Authorization": header})
    assert response.status_code == 401


def test_tampered_token_is_401(client: TestClient, make_token: Any) -> None:
    tampered = make_token()[:-8] + "AAAAAAAA"
    response = client.get("/api/v1/fleet/summary", headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


def test_expired_token_is_401(client: TestClient, make_token: Any) -> None:
    expired = make_token(ttl_seconds=-7200)
    response = client.get("/api/v1/fleet/summary", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401


def test_token_for_the_wrong_audience_is_401(client: TestClient, make_token: Any) -> None:
    """A token minted for another service must not be accepted here."""
    wrong = make_token(audience="some-other-service")
    response = client.get("/api/v1/fleet/summary", headers={"Authorization": f"Bearer {wrong}"})
    assert response.status_code == 401


def test_token_from_the_wrong_issuer_is_401(client: TestClient, make_token: Any) -> None:
    wrong = make_token(issuer="https://attacker.example")
    response = client.get("/api/v1/fleet/summary", headers={"Authorization": f"Bearer {wrong}"})
    assert response.status_code == 401


def test_token_without_a_tenant_claim_is_refused(client: TestClient, make_token: Any) -> None:
    """Without a tenant the request has no scope, and defaulting one would expose a
    tenant's data to everyone."""
    response = client.get(
        "/api/v1/fleet/summary",
        headers={"Authorization": f"Bearer {make_token(omit=('tenant_id',))}"},
    )
    assert response.status_code == 401


def test_401_detail_does_not_reveal_which_check_failed(client: TestClient, make_token: Any) -> None:
    """Distinguishing a bad signature from a bad audience tells an attacker which half
    of a forgery attempt worked."""
    bodies = {
        client.get(
            "/api/v1/fleet/summary",
            headers={"Authorization": f"Bearer {make_token(audience='wrong')}"},
        ).json()["detail"],
        client.get(
            "/api/v1/fleet/summary",
            headers={"Authorization": f"Bearer {make_token()[:-8] + 'AAAAAAAA'}"},
        ).json()["detail"],
    }
    assert bodies == {"token is not valid"}


# --------------------------------------------------------------------------
# Fleet summary
# --------------------------------------------------------------------------


def test_fleet_summary_returns_kpis(client: TestClient, auth_header: dict[str, str]) -> None:
    body = client.get("/api/v1/fleet/summary", headers=auth_header).json()
    assert body["tenant_id"] == TENANT_A
    assert body["vehicle_count"] == 1000
    assert body["excessive_count"] == 34


def test_money_is_serialised_as_a_string(client: TestClient, auth_header: dict[str, str]) -> None:
    """JSON numbers are IEEE 754 doubles in every JavaScript client, so a float would
    accumulate error as soon as the UI summed anything (D11)."""
    raw = client.get("/api/v1/fleet/summary", headers=auth_header).text
    assert '"total_cost":"3733.5017"' in raw
    body = client.get("/api/v1/fleet/summary", headers=auth_header).json()
    assert isinstance(body["total_cost"], str)
    assert Decimal(body["total_cost"]) == Decimal("3733.5017")


def test_summary_declares_its_assumptions(client: TestClient, auth_header: dict[str, str]) -> None:
    """CLAUDE.md S14: a figure a finance user cannot reproduce is one they will not act on."""
    assumptions = client.get("/api/v1/fleet/summary", headers=auth_header).json()["assumptions"]
    assert assumptions["formula_version"] == "idle-fuel-v1"
    assert assumptions["idle_threshold_seconds"] == 180
    assert assumptions["currency"] == "INR"


def test_summary_flags_a_partial_cost_total(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """BEVs have no fuel cost, so the total covers fewer vehicles than it appears to.
    A total presented as complete when it is not is simply wrong (D9)."""
    body = client.get("/api/v1/fleet/summary", headers=auth_header).json()
    assert body["excluded_bev_count"] == 2
    assert body["cost_total_is_partial"] is True


def test_every_monetary_response_is_labelled_an_estimate(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    for path in (
        "/api/v1/fleet/summary",
        "/api/v1/leaderboard/idle-cost",
        "/api/v1/fleet/trend",
    ):
        assert client.get(path, headers=auth_header).json()["is_estimate"] is True


@pytest.mark.parametrize("days", [0, -5, 400])
def test_out_of_range_window_is_rejected(
    client: TestClient, auth_header: dict[str, str], days: int
) -> None:
    response = client.get(f"/api/v1/fleet/summary?days={days}", headers=auth_header)
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Idle events and pagination
# --------------------------------------------------------------------------


def test_idle_events_returns_a_page_envelope(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    body = client.get("/api/v1/idle-events", headers=auth_header).json()
    assert "items" in body
    assert set(body["page"]) == {"next_cursor", "page_size", "returned", "has_more"}


def test_page_envelope_has_no_total_count(client: TestClient, auth_header: dict[str, str]) -> None:
    """An exact COUNT(*) over a partitioned table of billions of rows is its own scan
    and would be stale immediately. Clients follow next_cursor instead."""
    page = client.get("/api/v1/idle-events", headers=auth_header).json()["page"]
    assert "total" not in page
    assert "total_count" not in page


def test_pagination_walks_without_repeating_rows(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.idle_rows = [
        make_idle_row(idle_event_id=i, started_at=NOW - timedelta(minutes=i)) for i in range(1, 11)
    ]

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(10):
        url = "/api/v1/idle-events?page_size=3"
        if cursor:
            url += f"&cursor={cursor}"
        body = client.get(url, headers=auth_header).json()
        seen.extend(item["idle_event_id"] for item in body["items"])
        cursor = body["page"]["next_cursor"]
        if not cursor:
            break

    assert len(seen) == 10
    assert len(set(seen)) == 10, "a row was returned twice across pages"


def test_malformed_cursor_is_400_not_500(client: TestClient, auth_header: dict[str, str]) -> None:
    """A bad cursor is the client's mistake. Letting it become a 500 would page someone."""
    response = client.get("/api/v1/idle-events?cursor=notacursor", headers=auth_header)
    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.json()["type"].endswith("invalid-cursor")


def test_page_size_is_capped_by_the_api(client: TestClient, auth_header: dict[str, str]) -> None:
    response = client.get("/api/v1/idle-events?page_size=5000", headers=auth_header)
    assert response.status_code == 422


def test_idle_event_carries_its_assumptions(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    item = client.get("/api/v1/idle-events", headers=auth_header).json()["items"][0]
    assert item["assumptions"]["burn_rate_lph"] == 1.5
    assert item["assumptions"]["price_id"] == "inr-diesel-current"
    assert item["is_estimate"] is True


def test_idle_event_exposes_the_measured_cross_check(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    """D2: the device-reported figure beside the estimate, so a wrong configured burn
    rate becomes visible instead of silently producing wrong money."""
    item = client.get("/api/v1/idle-events", headers=auth_header).json()["items"][0]
    assert item["fuel_litres_measured"] == pytest.approx(0.39)


def test_bev_event_reports_an_exclusion_not_a_zero(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """A zero would be indistinguishable from a measured zero (D9)."""
    repo.idle_rows = [make_idle_row(cost=None, bev_excluded=True)]
    item = client.get("/api/v1/idle-events", headers=auth_header).json()["items"][0]
    assert item["cost_est"] is None
    assert item["bev_excluded"] is True


def test_data_quality_signal_is_surfaced(client: TestClient, auth_header: dict[str, str]) -> None:
    """max_gap_seconds tells a user a figure rests on sparse samples (D1)."""
    item = client.get("/api/v1/idle-events", headers=auth_header).json()["items"][0]
    assert item["max_gap_seconds"] == 20.0


def test_excessive_only_filter_applies(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    repo.idle_rows = [
        make_idle_row(idle_event_id=1, excessive=True),
        make_idle_row(idle_event_id=2, excessive=False, started_at=NOW - timedelta(minutes=5)),
    ]
    body = client.get("/api/v1/idle-events?excessive_only=true", headers=auth_header).json()
    assert [i["idle_event_id"] for i in body["items"]] == [1]


def test_vin_filter_must_be_a_full_vin(client: TestClient, auth_header: dict[str, str]) -> None:
    """A partial VIN would turn an indexed equality into a scan."""
    assert client.get("/api/v1/idle-events?vin=ZZAA", headers=auth_header).status_code == 422


# --------------------------------------------------------------------------
# Location masking (PDF S11)
# --------------------------------------------------------------------------


def test_finance_role_receives_coarsened_coordinates(client: TestClient, make_token: Any) -> None:
    """Finance works with cost, not with where a driver physically is."""
    token = make_token(role=Role.FINANCE_MANAGER)
    item = client.get("/api/v1/idle-events", headers={"Authorization": f"Bearer {token}"}).json()[
        "items"
    ][0]
    assert item["location_coarsened"] is True
    assert item["start_lat"] == pytest.approx(round(PRECISE_LAT, 2))
    assert item["start_lat"] != pytest.approx(PRECISE_LAT)


def test_operations_role_receives_precise_coordinates(client: TestClient, make_token: Any) -> None:
    """Operations dispatches vehicles, so precision is part of the job."""
    token = make_token(role=Role.OPS_MANAGER)
    item = client.get("/api/v1/idle-events", headers={"Authorization": f"Bearer {token}"}).json()[
        "items"
    ][0]
    assert item["location_coarsened"] is False
    assert item["start_lat"] == pytest.approx(PRECISE_LAT)


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


def test_another_tenants_vehicle_is_404_not_403(
    client: TestClient, repo: FakeRepository, make_token: Any
) -> None:
    """403 would confirm the VIN exists somewhere, which is itself a disclosure."""
    from api.adapters.repository import VehicleDetail

    vin = "ZZAAAAAAAAAAAAAAA"
    repo.vehicles[vin] = VehicleDetail(
        vin=vin,
        registration="SYN-1",
        tenant_id=TENANT_A,
        fleet_name="Depot 1",
        vehicle_class="medium_truck",
        powertrain="diesel",
        fuel_type="diesel",
        model_year=2022,
        is_active=True,
        burns_fuel=True,
        idle_event_count=3,
        total_idle_hours=1.5,
        total_cost=Decimal("120.0000"),
        currency="INR",
    )

    own = client.get(
        f"/api/v1/vehicles/{vin}",
        headers={"Authorization": f"Bearer {make_token(tenant_id=TENANT_A)}"},
    )
    assert own.status_code == 200

    other = client.get(
        f"/api/v1/vehicles/{vin}",
        headers={"Authorization": f"Bearer {make_token(tenant_id=TENANT_B)}"},
    )
    assert other.status_code == 404


def test_summary_is_scoped_to_the_token_tenant(client: TestClient, make_token: Any) -> None:
    """The tenant comes from the verified token, never a request parameter."""
    body = client.get(
        "/api/v1/fleet/summary",
        headers={"Authorization": f"Bearer {make_token(tenant_id=TENANT_B)}"},
    ).json()
    assert body["tenant_id"] == TENANT_B


def test_tenant_cannot_be_overridden_by_a_query_parameter(
    client: TestClient, make_token: Any
) -> None:
    """An unknown query parameter must not silently re-scope the request."""
    body = client.get(
        f"/api/v1/fleet/summary?tenant_id={TENANT_B}",
        headers={"Authorization": f"Bearer {make_token(tenant_id=TENANT_A)}"},
    ).json()
    assert body["tenant_id"] == TENANT_A


# --------------------------------------------------------------------------
# Audit trail (PDF S11)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected_action"),
    [
        ("/api/v1/fleet/summary", "read_fleet_summary"),
        ("/api/v1/idle-events", "read_idle_events"),
        ("/api/v1/leaderboard/idle-cost", "read_idle_leaderboard"),
        ("/api/v1/fleet/trend", "read_fleet_trend"),
    ],
)
def test_every_data_read_writes_an_audit_entry(
    client: TestClient,
    repo: FakeRepository,
    auth_header: dict[str, str],
    path: str,
    expected_action: str,
) -> None:
    client.get(path, headers=auth_header)
    assert [c["action"] for c in repo.audit_calls] == [expected_action]


def test_audit_entry_identifies_the_actor_and_the_request(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    client.get("/api/v1/fleet/summary", headers=auth_header)
    entry = repo.audit_calls[0]
    assert entry["actor"] == "test-user"
    assert entry["tenant"] == TENANT_A
    assert entry["role"] == Role.FINANCE_MANAGER.value
    assert entry["correlation_id"]


def test_audit_entry_masks_the_client_address(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    """Enough to investigate abuse from a network, not enough to identify a person."""
    client.get("/api/v1/fleet/summary", headers=auth_header)
    masked = repo.audit_calls[0]["client_ip_masked"]
    if masked is not None:
        assert masked.endswith(("/24", "/48"))


def test_unauthenticated_request_writes_no_audit_entry(
    client: TestClient, repo: FakeRepository
) -> None:
    """Nothing was accessed, so there is nothing to record."""
    client.get("/api/v1/fleet/summary")
    assert repo.audit_calls == []


# --------------------------------------------------------------------------
# Correlation ids
# --------------------------------------------------------------------------


def test_every_response_carries_a_correlation_id(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    assert client.get("/api/v1/fleet/summary", headers=auth_header).headers["X-Correlation-Id"]


def test_supplied_correlation_id_is_echoed(client: TestClient, auth_header: dict[str, str]) -> None:
    """So a caller can trace one request across services."""
    supplied = "trace-abcdef123456"
    response = client.get(
        "/api/v1/fleet/summary",
        headers={**auth_header, "X-Correlation-Id": supplied},
    )
    assert response.headers["X-Correlation-Id"] == supplied


def test_error_responses_also_carry_a_correlation_id(client: TestClient) -> None:
    """ "The API returned 401" is unactionable without one."""
    response = client.get("/api/v1/fleet/summary")
    assert response.json()["correlation_id"]
    assert response.headers["X-Correlation-Id"]


def test_response_reports_server_timing(client: TestClient, auth_header: dict[str, str]) -> None:
    timing = client.get("/api/v1/fleet/summary", headers=auth_header).headers["Server-Timing"]
    assert timing.startswith("app;dur=")


# --------------------------------------------------------------------------
# Leaderboard and vehicle detail
# --------------------------------------------------------------------------


def test_leaderboard_is_ranked_and_states_its_exclusion(
    client: TestClient, repo: FakeRepository, auth_header: dict[str, str]
) -> None:
    from api.adapters.repository import LeaderboardRow

    repo.leaderboard = [
        LeaderboardRow(
            vin=f"ZZ{i:015d}",
            registration=f"SYN-{i}",
            vehicle_class="medium_truck",
            powertrain="diesel",
            fleet_name="Depot 1",
            idle_event_count=5,
            excessive_count=2,
            total_idle_hours=3.0 - i,
            total_fuel_litres=7.2,
            total_cost=Decimal(f"{300 - i * 50}.0000"),
            currency="INR",
        )
        for i in range(3)
    ]
    body = client.get("/api/v1/leaderboard/idle-cost", headers=auth_header).json()
    assert [i["rank"] for i in body["items"]] == [1, 2, 3]
    # BEVs cannot be ranked by fuel cost, and the response says so explicitly.
    assert body["excludes_bev"] is True


def test_vehicle_detail_requires_a_full_vin(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    assert client.get("/api/v1/vehicles/SHORT", headers=auth_header).status_code == 422


def test_unknown_vehicle_is_404_with_a_problem_document(
    client: TestClient, auth_header: dict[str, str]
) -> None:
    response = client.get("/api/v1/vehicles/ZZZZZZZZZZZZZZZZZ", headers=auth_header)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


# --------------------------------------------------------------------------
# Contract surface
# --------------------------------------------------------------------------


def test_openapi_document_is_served(client: TestClient) -> None:
    """The OpenAPI document is the contract the web client is generated from."""
    spec = client.get("/openapi.json").json()
    assert spec["info"]["version"]
    for path in (
        "/api/v1/fleet/summary",
        "/api/v1/idle-events",
        "/api/v1/leaderboard/idle-cost",
        "/api/v1/vehicles/{vin}",
    ):
        assert path in spec["paths"], f"{path} is missing from the OpenAPI document"


def test_all_api_routes_are_versioned(client: TestClient) -> None:
    """An unversioned data route could never be changed without breaking clients."""
    spec = client.get("/openapi.json").json()
    unversioned = [
        p for p in spec["paths"] if not p.startswith("/api/v1") and p not in {"/healthz", "/readyz"}
    ]
    assert unversioned == [], f"unversioned routes: {unversioned}"


def test_unknown_route_is_404(client: TestClient) -> None:
    assert client.get("/api/v1/does-not-exist").status_code == 404
