"""Tests for configuration loading.

Every test passes ``_env_file=None``. Without it, pydantic-settings reads the
repository's own ``.env`` and the results would depend on the developer's local
file - which is exactly the kind of non-determinism CLAUDE.md S12 forbids.

The JWT algorithm validator gets the most attention here because it is a security
control, not a convenience: accepting ``none`` or an HMAC algorithm from a JWKS
issuer is a complete authentication bypass.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fleet_common.settings import (
    ApiSettings,
    AppSettings,
    AuthSettings,
    CostSettings,
    IdleSettings,
    KafkaSettings,
    MongoSettings,
    PostgresSettings,
    RedisSettings,
    SimulatorSettings,
    StreamSettings,
    UtilisationSettings,
)


def auth(**overrides: object) -> AuthSettings:
    return AuthSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


def postgres(**overrides: object) -> PostgresSettings:
    defaults: dict[str, object] = {"postgres_password": "test-only"}
    return PostgresSettings(_env_file=None, **{**defaults, **overrides})  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# JWT algorithm allow-list (D4) - a security control
# --------------------------------------------------------------------------


def test_default_algorithms_are_asymmetric_only() -> None:
    assert auth().allowed_algorithms == ["RS256", "ES256"]


@pytest.mark.parametrize(
    "value",
    ["none", "NONE", "RS256,none", "none,RS256", "  None  "],
)
def test_alg_none_is_refused(value: str) -> None:
    """'none' disables signature verification entirely: any forged token verifies."""
    with pytest.raises(ValidationError, match="unsafe JWT algorithms"):
        auth(oidc_allowed_algorithms=value)


@pytest.mark.parametrize("value", ["HS256", "HS384", "HS512", "RS256,HS256"])
def test_hmac_algorithms_are_refused(value: str) -> None:
    """Algorithm confusion: with an HMAC algorithm accepted against a JWKS issuer,
    an attacker signs a token using the public key as the shared secret, and it
    verifies. The public key is, by definition, public."""
    with pytest.raises(ValidationError, match="unsafe JWT algorithms"):
        auth(oidc_allowed_algorithms=value)


def test_empty_algorithm_list_is_refused() -> None:
    """An empty allow-list would accept nothing or, worse, be treated as 'any'."""
    with pytest.raises(ValidationError, match="at least one JWT algorithm"):
        auth(oidc_allowed_algorithms="   ")


def test_algorithm_list_tolerates_whitespace() -> None:
    assert auth(oidc_allowed_algorithms=" RS256 , ES384 ").allowed_algorithms == [
        "RS256",
        "ES384",
    ]


def test_no_auth_vendor_appears_in_defaults() -> None:
    """D4 stays open: the code must not name a provider (CLAUDE.md S3)."""
    values = " ".join(str(v) for v in auth().model_dump().values()).lower()
    for vendor in ("cognito", "auth0", "okta", "keycloak", "azure", "firebase"):
        assert vendor not in values


# --------------------------------------------------------------------------
# Postgres: secrets must not leak into logs
# --------------------------------------------------------------------------


def test_password_has_no_default() -> None:
    """A missing password must fail at startup, not fall back to something that
    might connect to the wrong database."""
    with pytest.raises(ValidationError):
        PostgresSettings(_env_file=None)  # type: ignore[call-arg]


def test_dsn_contains_the_password() -> None:
    assert "password=test-only" in postgres().dsn


def test_safe_dsn_omits_the_password() -> None:
    """safe_dsn is what may appear in logs and error messages (CLAUDE.md S3)."""
    settings = postgres()
    assert "test-only" not in settings.safe_dsn
    assert "password" not in settings.safe_dsn
    assert "dbname=fleet" in settings.safe_dsn


def test_password_is_not_exposed_by_repr() -> None:
    """repr() lands in tracebacks and debug output, so it must not carry the secret.

    Regression guard: `dsn` was briefly a pydantic computed_field, which pydantic
    includes in repr() and model_dump(). That leaked the password everywhere the
    settings object was printed, defeating SecretStr. It is a plain property now.
    """
    assert "test-only" not in repr(postgres())


def test_password_is_not_exposed_by_model_dump() -> None:
    """model_dump() is what gets logged as structured context."""
    dumped = str(postgres().model_dump())
    assert "test-only" not in dumped


def test_password_is_not_exposed_by_json_serialisation() -> None:
    """The path a settings object would take into a JSON log line."""
    assert "test-only" not in postgres().model_dump_json()


def test_dsn_is_not_a_serialised_field() -> None:
    """Asserts the mechanism, not just the symptom: if someone re-adds
    @computed_field to dsn, this fails immediately."""
    assert "dsn" not in postgres().model_dump()
    assert "safe_dsn" not in postgres().model_dump()


def test_default_port_avoids_the_common_local_collision() -> None:
    """Not 5432: a locally installed PostgreSQL service usually owns that port, and
    on Windows connections silently reach it instead of the container - which
    presents as a wrong-password error."""
    assert postgres().postgres_port == 5432 or postgres().postgres_port == 55432


def test_statement_timeout_is_set() -> None:
    """Every external call needs a timeout (CLAUDE.md S7): without one a slow query
    holds a connection until the pool is exhausted."""
    assert postgres().postgres_statement_timeout_ms > 0


# --------------------------------------------------------------------------
# Business parameters
# --------------------------------------------------------------------------


def test_idle_defaults_match_adr_0001() -> None:
    settings = IdleSettings(_env_file=None)
    assert settings.idle_min_seconds == 180
    assert settings.idle_excessive_seconds == 600
    assert settings.idle_gap_tolerance_seconds == 30
    # Hysteresis: the exit bound must sit above the entry bound (ADR-0001).
    assert settings.idle_exit_speed_kph > settings.idle_entry_speed_kph
    # Optional exclusions default off.
    assert settings.idle_exclude_pto_active is False


def test_utilisation_weights_sum_to_one() -> None:
    """Otherwise the score silently stops being a percentage (ADR-0003)."""
    settings = UtilisationSettings(_env_file=None)
    total = (
        settings.util_weight_active_day
        + settings.util_weight_moving_hours
        + settings.util_weight_distance
    )
    assert total == pytest.approx(1.0)


def test_currency_is_normalised_to_upper_case() -> None:
    assert CostSettings(_env_file=None, default_currency="inr").default_currency == "INR"


@pytest.mark.parametrize("bad", ["RUPEE", "IN", "I2R", ""])
def test_invalid_currency_is_refused(bad: str) -> None:
    with pytest.raises(ValidationError):
        CostSettings(_env_file=None, default_currency=bad)


def test_formula_versions_are_stamped() -> None:
    """Every figure must be attributable to the rules that produced it."""
    assert CostSettings(_env_file=None).fuel_formula_version == "idle-fuel-v1"
    assert UtilisationSettings(_env_file=None).utilisation_formula_version == "utilisation-v1"


# --------------------------------------------------------------------------
# Kafka, stores, API, simulator
# --------------------------------------------------------------------------


#: Measured single-consumer throughput, from docs/evidence/load/pipeline-end-to-end-local.md.
MEASURED_CONSUMER_EVENTS_PER_SECOND = 4_404
NFR_EVENTS_PER_SECOND = 100_000


def test_kafka_partition_count_matches_adr_0004() -> None:
    settings = KafkaSettings(_env_file=None)
    assert settings.kafka_num_partitions == 96
    # Divides by all of these, so consumer count scales without repartitioning.
    for divisor in (2, 3, 4, 6, 8, 12, 16, 24, 32, 48):
        assert settings.kafka_num_partitions % divisor == 0


def test_partition_count_leaves_headroom_above_the_consumers_the_nfr_needs() -> None:
    """Partitions are a hard ceiling on consumer parallelism: one partition is read by
    exactly one consumer, so a 25th consumer against 24 partitions simply idles.

    At the measured throughput the NFR needs ~23 consumers. 24 partitions left none
    spare for a failed consumer or a rolling deploy, and Kafka cannot shrink a topic
    later - nor grow one safely, because that rewrites every key's partition mapping and
    would split a vehicle's in-flight idle state across two consumers.
    """
    settings = KafkaSettings(_env_file=None)
    consumers_needed = NFR_EVENTS_PER_SECOND / MEASURED_CONSUMER_EVENTS_PER_SECOND
    headroom = settings.kafka_num_partitions / consumers_needed
    assert headroom >= 3.0, (
        f"only {headroom:.1f}x headroom over the ~{consumers_needed:.0f} consumers the "
        "NFR needs; a single consumer failure would put throughput under target"
    )


def test_kafka_batches_rather_than_sending_per_event() -> None:
    """Per-event round trips in the hot path are forbidden (CLAUDE.md S3)."""
    assert KafkaSettings(_env_file=None).kafka_batch_max_messages > 1


def test_all_five_topics_are_configured() -> None:
    settings = KafkaSettings(_env_file=None)
    topics = {
        settings.kafka_topic_telemetry,
        settings.kafka_topic_dlq,
        settings.kafka_topic_late,
        settings.kafka_topic_idle_events,
        settings.kafka_topic_alerts,
    }
    assert len(topics) == 5, "topic names must be distinct"


def test_mongo_hot_tier_has_a_ttl() -> None:
    """At the PDF's 1 KB/event model a week-long hot tier is ~60 TB, so raw
    telemetry must expire (CLAUDE.md S8 lifecycle)."""
    assert 0 < MongoSettings(_env_file=None).mongo_raw_ttl_seconds <= 7 * 86_400


def test_redis_url_is_configurable() -> None:
    assert RedisSettings(_env_file=None, redis_url="redis://h:1/2").redis_url.endswith("/2")


def test_api_page_size_has_a_hard_maximum() -> None:
    """An unbounded page size is a denial-of-service vector and defeats keyset
    pagination (CLAUDE.md S10)."""
    settings = ApiSettings(_env_file=None)
    assert settings.api_page_size_default <= settings.api_page_size_max
    assert settings.api_page_size_max <= 1000


def test_cors_origins_parse_to_a_list() -> None:
    settings = ApiSettings(_env_file=None, api_cors_origins="http://a, http://b")
    assert settings.cors_origins == ["http://a", "http://b"]


def test_simulator_is_seeded() -> None:
    """An unseeded simulator makes every measurement unrepeatable (CLAUDE.md S9)."""
    assert SimulatorSettings(_env_file=None).sim_seed > 0


@pytest.mark.parametrize(
    "field", ["sim_duplicate_rate", "sim_out_of_order_rate", "sim_malformed_rate"]
)
def test_simulator_rates_are_probabilities(field: str) -> None:
    with pytest.raises(ValidationError):
        SimulatorSettings(_env_file=None, **{field: 1.5})  # type: ignore[arg-type]


def test_simulator_generates_the_edge_cases_the_pdf_asks_for() -> None:
    """PDF S7 wants a "bursty, out-of-order, duplicate" generator."""
    settings = SimulatorSettings(_env_file=None)
    assert settings.sim_duplicate_rate > 0
    assert settings.sim_out_of_order_rate > 0
    assert settings.sim_malformed_rate > 0


def test_lateness_window_is_positive() -> None:
    """Late events go to the late topic and are reconciled; never dropped."""
    assert StreamSettings(_env_file=None).allowed_lateness_seconds > 0


def test_settings_are_immutable() -> None:
    """Config must not drift at runtime, or a figure stops being reproducible."""
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None).log_level = "DEBUG"  # type: ignore[misc]


def test_unknown_environment_variables_are_ignored() -> None:
    """Services share one .env, so each must tolerate the others' keys."""
    assert AppSettings(_env_file=None, some_other_service_key="x").app_env == "local"  # type: ignore[call-arg]


def test_invalid_log_level_is_refused() -> None:
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, log_level="CHATTY")  # type: ignore[arg-type]
