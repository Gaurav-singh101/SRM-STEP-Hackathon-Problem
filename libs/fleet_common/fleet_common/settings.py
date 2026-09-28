"""Configuration, from the environment only (CLAUDE.md S7, 12-factor).

One settings class per concern, composed by each service. No business constant
lives in code: idle thresholds, burn-rate versions, prices and weights all arrive
here so a figure can be traced to the configuration that produced it.

Nothing in this module has a usable default for a secret. ``POSTGRES_PASSWORD``
has no default at all, so a service fails at startup rather than silently
connecting somewhere unintended.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Base(BaseSettings):
    """Shared loading rules: environment first, then a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )


class AppSettings(_Base):
    """Service identity and logging."""

    app_env: Literal["local", "ci", "dev", "staging", "prod"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    service_name: str = "fleet-service"


class PostgresSettings(_Base):
    """Relational core connection (CLAUDE.md S8)."""

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "fleet"
    postgres_user: str = "fleet"
    # No default: a missing password must fail loudly at startup, not fall back
    # to something that might connect to the wrong database.
    postgres_password: SecretStr
    postgres_pool_min: int = 2
    postgres_pool_max: int = 16
    # Every external call gets a timeout (CLAUDE.md S7). Without one, a slow query
    # holds a connection until the pool is exhausted and the API stops serving.
    postgres_statement_timeout_ms: int = 5_000

    # Deliberately a plain property, NOT a computed_field. A computed_field is
    # included in repr() and model_dump(), which would put the password into every
    # log line and traceback that touched this object - defeating SecretStr
    # entirely. A test asserts this stays true (CLAUDE.md S3: never log secrets).
    @property
    def dsn(self) -> str:
        """libpq connection string. Carries the password: never log this."""
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} "
            f"password={self.postgres_password.get_secret_value()}"
        )

    @property
    def safe_dsn(self) -> str:
        """The same target with the password removed. Safe for logs and errors."""
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user}"
        )


class KafkaSettings(_Base):
    """Broker and topic configuration (ADR-0004, ADR-0005)."""

    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_telemetry: str = "telemetry.raw"
    kafka_topic_dlq: str = "telemetry.dlq"
    kafka_topic_late: str = "telemetry.late"
    kafka_topic_idle_events: str = "idle.events"
    kafka_topic_alerts: str = "alerts"
    kafka_consumer_group: str = "processor"
    kafka_num_partitions: int = 24
    kafka_replication_factor: int = 1
    # Batch size in the hot path. Per-event round trips are forbidden
    # (CLAUDE.md S3), so this is the unit of work everywhere downstream.
    kafka_batch_max_messages: int = 5_000
    kafka_poll_timeout_seconds: float = 1.0


class MongoSettings(_Base):
    """Raw telemetry store, hot tier with TTL (CLAUDE.md S8)."""

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "fleet_telemetry"
    mongo_raw_collection: str = "telemetry_raw"
    # 48h default. At the PDF's 1 KB/event model, 100K events/s is ~8.6 TB/day, so
    # a week-long hot tier is not affordable. Cold data moves to Parquet (D6).
    mongo_raw_ttl_seconds: int = 172_800


class RedisSettings(_Base):
    """Hot state: latest vehicle state, dedup window, response cache.

    Pinned to Redis 7.2.x for licence reasons (ADR-0002). No Redis modules.
    """

    redis_url: str = "redis://localhost:6379/0"
    redis_state_ttl_seconds: int = 86_400


class AuthSettings(_Base):
    """Generic OIDC/JWT validation (D4). No vendor appears here by design."""

    oidc_issuer: str = "http://localhost:8081"
    oidc_audience: str = "fleet-api"
    oidc_jwks_url: str = "http://localhost:8081/jwks.json"
    # Allow-list, never a blocklist. Accepting 'none' or HS256 from a JWKS issuer
    # is the classic algorithm-confusion bypass: an attacker signs a token with
    # the public key as an HMAC secret and it verifies.
    oidc_allowed_algorithms: str = "RS256,ES256"
    oidc_jwks_cache_seconds: int = 300

    @property
    def allowed_algorithms(self) -> list[str]:
        return [a.strip() for a in self.oidc_allowed_algorithms.split(",") if a.strip()]

    @field_validator("oidc_allowed_algorithms")
    @classmethod
    def _reject_unsafe_algorithms(cls, v: str) -> str:
        """Refuse a configuration that would accept an unsigned or symmetric token."""
        requested = {a.strip().upper() for a in v.split(",") if a.strip()}
        unsafe = requested & {"NONE", "HS256", "HS384", "HS512"}
        if unsafe:
            raise ValueError(
                f"refusing unsafe JWT algorithms {sorted(unsafe)}: 'none' disables "
                "verification, and HMAC algorithms against a JWKS issuer enable "
                "algorithm-confusion attacks"
            )
        if not requested:
            raise ValueError("at least one JWT algorithm must be allowed")
        return v


