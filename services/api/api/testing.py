"""Test doubles and helpers for the API.

Shipped inside the package rather than living in a conftest, so test modules can import
them directly. The fake repository mirrors ``FleetRepository``'s surface: these tests are
about the HTTP contract - status codes, problem documents, serialisation, masking,
pagination envelopes - and a real database would only re-test the schema, which the
integration suite already covers.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from api.adapters.repository import (
    AlertRow,
    FleetSummary,
    IdleEventRow,
    Leaderboard,
    LeaderboardRow,
    UtilisationRow,
    UtilisationSnapshot,
    UtilisationWindowRow,
    VehicleDetail,
)
from api.deps import Authenticator, RateLimiter
from api.main import create_app, get_repository
from fleet_common.auth import Principal, Role, TokenVerifier
from fleet_common.errors import NotFoundError
from fleet_common.pagination import Cursor, Page, build_page
from fleet_common.settings import api_settings, auth_settings

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-bravo"
NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=UTC)
NOW_DAY = NOW.date()

#: Precise coordinates, so masking is observable in the response.
PRECISE_LAT = 12.971598
PRECISE_LON = 77.594562


class _StaticJwks:
    """A JWKS source backed by the test key, so verification runs for real."""

    def __init__(self, key: rsa.RSAPrivateKey) -> None:
        self._public = key.public_key()

    def client(self) -> Any:
        public = self._public

        class _Client:
            @staticmethod
            def get_signing_key_from_jwt(_token: str) -> Any:
                @dataclass
                class _Key:
                    key: Any

                return _Key(key=public)

        return _Client()

    def invalidate(self) -> None:
        return None


def make_idle_row(
    *,
    idle_event_id: int = 1,
    vin: str = "ZZAAAAAAAAAAAAAAA",
    duration_seconds: float = 900.0,
    cost: Decimal | None = Decimal("34.6875"),
    excessive: bool = True,
    bev_excluded: bool = False,
    started_at: datetime | None = None,
) -> IdleEventRow:
    start = started_at or NOW
    return IdleEventRow(
        idle_event_id=idle_event_id,
        vin=vin,
        started_at=start,
        ended_at=start + timedelta(seconds=duration_seconds),
        duration_seconds=duration_seconds,
        sample_count=90,
        max_gap_seconds=20.0,
        excessive=excessive,
        pto_active_any=False,
        start_lat=PRECISE_LAT,
        start_lon=PRECISE_LON,
        fuel_litres_est=0.375 if cost is not None else None,
        cost_est=cost,
        currency="INR" if cost is not None else None,
        formula_version="idle-fuel-v1" if cost is not None else "unpriced",
        burn_rate_lph=1.5 if cost is not None else None,
        price_id="inr-diesel-current" if cost is not None else None,
        idle_threshold_seconds=180,
        fuel_litres_measured=0.39 if cost is not None else None,
        bev_excluded=bev_excluded,
    )


def make_leaderboard_row(
    *,
    vin: str = "ZZAAAAAAAAAAAAAAA",
    registration: str = "KA01AA0001",
    total_cost: Decimal = Decimal("1250.5000"),
    idle_event_count: int = 9,
    excessive_count: int = 4,
    total_idle_hours: float = 3.5,
    powertrain: str = "ICE",
) -> LeaderboardRow:
    return LeaderboardRow(
        vin=vin,
        registration=registration,
        vehicle_class="LCV",
        powertrain=powertrain,
        fleet_name="Bengaluru South",
        idle_event_count=idle_event_count,
        excessive_count=excessive_count,
        total_idle_hours=total_idle_hours,
        total_fuel_litres=5.25,
        total_cost=total_cost,
        currency="INR",
    )


def make_utilisation_row(
    *,
    vin: str = "ZZAAAAAAAAAAAAAAA",
    score: float = 18.4,
    band: str = "low",
    active_day_component: float = 0.20,
    moving_hours_component: float = 0.18,
    distance_component: float = 0.22,
    abnormal_reason: str | None = None,
    baseline_sample_size: int = 240,
) -> UtilisationRow:
    components = (active_day_component, moving_hours_component, distance_component)
    return UtilisationRow(
        vin=vin,
        registration="KA01AA0001",
        vehicle_class="LCV",
        powertrain="ICE",
        fleet_name="Bengaluru South",
        window_end=NOW.date(),
        window_days=30,
        score=score,
        band=band,
        active_day_component=active_day_component,
        moving_hours_component=moving_hours_component,
        distance_component=distance_component,
        idle_share=0.31,
        component_spread=max(components) - min(components),
        abnormal_reason=abnormal_reason,
        baseline_sample_size=baseline_sample_size,
        formula_version="utilisation-v1",
    )


def make_alert_row(
    *,
    alert_id: int = 1,
    vin: str = "ZZAAAAAAAAAAAAAAA",
    alert_type: str = "excessive_idle",
    severity: str = "warning",
    triggering_event_ts: datetime | None = None,
    ingest_lag_seconds: float = 1.0,
    processing_seconds: float = 0.8,
    acknowledged: bool = False,
) -> AlertRow:
    """An alert whose two latency figures can be set independently.

    Separately, because that is the distinction the view exists to preserve: a replayed
    day has a huge end-to-end figure and a small processing figure, and quoting the first
    as a reaction time is the mistake this was written to prevent.
    """
    event_ts = triggering_event_ts or NOW
    ingest_ts = event_ts + timedelta(seconds=ingest_lag_seconds)
    return AlertRow(
        alert_id=alert_id,
        vin=vin,
        registration="KA01AA0001",
        alert_type=alert_type,
        severity=severity,
        triggering_event_ts=event_ts,
        ingest_ts=ingest_ts,
        persisted_at=ingest_ts + timedelta(seconds=processing_seconds),
        idle_event_started_at=event_ts - timedelta(seconds=600),
        detail={"duration_seconds": 780.0, "threshold_seconds": 600},
        acknowledged_at=NOW if acknowledged else None,
    )


def make_utilisation_snapshot(
    *,
    score: float = 18.4,
    band: str = "low",
    abnormal_reason: str | None = None,
    baseline_sample_size: int = 240,
) -> UtilisationSnapshot:
    return UtilisationSnapshot(
        window_end=NOW.date(),
        window_days=30,
        score=score,
        band=band,
        active_day_component=0.20,
        moving_hours_component=0.18,
        distance_component=0.22,
        idle_share=0.31,
        abnormal_reason=abnormal_reason,
        baseline_sample_size=baseline_sample_size,
        formula_version="utilisation-v1",
    )


def make_vehicle_detail(
    *,
    vin: str = "ZZAAAAAAAAAAAAAAA",
    tenant_id: str = TENANT_A,
    powertrain: str = "ICE",
    burns_fuel: bool = True,
    total_cost: Decimal | None = Decimal("1250.5000"),
    utilisation: UtilisationSnapshot | None = None,
) -> VehicleDetail:
    return VehicleDetail(
        vin=vin,
        registration="KA01AA0001",
        tenant_id=tenant_id,
        fleet_name="Bengaluru South",
        vehicle_class="LCV",
        powertrain=powertrain,
        fuel_type="diesel" if burns_fuel else None,
        model_year=2022,
        is_active=True,
        burns_fuel=burns_fuel,
        idle_event_count=9,
        total_idle_hours=3.5,
        total_cost=total_cost,
        currency="INR" if total_cost is not None else None,
        utilisation=utilisation,
    )


def make_coverage(
    *,
    vehicles_active: int = 2000,
    scored: int = 180,
    unscored_no_telemetry: int = 1808,
    unscored_class_too_small: int = 12,
) -> UtilisationWindowRow:
    return UtilisationWindowRow(
        window_end=NOW.date(),
        window_days=30,
        vehicles_active=vehicles_active,
        scored=scored,
        unscored_no_telemetry=unscored_no_telemetry,
        unscored_class_too_small=unscored_class_too_small,
        formula_version="utilisation-v1",
    )


@dataclass
class FakeRepository:
    """In-memory stand-in with the same surface as ``FleetRepository``.

    Records the audit calls it receives, so tests can assert an audit entry was written
    for every access without needing the table (PDF S11).
    """

    idle_rows: list[IdleEventRow] = field(default_factory=list)
    vehicles: dict[str, VehicleDetail] = field(default_factory=dict)
    leaderboard: list[LeaderboardRow] = field(default_factory=list)
    #: Rollup freshness. Defaults to the fixture's "now" so tests that do not care about
    #: staleness still get a populated, plausible pair.
    leaderboard_data_through: date | None = NOW_DAY
    leaderboard_computed_at: datetime | None = NOW
    summary: FleetSummary | None = None
    utilisation_rows: list[UtilisationRow] = field(default_factory=list)
    alert_rows: list[AlertRow] = field(default_factory=list)
    coverage: UtilisationWindowRow | None = None
    audit_calls: list[dict[str, Any]] = field(default_factory=list)
    postgres_ready: bool = True
    #: Set to coarsen coordinates, mirroring what the real repository does per role.
    coarsen: bool = False

    def check_ready(self) -> bool:
        return self.postgres_ready

    def record_access(
        self,
        principal: Principal,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None,
        correlation_id: str,
        client_ip_masked: str | None,
    ) -> None:
        self.audit_calls.append(
            {
                "actor": principal.subject,
                "tenant": principal.tenant_id,
                "role": principal.role.value,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "correlation_id": correlation_id,
                "client_ip_masked": client_ip_masked,
            }
        )

    def list_idle_events(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: Cursor | None = None,
        vin: str | None = None,
        excessive_only: bool = False,
        since: datetime | None = None,
    ) -> Page[IdleEventRow]:
        from fleet_common.auth import coarsen_location

        rows = list(self.idle_rows)
        if vin:
            rows = [r for r in rows if r.vin == vin]
        if excessive_only:
            rows = [r for r in rows if r.excessive]
        if cursor is not None:
            anchor = (cursor.sort_datetime, cursor.row_id)
            rows = [r for r in rows if (r.started_at, r.idle_event_id) < anchor]

        if not principal.may_see_precise_location:
            # dataclasses.replace, not __dict__: IdleEventRow uses slots=True and so
            # has no instance dict to copy.
            masked = []
            for row in rows:
                lat, lon = coarsen_location(row.start_lat, row.start_lon, precise=False)
                masked.append(replace(row, start_lat=lat, start_lon=lon))
            rows = masked

        return build_page(
            rows[: page_size + 1],
            page_size=page_size,
            cursor_of=lambda r: Cursor.from_row(r.started_at, r.idle_event_id),
        )

    def fleet_summary(
        self, principal: Principal, *, since: datetime, until: datetime
    ) -> FleetSummary:
        if self.summary is not None:
            return self.summary
        return FleetSummary(
            tenant_id=principal.tenant_id,
            window_from=since,
            window_to=until,
            vehicle_count=1000,
            vehicles_with_idle=57,
            idle_event_count=127,
            excessive_count=34,
            total_idle_hours=23.09,
            total_fuel_litres=40.07,
            total_cost=Decimal("3733.5017"),
            currency="INR",
            excluded_bev_count=2,
            formula_version="idle-fuel-v1",
            idle_threshold_seconds=180,
            open_alert_count=32,
        )

    def idle_cost_leaderboard(
        self, principal: Principal, *, since: datetime, limit: int
    ) -> Leaderboard:
        ranked = sorted(self.leaderboard, key=lambda r: r.total_cost, reverse=True)
        return Leaderboard(
            items=ranked[:limit],
            data_through=self.leaderboard_data_through,
            computed_at=self.leaderboard_computed_at,
        )

    def vehicle_detail(self, principal: Principal, vin: str) -> VehicleDetail:
        detail = self.vehicles.get(vin)
        if detail is None or detail.tenant_id != principal.tenant_id:
            # Not-found rather than forbidden for another tenant's vehicle: a 403 would
            # confirm the VIN exists somewhere.
            raise NotFoundError(f"no vehicle {vin!r} in this tenant")
        return detail

    def daily_idle_trend(
        self, principal: Principal, *, since: Any, until: Any
    ) -> list[tuple[Any, float, Decimal]]:
        return [(NOW.date(), 5.5, Decimal("512.2500"))]

    def list_utilisation(
        self,
        principal: Principal,
        *,
        page_size: int,
        cursor: Cursor | None = None,
        bands: tuple[str, ...] = (),
    ) -> Page[UtilisationRow]:
        # Worst first, mirroring the real query's ORDER BY score ASC: the point of the
        # view is the vehicles at the bottom, so they belong on the first page.
        rows = sorted(self.utilisation_rows, key=lambda r: (r.score, r.vin))
        if bands:
            rows = [r for r in rows if r.band in bands]
        if cursor is not None:
            anchor = (float(cursor.sort_value), cursor.tie_break)
            rows = [r for r in rows if (r.score, r.vin) > anchor]
        return build_page(
            rows[: page_size + 1],
            page_size=page_size,
            cursor_of=lambda r: Cursor.with_tie_break(str(r.score), r.vin),
        )

    def utilisation_coverage(self, principal: Principal) -> UtilisationWindowRow | None:
        return self.coverage

    def list_alerts(
        self, principal: Principal, *, limit: int, unacknowledged_only: bool = True
    ) -> list[AlertRow]:
        rows = list(self.alert_rows)
        if unacknowledged_only:
            rows = [r for r in rows if r.acknowledged_at is None]
        rows.sort(key=lambda r: r.triggering_event_ts, reverse=True)
        return rows[:limit]


def make_signing_key() -> rsa.RSAPrivateKey:
    """Generate a test signing key.

    A real key pair, not a stub: authentication is the one thing worth exercising for
    real, because a mocked verifier would pass even with a broken algorithm
    allow-list - which is exactly the failure that matters (D4).
    """
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token_factory(signing_key: rsa.RSAPrivateKey) -> Any:
    """Return a callable that mints genuinely RS256-signed tokens."""
    auth = auth_settings()

    def _make(
        *,
        tenant_id: str = TENANT_A,
        role: Role = Role.FINANCE_MANAGER,
        subject: str = "test-user",
        ttl_seconds: int = 3600,
        audience: str | None = None,
        issuer: str | None = None,
        omit: tuple[str, ...] = (),
        algorithm: str = "RS256",
    ) -> str:
        issued = int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "iss": issuer or auth.oidc_issuer,
            "aud": audience or auth.oidc_audience,
            "iat": issued,
            "nbf": issued,
            "exp": issued + ttl_seconds,
            "jti": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "role": role.value,
        }
        for claim in omit:
            claims.pop(claim, None)
        return jwt.encode(claims, signing_key, algorithm=algorithm, headers={"kid": "test-key"})

    return _make


def build_test_client(repo: FakeRepository, signing_key: rsa.RSAPrivateKey) -> Iterator[TestClient]:
    """A TestClient wired to the fake repository and a real token verifier."""
    app = create_app()

    verifier = TokenVerifier(auth_settings(), jwks=_StaticJwks(signing_key))  # type: ignore[arg-type]
    authenticator = Authenticator(
        verifier,
        # Rate limiting off by default: it has its own tests, and an accidental limit
        # would make every other test order-dependent.
        RateLimiter(None, limit_per_minute=0),
        api_settings(),
    )

    app.dependency_overrides[get_repository] = lambda: repo

    with TestClient(app, raise_server_exceptions=False) as test_client:
        # Replace the state the lifespan created, so no real pool is used.
        app.state.repository = repo
        app.state.redis = None
        app.state.authenticator = authenticator
        yield test_client

    app.dependency_overrides.clear()
