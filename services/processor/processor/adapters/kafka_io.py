"""Kafka consumer and side-topic producer.

Offsets are committed **only after** a batch has been persisted. That single ordering
rule is what makes the system effectively-once: a crash between persisting and
committing replays the batch, and the idempotent sinks absorb it. Committing first
would instead lose data silently, which is the worse failure by far.

``confluent_kafka`` is imported lazily so unit tests can import this module on a
machine where the native library is blocked (blocker B2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

import orjson

from fleet_common.events import TelemetryEvent
from fleet_common.settings import KafkaSettings
from processor.validation import Rejection

logger = logging.getLogger(__name__)

#: How long a poll waits for a full batch before returning what it has. Short enough
#: that a quiet topic still yields promptly, long enough to fill real batches.
DEFAULT_POLL_TIMEOUT: Final[float] = 1.0


class _Consumer(Protocol):
    """The slice of confluent_kafka.Consumer this adapter uses."""

    def subscribe(self, topics: list[str], **kwargs: Any) -> None: ...
    def consume(self, num_messages: int, timeout: float) -> list[Any]: ...
    def commit(self, asynchronous: bool = ...) -> Any: ...
    def assignment(self) -> list[Any]: ...
    def close(self) -> None: ...
    def pause(self, partitions: list[Any]) -> None: ...
    def resume(self, partitions: list[Any]) -> None: ...


class _Producer(Protocol):
    def produce(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        headers: Any = ...,
    ) -> None: ...
    def poll(self, timeout: float) -> int: ...
    def flush(self, timeout: float) -> int: ...


def build_consumer_config(settings: KafkaSettings) -> dict[str, Any]:
    """librdkafka consumer configuration.

    ``enable.auto.commit`` is false and stays false: auto-commit would advance offsets
    on a timer regardless of whether the batch was persisted, which loses data on any
    crash. Manual commit after persistence is the whole correctness argument.
    """
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": settings.kafka_consumer_group,
        "enable.auto.commit": False,
        # Start from the beginning for a new group, so a fresh deployment processes
        # the backlog rather than silently skipping it.
        "auto.offset.reset": "earliest",
        # Cooperative rebalancing: only the moving partitions stop, instead of every
        # consumer dropping everything on each membership change.
        "partition.assignment.strategy": "cooperative-sticky",
        # Must exceed the worst-case batch processing time, or the broker evicts a
        # consumer that is merely busy and triggers a pointless rebalance.
        "max.poll.interval.ms": 300_000,
        "session.timeout.ms": 45_000,
        "fetch.min.bytes": 1,
        "fetch.wait.max.ms": 100,
        "queued.max.messages.kbytes": 65_536,
        "client.id": "fleet-processor",
    }


def build_side_producer_config(settings: KafkaSettings) -> dict[str, Any]:
    """Producer for the DLQ, late and idle-event topics."""
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "enable.idempotence": True,
        "acks": "all",
        "compression.type": "lz4",
        "linger.ms": 20,
        "client.id": "fleet-processor-side",
    }


def _load_kafka() -> tuple[type[Any], type[Any]]:
    """Import confluent_kafka on demand, with an actionable message."""
    try:
        from confluent_kafka import Consumer, Producer
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "confluent-kafka could not be imported. On Windows this is usually "
            "Application Control blocking librdkafka's native library (blocker B2 in "
            "docs/PROGRESS.md); run the processor in its container instead."
        ) from exc
    return Consumer, Producer


@dataclass(slots=True)
class SideTopicCounts:
    dlq: int = 0
    late: int = 0
    idle_events: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


class SideTopicProducer:
    """Publishes rejections, late events and closed idle segments.

    The DLQ and late topics are not incidental: CLAUDE.md S8 forbids silently dropping
    anything, so every event the stream cannot use has to land somewhere durable with
    a reason attached.
    """

    def __init__(self, settings: KafkaSettings, *, producer: _Producer | None = None) -> None:
        self._settings = settings
        if producer is not None:
            self._producer: _Producer = producer
        else:
            _, producer_cls = _load_kafka()
            self._producer = producer_cls(build_side_producer_config(settings))
        self.counts = SideTopicCounts()

    def publish_rejections(self, rejections: Sequence[Rejection]) -> None:
        """Send rejections to the DLQ with a machine-readable reason code.

        The reason travels as a Kafka header so the DLQ can be filtered by reason
        without parsing every payload.
        """
        for rejection in rejections:
            headers = [
                ("reason", rejection.reason.value.encode()),
                ("truncated", b"1" if rejection.was_truncated else b"0"),
            ]
            if rejection.vin:
                headers.append(("vin", rejection.vin.encode()))
            try:
                self._producer.produce(
                    self._settings.kafka_topic_dlq,
                    value=rejection.truncated_payload,
                    key=(rejection.vin or "unknown").encode(),
                    headers=headers,
                )
                self.counts.dlq += 1
            except BufferError:
                self._producer.poll(0.5)
                self._retry(
                    self._settings.kafka_topic_dlq,
                    rejection.truncated_payload,
                    (rejection.vin or "unknown").encode(),
                )

    def publish_late(self, events: Sequence[TelemetryEvent]) -> None:
        """Send late and out-of-order events to the reconciliation topic."""
        for event in events:
            payload = event.model_dump_json().encode()
            try:
                self._producer.produce(
                    self._settings.kafka_topic_late,
                    value=payload,
                    key=event.vin.encode(),
                )
                self.counts.late += 1
            except BufferError:
                self._producer.poll(0.5)
                self._retry(self._settings.kafka_topic_late, payload, event.vin.encode())

    def publish_idle_events(self, payloads: Sequence[tuple[str, bytes]]) -> None:
        """Publish closed idle segments for downstream consumers."""
        for vin, payload in payloads:
            try:
                self._producer.produce(
                    self._settings.kafka_topic_idle_events,
                    value=payload,
                    key=vin.encode(),
                )
                self.counts.idle_events += 1
            except BufferError:
                self._producer.poll(0.5)
                self._retry(self._settings.kafka_topic_idle_events, payload, vin.encode())

    def _retry(self, topic: str, payload: bytes, key: bytes) -> None:
        """One retry after draining the queue; a second failure is recorded, not hidden."""
        try:
            self._producer.produce(topic, value=payload, key=key)
        except BufferError as exc:
            self.counts.failed += 1
            if len(self.counts.errors) < 20:
                self.counts.errors.append(f"{topic}: {exc}")

    def flush(self, timeout: float = 30.0) -> int:
        """Flush before the caller commits offsets.

        Order matters: if offsets were committed while a DLQ message was still only
        queued locally, a crash would lose the record that the event was rejected.
        """
        remaining = self._producer.flush(timeout)
        if remaining:
            logger.error(
                "side-topic flush left %d message(s) unsent; not safe to commit",
                remaining,
            )
        return remaining


class TelemetryConsumer:
    """Batched consumer with manual offset commits."""

    def __init__(
        self,
        settings: KafkaSettings,
        *,
        consumer: _Consumer | None = None,
        on_assign: Callable[[list[str]], None] | None = None,
        on_revoke: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_assign = on_assign
        self._on_revoke = on_revoke
        if consumer is not None:
            self._consumer: _Consumer = consumer
        else:
            consumer_cls, _ = _load_kafka()
            self._consumer = consumer_cls(build_consumer_config(settings))

    def subscribe(self) -> None:
        self._consumer.subscribe(
            [self._settings.kafka_topic_telemetry],
            on_assign=self._handle_assign,
            on_revoke=self._handle_revoke,
        )

    def _handle_assign(self, _consumer: object, partitions: list[Any]) -> None:
        names = [f"{p.topic}:{p.partition}" for p in partitions]
        logger.info("partitions assigned: %s", names)
        if self._on_assign:
            self._on_assign(names)

    def _handle_revoke(self, _consumer: object, partitions: list[Any]) -> None:
        """Flush before the partitions move.

        An idle segment open at revocation would otherwise be lost, and long segments
        - the expensive ones - are the most likely to be open at any moment.
        """
        names = [f"{p.topic}:{p.partition}" for p in partitions]
        logger.info("partitions revoked: %s", names)
        if self._on_revoke:
            self._on_revoke(names)

    def poll_batch(
        self, max_messages: int, timeout: float = DEFAULT_POLL_TIMEOUT
    ) -> tuple[list[bytes], int]:
        """Return (payloads, error_count) for one batch.

        Broker-level errors are counted and logged rather than raised: a partition EOF
        or a transient metadata error must not kill the consumer.
        """
        messages = self._consumer.consume(num_messages=max_messages, timeout=timeout)
        payloads: list[bytes] = []
        errors = 0
        for message in messages:
            error = message.error()
            if error is not None:
                errors += 1
                logger.warning("consume error: %s", error)
                continue
            value = message.value()
            if value is not None:
                payloads.append(value)
        return payloads, errors

    def commit(self) -> None:
        """Commit synchronously, after persistence.

        Synchronous on purpose: an asynchronous commit could be in flight during a
        crash, leaving it ambiguous whether the batch was recorded as done.
        """
        self._consumer.commit(asynchronous=False)

    def assigned_partitions(self) -> list[str]:
        return [f"{p.topic}:{p.partition}" for p in self._consumer.assignment()]

    def close(self) -> None:
        """Leave the group cleanly, so a rebalance happens immediately rather than
        after the session timeout expires."""
        self._consumer.close()


def encode_idle_event(priced: Any) -> tuple[str, bytes]:
    """Serialise a priced idle segment for the ``idle.events`` topic."""
    segment = priced.segment
    estimate = priced.estimate
    payload: dict[str, Any] = {
        "tenant_id": segment.tenant_id,
        "vin": segment.vin,
        "started_at": segment.started_at.isoformat(),
        "ended_at": segment.ended_at.isoformat(),
        "duration_seconds": segment.duration_seconds,
        "sample_count": segment.sample_count,
        "max_gap_seconds": segment.max_gap_seconds,
        "excessive": segment.excessive,
        "bev_excluded": priced.bev_excluded,
    }
    if estimate is not None:
        # The assumptions travel with the figure, so any consumer can reproduce it
        # without asking this service what its configuration was (CLAUDE.md S14).
        payload |= {
            "fuel_litres_est": estimate.litres_estimated,
            "cost_est": str(estimate.cost),
            "currency": estimate.currency,
            "formula_version": estimate.formula_version,
            "burn_rate_lph": estimate.burn_rate_lph,
            "price_id": estimate.price_id,
            "idle_threshold_seconds": estimate.idle_threshold_seconds,
        }
    return segment.vin, orjson.dumps(payload)
