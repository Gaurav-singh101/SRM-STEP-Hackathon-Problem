"""Step definitions for every feature.

One module rather than one per feature: the steps are shared vocabulary, and splitting
them would mean the same "signed in as" step existed several times - which is the
duplication CLAUDE.md S7 exists to prevent.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from behave import given, then, when

from api.adapters.repository import FleetSummary
from api.testing import (
    NOW,
    TENANT_A,
    make_alert_row,
    make_coverage,
    make_idle_row,
    make_leaderboard_row,
    make_utilisation_row,
    make_vehicle_detail,
)
from fleet_common.auth import Role

#: Roles as a business reader names them, so the feature files stay in their language.
#: Defined here rather than in environment.py because behave exec()s both files outside
#: any package, so they cannot import from each other.
ROLES: dict[str, Role] = {
    "fleet finance manager": Role.FINANCE_MANAGER,
    "fleet operations manager": Role.OPS_MANAGER,
    "fleet supervisor": Role.SUPERVISOR,
    "operations analyst": Role.ANALYST,
}

#: Named views, so a feature file says "the idle cost leaderboard" rather than a path.
VIEWS: dict[str, str] = {
    "fleet overview": "/api/v1/fleet/summary",
    "idle cost leaderboard": "/api/v1/leaderboard/idle-cost",
    "idle event list": "/api/v1/idle-events",
    "utilisation view": "/api/v1/utilisation",
    "attention list": "/api/v1/alerts",
    "fleet trend": "/api/v1/fleet/trend",
}


#: A synthetic VIN per row index, so feature files can name vehicles as "vehicle 1".
def _vin(index: int) -> str:
    return f"ZZAAAAAAAAAAAAA{index:02d}"


def _body(context: Any) -> Any:
    assert context.response is not None, "no request has been made yet"
    return context.response.json()


# --------------------------------------------------------------------- given


@given('a {role} signed in to "{tenant}"')
def step_signed_in(context: Any, role: str, tenant: str) -> None:
    assert role in ROLES, f"unknown role {role!r}; known: {sorted(ROLES)}"
    token = context.make_token(tenant_id=tenant, role=ROLES[role])
    context.headers = {"Authorization": f"Bearer {token}"}
    context.tenant = tenant


@given("a fleet where the idle cost is concentrated in a few vehicles")
def step_concentrated_cost(context: Any) -> None:
    """The shape the leaderboard exists for: a long tail plus a handful of offenders."""
    context.repo.leaderboard = [
        make_leaderboard_row(
            vin=_vin(1), total_cost=Decimal("8400.0000"), idle_event_count=41, excessive_count=33
        ),
        make_leaderboard_row(
            vin=_vin(2), total_cost=Decimal("6150.0000"), idle_event_count=30, excessive_count=24
        ),
        make_leaderboard_row(
            vin=_vin(3), total_cost=Decimal("310.0000"), idle_event_count=4, excessive_count=0
        ),
        make_leaderboard_row(
            vin=_vin(4), total_cost=Decimal("95.0000"), idle_event_count=2, excessive_count=0
        ),
    ]


@given("the fleet has {count:d} battery-electric vehicles with no fuel cost")
def step_bev_present(context: Any, count: int) -> None:
    """BEVs idle but burn no fuel, so a fuel-cost total covers fewer vehicles than the fleet."""
    context.repo.summary = FleetSummary(
        tenant_id=TENANT_A,
        window_from=NOW - timedelta(days=30),
        window_to=NOW,
        vehicle_count=120,
        vehicles_with_idle=57,
        idle_event_count=127,
        excessive_count=34,
        total_idle_hours=23.09,
        total_fuel_litres=40.07,
        total_cost=Decimal("3733.5017"),
        currency="INR",
        excluded_bev_count=count,
        formula_version="idle-fuel-v1",
        idle_threshold_seconds=180,
        open_alert_count=32,
    )


@given("an idle event of {minutes:d} minutes that cost {cost}")
def step_one_idle_event(context: Any, minutes: int, cost: str) -> None:
    context.repo.idle_rows = [
        make_idle_row(duration_seconds=minutes * 60.0, cost=Decimal(cost), excessive=minutes >= 10)
    ]


@given("the utilisation scores are")
def step_utilisation_table(context: Any) -> None:
    rows = []
    for index, row in enumerate(context.table, start=1):
        rows.append(
            make_utilisation_row(
                vin=_vin(index),
                score=float(row["score"]),
                band=row["band"],
                active_day_component=float(row.get("active_days", 0.5)),
                moving_hours_component=float(row.get("moving", 0.5)),
                distance_component=float(row.get("distance", 0.5)),
                abnormal_reason=row.get("reason") or None,
            )
        )
    context.repo.utilisation_rows = rows


@given(
    "{scored:d} of {active:d} active vehicles were scored, {no_telemetry:d} having no "
    "telemetry and {too_small:d} in a class too small"
)
def step_coverage(
    context: Any, scored: int, active: int, no_telemetry: int, too_small: int
) -> None:
    context.repo.coverage = make_coverage(
        vehicles_active=active,
        scored=scored,
        unscored_no_telemetry=no_telemetry,
        unscored_class_too_small=too_small,
    )


@given("no utilisation window has been scored yet")
def step_no_coverage(context: Any) -> None:
    context.repo.coverage = None
    context.repo.utilisation_rows = []


@given("an alert raised from live telemetry {seconds:g} seconds after the event")
def step_live_alert(context: Any, seconds: float) -> None:
    context.repo.alert_rows = [make_alert_row(ingest_lag_seconds=1.0, processing_seconds=seconds)]


@given("an alert raised from a backfill of {hours:d} hours of history")
def step_backfill_alert(context: Any, hours: int) -> None:
    """The case that produced an 83,711-second figure once, which measured data age."""
    context.repo.alert_rows = [
        make_alert_row(ingest_lag_seconds=hours * 3600.0, processing_seconds=2.4)
    ]


@given("the attention list holds")
def step_alert_table(context: Any) -> None:
    rows = []
    for index, row in enumerate(context.table, start=1):
        rows.append(
            make_alert_row(
                alert_id=index,
                vin=_vin(index),
                alert_type=row["type"],
                severity=row["severity"],
                acknowledged=row["acknowledged"].strip().lower() == "yes",
            )
        )
    context.repo.alert_rows = rows


@given('a vehicle "{vin}" belonging to "{tenant}"')
def step_vehicle_in_tenant(context: Any, vin: str, tenant: str) -> None:
    context.repo.vehicles[vin] = make_vehicle_detail(vin=vin, tenant_id=tenant)
    context.subject_vin = vin


@given("an idle event recorded at a precise location")
def step_precise_location(context: Any) -> None:
    context.repo.idle_rows = [make_idle_row()]


# ---------------------------------------------------------------------- when


@when("they open the {view}")
def step_open_view(context: Any, view: str) -> None:
    assert view in VIEWS, f"unknown view {view!r}; known: {sorted(VIEWS)}"
    context.response = context.client.get(VIEWS[view], headers=context.headers)


@when('they filter the utilisation view to the "{band}" band')
def step_open_view_filtered(context: Any, band: str) -> None:
    # A literal step rather than one parameterised on the view: behave's default parser
    # would otherwise see this as ambiguous against "they open the {view}".
    context.response = context.client.get(
        f"{VIEWS['utilisation view']}?band={band}", headers=context.headers
    )


@when("they review the attention list including handled alerts")
def step_open_all_alerts(context: Any) -> None:
    context.response = context.client.get(
        "/api/v1/alerts?unacknowledged_only=false", headers=context.headers
    )


@when('they look up vehicle "{vin}"')
def step_look_up_vehicle(context: Any, vin: str) -> None:
    context.response = context.client.get(f"/api/v1/vehicles/{vin}", headers=context.headers)


@when("they request the view without signing in")
def step_no_token(context: Any) -> None:
    context.response = context.client.get("/api/v1/fleet/summary")


# ---------------------------------------------------------------------- then


@then("the request succeeds")
def step_ok(context: Any) -> None:
    assert context.response.status_code == 200, (
        f"expected 200, got {context.response.status_code}: {context.response.text}"
    )


@then("the request is refused as unauthenticated")
def step_unauthenticated(context: Any) -> None:
    assert context.response.status_code == 401
    assert context.response.headers["content-type"].startswith("application/problem+json")


@then("the vehicle is reported as not found")
def step_not_found(context: Any) -> None:
    """404, never 403: a forbidden response would confirm the VIN exists somewhere."""
    assert context.response.status_code == 404, context.response.text


@then("the worst offender is listed first")
def step_worst_first(context: Any) -> None:
    items = _body(context)["items"]
    costs = [Decimal(item["total_cost"]) for item in items]
    assert costs == sorted(costs, reverse=True), costs
    assert items[0]["rank"] == 1


@then("every vehicle carries its own estimated cost")
def step_every_cost(context: Any) -> None:
    for item in _body(context)["items"]:
        assert item["total_cost"] is not None
        assert item["currency"]


@then("the list is marked as an estimate")
def step_marked_estimate(context: Any) -> None:
    """A figure derived from a burn-rate assumption must never read as a measurement."""
    assert _body(context)["is_estimate"] is True


@then("the total cost is {amount}")
def step_total_cost(context: Any, amount: str) -> None:
    assert Decimal(_body(context)["total_cost"]) == Decimal(amount)


@then("the cost is exact to the last paisa")
def step_money_exact(context: Any) -> None:
    """Serialised as a string, because a JSON number is an IEEE 754 double in a browser.

    Both halves matter. A JSON number would already have lost precision before the
    browser saw it, and a string rounded to two places server-side would lose it just as
    surely - so the scale has to survive the wire as well as the type.
    """
    raw = _body(context)["total_cost"]
    assert isinstance(raw, str), f"expected a string, got {type(raw).__name__}"
    assert "." in raw, f"{raw!r} carries no fractional part"
    places = len(raw.split(".")[1])
    assert places >= 4, f"{raw!r} kept only {places} decimal place(s); rounding happened"


@then("the total is flagged as covering only part of the fleet")
def step_partial_total(context: Any) -> None:
    body = _body(context)
    assert body["cost_total_is_partial"] is True
    assert body["excluded_bev_count"] > 0


@then("the figure states the formula, threshold and fuel price behind it")
def step_assumptions(context: Any) -> None:
    """CLAUDE.md S14: a figure a finance user cannot reproduce is one they will not act on."""
    assumptions = _body(context)["assumptions"]
    assert assumptions["formula_version"]
    assert assumptions["idle_threshold_seconds"] > 0
    assert assumptions["currency"]


@then("the under-used vehicles appear before the healthy ones")
def step_worst_utilisation_first(context: Any) -> None:
    scores = [item["score"] for item in _body(context)["items"]]
    assert scores == sorted(scores), scores


@then('only vehicles in the "{band}" band are listed')
def step_only_band(context: Any, band: str) -> None:
    bands = {item["band"] for item in _body(context)["items"]}
    assert bands == {band}, bands


@then("each vehicle shows the three components behind its score")
def step_components(context: Any) -> None:
    """A manager deciding whether to sell a van needs to see why it scored 24."""
    for item in _body(context)["items"]:
        assert item["active_day_component"] is not None
        assert item["moving_hours_component"] is not None
        assert item["distance_component"] is not None


@then("the abnormal vehicle explains itself in words")
def step_abnormal_reason(context: Any) -> None:
    abnormal = [i for i in _body(context)["items"] if i["band"] == "abnormal"]
    assert abnormal, "no abnormal vehicle in the response"
    for item in abnormal:
        assert item["abnormal_reason"], "an abnormal band with no reason is not actionable"


@then("the view reports that {scored:d} of {active:d} vehicles were scored")
def step_coverage_reported(context: Any, scored: int, active: int) -> None:
    coverage = _body(context)["coverage"]
    assert coverage is not None, "coverage must be reported, or the table reads as the fleet"
    assert coverage["scored"] == scored
    assert coverage["vehicles_active"] == active


@then("vehicles with no telemetry are counted separately from unused ones")
def step_no_telemetry_separate(context: Any) -> None:
    """ "We have no data" and "this asset is idle" lead to opposite actions."""
    coverage = _body(context)["coverage"]
    assert coverage["unscored_no_telemetry"] > 0
    assert coverage["unscored_no_telemetry"] != coverage["unscored_class_too_small"]


@then("the view does not claim full coverage")
def step_no_false_coverage(context: Any) -> None:
    assert _body(context)["coverage"] is None


@then("the reaction time is reported as {seconds:g} seconds")
def step_processing_latency(context: Any, seconds: float) -> None:
    assert _body(context)["items"][0]["processing_latency_seconds"] == seconds


@then("the alert is flagged as coming from replayed history")
def step_backfill_flagged(context: Any) -> None:
    body = _body(context)
    assert body["items"][0]["is_backfill"] is True
    assert body["contains_backfill"] is True


@then("the data age is not presented as the reaction time")
def step_age_distinct(context: Any) -> None:
    """One run reported 83,711 s as an alert latency; it was the age of the replayed data."""
    alert = _body(context)["items"][0]
    assert alert["end_to_end_latency_seconds"] > alert["processing_latency_seconds"]
    assert _body(context)["max_processing_latency_seconds"] == alert["processing_latency_seconds"]


@then("the alert is not flagged as replayed")
def step_not_backfill(context: Any) -> None:
    assert _body(context)["items"][0]["is_backfill"] is False
    assert _body(context)["contains_backfill"] is False


@then("{count:d} alerts need attention")
def step_alert_count(context: Any, count: int) -> None:
    assert len(_body(context)["items"]) == count


@then("each alert carries the detail needed to act on it")
def step_alert_detail(context: Any) -> None:
    for item in _body(context)["items"]:
        assert item["vin"]
        assert item["registration"]
        assert item["alert_type"]
        assert item["detail"]


@then("the trend has one point per day")
def step_trend_points(context: Any) -> None:
    items = _body(context)["items"]
    assert items, "an empty trend cannot show a direction"
    days = [item["day"] for item in items]
    assert len(days) == len(set(days)), "a repeated day would double-count"


@then("every point carries both idle hours and cost")
def step_trend_values(context: Any) -> None:
    for item in _body(context)["items"]:
        assert item["idle_hours"] is not None
        assert item["cost"] is not None


@then("the location is coarsened")
def step_location_coarsened(context: Any) -> None:
    """A finance user needs the cost, not the street a driver parked on (CLAUDE.md S11)."""
    event = _body(context)["items"][0]
    assert event["location_coarsened"] is True
    assert round(event["start_lat"], 2) == event["start_lat"]


@then("the location is precise")
def step_location_precise(context: Any) -> None:
    event = _body(context)["items"][0]
    assert event["location_coarsened"] is False
    assert round(event["start_lat"], 2) != event["start_lat"]


@then("the access is recorded in the audit trail")
def step_audited(context: Any) -> None:
    calls = context.repo.audit_calls
    assert calls, "a read of vehicle, location or driver data must leave an audit entry"
    entry = calls[-1]
    assert entry["tenant"] == context.tenant
    assert entry["action"]
    assert entry["correlation_id"]
