"""Draining the ``telemetry.late`` topic (ADR-0007).

A bounded drain, not a long-running consumer. Reconciliation is a batch job: it takes
whatever has accumulated, puts it right, commits, and exits. A daemon here would duplicate
the processor's lifecycle for no benefit and would make "run reconciliation and show me the
log" an awkward thing to ask for.

**Its own consumer group**, separate from the processor's. Sharing one would have the two
competing for partitions of the same topic, and the processor does not read this topic at
all.

``confluent_kafka`` is imported on demand. On Windows the native library is blocked by
Application Control (PROGRESS blocker B2), and a module-level import would make every other
batch subcommand - and every unit test - unrunnable on the developer's machine for the sake
of one command that only ever runs in a container.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from fleet_common.settings import KafkaSettings

logger = logging.getLogger(__name__)

#: Consumer group for reconciliation. Distinct from the processor's on purpose.
RECONCILE_GROUP_SUFFIX = "-reconciler"


def build_reconciler_config(settings: KafkaSettings) -> dict[str, Any]:
    """Consumer configuration for the drain.

    ``enable.auto.commit`` is false for the same reason it is false in the processor: an
    offset advanced on a timer rather than after persistence loses data on any crash. Here
    the stakes are slightly higher, because a lost late event is one nothing will send again.
    """
    return {
        "bootstrap.servers": settings.kafka_bootstrap_servers,
        "group.id": f"{settings.kafka_consumer_group}{RECONCILE_GROUP_SUFFIX}",
        "enable.auto.commit": False,
        # A new reconciler group must process the backlog that accumulated before it
        # existed - which, the first time this runs, is the entire point.
        "auto.offset.reset": "earliest",
        "partition.assignment.strategy": "cooperative-sticky",
        "max.poll.interval.ms": 600_000,
        "session.timeout.ms": 45_000,
        "client.id": "fleet-reconciler",
    }


class LateTopicDrain:
    """Reads the late topic until it is empty, then stops.

    "Empty" is inferred from consecutive empty polls rather than from broker lag, because
    lag requires an admin call and the consequence of guessing wrong is only that the next
    run picks up what this one missed - which is safe, being idempotent.
    """

    def __init__(
        self,
        settings: KafkaSettings,
        *,
        consumer: Any = None,
        idle_polls_before_stop: int = 3,
        poll_timeout_seconds: float = 2.0,
    ) -> None:
        self._settings = settings
        self._idle_polls_before_stop = idle_polls_before_stop
        self._poll_timeout = poll_timeout_seconds
        if consumer is None:
            from confluent_kafka import Consumer

            consumer = Consumer(build_reconciler_config(settings))
        self._consumer = consumer
        self._consumer.subscribe([settings.kafka_topic_late])

    def drain(self, *, max_messages: int = 100_000) -> list[bytes]:
        """Collect the accumulated late events.

        Capped, so one run cannot consume unbounded memory if the topic has been ignored for
        a long time. Whatever is left stays on the topic for the next run, which is correct
        rather than merely acceptable: the offsets have not been committed.
        """
        collected: list[bytes] = []
        idle_polls = 0

        while len(collected) < max_messages and idle_polls < self._idle_polls_before_stop:
            batch = self._consumer.consume(
                num_messages=min(5_000, max_messages - len(collected)),
                timeout=self._poll_timeout,
            )
            if not batch:
                idle_polls += 1
                continue

            idle_polls = 0
            for message in batch:
                error = message.error()
                if error is not None:
                    # Logged, not raised. A partition EOF or a transient broker error must
                    # not abandon events already collected.
                    logger.warning("late-topic consume error: %s", error)
                    continue
                value = message.value()
                if value:
                    collected.append(value)

        logger.info(
            "drained %d late event(s) from %s", len(collected), self._settings.kafka_topic_late
        )
        return collected

    def commit(self) -> None:
        """Advance offsets. Called only after the reconciliation is durable."""
        self._consumer.commit(asynchronous=False)

    def close(self) -> None:
        self._consumer.close()


def decode_late_events(raw: Sequence[bytes]) -> tuple[list[Any], list[str]]:
    """Decode drained payloads into telemetry events.

    Returns the events and a list of decode failures. Failures are returned rather than
    raised because a single unparseable payload on this topic must not stop the
    reconciliation of everything else - and because a payload that cannot be decoded here has
    already survived validation once, so it is worth reporting loudly rather than dropping.
    """
    from fleet_common.events import TelemetryEvent

    events: list[Any] = []
    failures: list[str] = []
    for payload in raw:
        try:
            events.append(TelemetryEvent.model_validate_json(payload))
        except Exception as exc:
            failures.append(f"undecodable late payload: {exc}")
    return events, failures
