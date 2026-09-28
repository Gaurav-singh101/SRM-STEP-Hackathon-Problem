"""Kafka producer adapter: the simulator's only I/O.

Everything above this module is pure, so the generation logic is unit-testable
without a broker and the throughput of the generator can be measured separately
from the throughput of the transport.

``confluent_kafka`` is imported lazily inside ``_load_producer_class`` rather than
at module scope. Two reasons, one of them concrete: on the development machine
Windows Application Control blocks librdkafka's native DLL (blocker B2), so a
module-level import would break every unit test that merely touches this package -
including tests that never produce anything. The service itself runs in a Linux
container where the import succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import orjson

from fleet_common.settings import KafkaSettings

logger = logging.getLogger(__name__)


class _Producer(Protocol):
    """The slice of confluent_kafka.Producer this adapter uses.

    Declared as a Protocol so tests can substitute a fake without importing the
    native library, and so mypy still checks the call sites.
    """

    def produce(
        self,
        topic: str,
        value: bytes | None = ...,
        key: bytes | None = ...,
        on_delivery: Any = ...,
    ) -> None: ...

    def poll(self, timeout: float) -> int: ...

    def flush(self, timeout: float) -> int: ...


@dataclass(slots=True)
class ProduceStats:
    """Outcome of a produce run.

    ``delivered`` counts broker acknowledgements, not ``produce()`` calls. The
    distinction is the whole point: a lossless-burst claim rests on acknowledged
    deliveries, and counting local enqueues instead would make the claim
    meaningless (CLAUDE.md S3).
    """

    enqueued: int = 0
    delivered: int = 0
    failed: int = 0
    queue_full_retries: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def is_lossless(self) -> bool:
        return self.failed == 0 and self.delivered == self.enqueued


def build_producer_config(settings: KafkaSettings) -> dict[str, Any]:
    """librdkafka configuration.

    ``enable.idempotence`` implies acks=all, bounded in-flight requests and
    retries, which together give per-partition exactly-once *produce* semantics.
    That is what makes at-least-once delivery downstream safe to build idempotent
    writes on (CLAUDE.md S8).
    """
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        # Idempotent producer: no duplicates from internal retries, and ordering is
        # preserved per partition even when a retry occurs.
        "enable.idempotence": True,
        "acks": "all",
        "compression.type": "lz4",
        # Batching. Per-event network round trips are forbidden in the hot path
        # (CLAUDE.md S3); linger lets librdkafka fill batches before sending.
        "linger.ms": 20,
        "batch.size": 1_048_576,
        "queue.buffering.max.messages": 1_000_000,
        "queue.buffering.max.kbytes": 1_048_576,
        # Back-pressure rather than unbounded memory growth (CLAUDE.md S9): when
        # the local queue fills, produce() raises and the caller pauses.
        "message.timeout.ms": 120_000,
        "socket.keepalive.enable": True,
        "client.id": "fleet-simulator",
    }


def _load_producer_class() -> type[Any]:
    """Import confluent_kafka.Producer on demand, with an actionable error."""
    try:
        from confluent_kafka import Producer
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(
            "confluent-kafka could not be imported. On Windows this is usually "
            "Application Control blocking librdkafka's native DLL (see blocker B2 "
            "in docs/PROGRESS.md); run the simulator in its container instead."
        ) from exc
    return Producer


def serialise(event: dict[str, Any]) -> bytes:
    """Serialise one event for the wire.

    orjson rather than the stdlib: it is several times faster and emits compact
    output, and at 100K events/s serialisation is a measurable share of the
    producer's cost (CLAUDE.md S9).
    """
    return orjson.dumps(event)


def partition_key(event: dict[str, Any]) -> bytes:
    """Partition by VIN (ADR-0004).

    Per-vehicle ordering is not a preference: the idle state machine is a
    sequential fold over one vehicle's samples, so two partitions carrying the same
    VIN would interleave and corrupt every segment it produced.
    """
    return str(event["vin"]).encode("ascii")


class TelemetryProducer:
    """Batched Kafka producer for telemetry events."""

    def __init__(
        self,
        settings: KafkaSettings,
        *,
        producer: _Producer | None = None,
    ) -> None:
        self._settings = settings
        self._topic = settings.kafka_topic_telemetry
        if producer is not None:
            self._producer: _Producer = producer
        else:
            producer_cls = _load_producer_class()
            self._producer = producer_cls(build_producer_config(settings))
        self.stats = ProduceStats()

    def _on_delivery(self, err: object | None, _msg: object) -> None:
        """Delivery callback. Counts acknowledgements and records failures."""
        if err is None:
            self.stats.delivered += 1
            return
        self.stats.failed += 1
        # Bounded: an error storm must not exhaust memory through the error list.
        if len(self.stats.errors) < 20:
            self.stats.errors.append(str(err))

    def produce_batch(self, events: Sequence[dict[str, Any]]) -> None:
        """Enqueue a batch, applying back-pressure when the local queue is full."""
        for event in events:
            self._enqueue(event)
        # Serve delivery callbacks without blocking, so stats stay current and the
        # queue drains while we keep generating.
        self._producer.poll(0)

    def _enqueue(self, event: dict[str, Any]) -> None:
        payload = serialise(event)
        key = partition_key(event)
        while True:
            try:
                self._producer.produce(
                    self._topic, value=payload, key=key, on_delivery=self._on_delivery
                )
                self.stats.enqueued += 1
                return
            except BufferError:
                # The local queue is full: the broker is slower than we generate.
                # Block on poll to drain rather than dropping or growing unbounded.
                self.stats.queue_full_retries += 1
                self._producer.poll(0.5)

    def produce_all(
        self, batches: Iterable[Sequence[dict[str, Any]]], *, flush_every: int = 50
    ) -> ProduceStats:
        """Produce many batches, flushing periodically. Returns final stats."""
        for index, batch in enumerate(batches, start=1):
            self.produce_batch(batch)
            if index % flush_every == 0:
                self._producer.poll(0)
        self.flush()
        return self.stats

    def flush(self, timeout: float = 60.0) -> int:
        """Wait for outstanding messages. Returns the number still unsent.

        A non-zero return means messages were NOT acknowledged. The caller must
        treat that as data loss and report it, never assume success.
        """
        remaining = self._producer.flush(timeout)
        if remaining:
            logger.error(
                "flush timed out with %d message(s) unacknowledged; this is data loss",
                remaining,
            )
        return remaining

    def __enter__(self) -> TelemetryProducer:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.flush()