class IdleSettings(_Base):
    """Idle-detection parameters (D1, ADR-0001). Values are PROPOSED."""

    idle_min_seconds: int = 180
    idle_entry_speed_kph: float = 2.0
    idle_exit_speed_kph: float = 5.0
    idle_excessive_seconds: int = 600
    idle_gap_tolerance_seconds: int = 30
    idle_exclude_depot: bool = False
    idle_exclude_pto_active: bool = False


class CostSettings(_Base):
    """Fuel and cost parameters (D2, D11, ADR-0002)."""

    fuel_formula_version: str = "idle-fuel-v1"
    default_currency: str = "INR"

    @field_validator("default_currency")
    @classmethod
    def _iso_4217(cls, v: str) -> str:
        if len(v) != 3 or not v.isalpha():
            raise ValueError("default_currency must be a 3-letter ISO-4217 code")
        return v.upper()


class UtilisationSettings(_Base):
    """Utilisation parameters (D3, ADR-0003). Values are PROPOSED."""

    utilisation_formula_version: str = "utilisation-v1"
    util_weight_active_day: float = 0.40
    util_weight_moving_hours: float = 0.35
    util_weight_distance: float = 0.25
    util_low_threshold: float = 40.0
    util_normal_upper: float = 85.0
    util_abnormal_z: float = 3.0


class StreamSettings(_Base):
    """Event-time handling (CLAUDE.md S8)."""

    # Events later than this go to the late topic and are reconciled by the batch
    # job. They are never silently dropped.
    allowed_lateness_seconds: int = 300
    dedup_window_seconds: int = 600


class ApiSettings(_Base):
    """HTTP layer (CLAUDE.md S10)."""

    api_host: str = "0.0.0.0"  # noqa: S104 - binds inside a container, not on a host
    api_port: int = 8000
    api_page_size_default: int = 50
    # Hard maximum: an unbounded page size is a denial-of-service vector and
    # defeats keyset pagination entirely.
    api_page_size_max: int = 200
    api_rate_limit_per_minute: int = 600
    api_cors_origins: str = "http://localhost:5173"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]


class SimulatorSettings(_Base):
    """Synthetic generator (PDF S8: the simulator is part of the solution)."""

    sim_vehicle_count: int = 2_000
    # Seeded so a run is reproducible; an unseeded simulator makes every
    # measurement unrepeatable (CLAUDE.md S9).
    sim_seed: int = 20260929
    sim_events_per_vehicle_seconds: int = 10
    sim_duplicate_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    sim_out_of_order_rate: float = Field(default=0.02, ge=0.0, le=1.0)
    sim_malformed_rate: float = Field(default=0.005, ge=0.0, le=1.0)


# --------------------------------------------------------------------------
# Cached accessors. Settings are immutable, so one instance per process is
# correct and avoids re-reading .env on every call.
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def app_settings() -> AppSettings:
    return AppSettings()


@lru_cache(maxsize=1)
def postgres_settings() -> PostgresSettings:
    return PostgresSettings()  # type: ignore[call-arg]


@lru_cache(maxsize=1)
def kafka_settings() -> KafkaSettings:
    return KafkaSettings()


@lru_cache(maxsize=1)
def mongo_settings() -> MongoSettings:
    return MongoSettings()


@lru_cache(maxsize=1)
def redis_settings() -> RedisSettings:
    return RedisSettings()


@lru_cache(maxsize=1)
def auth_settings() -> AuthSettings:
    return AuthSettings()


@lru_cache(maxsize=1)
def idle_settings() -> IdleSettings:
    return IdleSettings()


@lru_cache(maxsize=1)
def cost_settings() -> CostSettings:
    return CostSettings()


@lru_cache(maxsize=1)
def utilisation_settings() -> UtilisationSettings:
    return UtilisationSettings()


@lru_cache(maxsize=1)
def stream_settings() -> StreamSettings:
    return StreamSettings()


@lru_cache(maxsize=1)
def api_settings() -> ApiSettings:
    return ApiSettings()


@lru_cache(maxsize=1)
def simulator_settings() -> SimulatorSettings:
    return SimulatorSettings()
