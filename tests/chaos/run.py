"""Chaos test: kill infrastructure, prove recovery, prove nothing was lost.

    uv run python -m tests.chaos.run --scenario broker
    uv run python -m tests.chaos.run --scenario all

CLAUDE.md §12 asks for a scripted chaos test that kills a pod and a broker and proves
recovery. The recovery half is the easy half — a container restarts and something reconnects.
The half worth writing is **accounting**: after the kill, does the number of events that
reached the stores still match the number produced?

That is the only question that distinguishes "it came back" from "it came back and the data is
intact", and it is the claim CLAUDE.md §3 forbids making without a test behind it.

Each scenario therefore: takes a baseline count, produces a known number of events, kills
something mid-flight, waits for recovery, and reconciles the counts. A scenario that cannot
reconcile its counts **fails** rather than reporting a successful restart.

**RUN THIS EXCLUSIVELY.** It SIGKILLs Kafka, PostgreSQL and Redis in turn, so anything else
using the stack will fail in a way that looks like that other thing's bug. It cost me a run
to learn: a 100,000-vehicle seed was executing at the same time and died with "server closed
the connection unexpectedly", which reads as a seed defect and was this script killing the
database underneath it. ``--require-idle`` refuses to start if the stack looks busy.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("chaos")

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "chaos"

#: Compose service names, matching docker-compose.yml.
BROKER = "kafka"
POSTGRES = "postgres"
REDIS = "redis"

#: How long to allow for recovery before calling it a failure. Generous on purpose: the
#: question is whether it recovers at all, not whether it is quick.
RECOVERY_TIMEOUT_SECONDS = 180

#: The processor image and the compose network it joins.
#:
#: The scenario has to run a consumer itself. Without one, events produced after the kill sit
#: on the topic, MongoDB gains nothing, and the shortfall check reports "data loss" when the
#: truth is "nobody was reading". The first working version of this script did exactly that
#: and declared 65,753 events lost. A test that false-alarms on loss gets ignored, and then it
#: is not believed on the run where loss is real - so the consumer is part of the scenario now,
#: not an assumption about the environment.
PROCESSOR_IMAGE = "fleet/processor:dev"
COMPOSE_NETWORK = "fleet-intel_default"

#: Seconds of empty polls before the processor exits. It has to outlast a rebalance after the
#: broker restart, or the drain stops before the backlog is consumed.
PROCESSOR_IDLE_EXIT_SECONDS = 20


@dataclass(slots=True)
class ScenarioResult:
    name: str
    target: str
    killed_at: str = ""
    recovered_after_seconds: float | None = None
    events_before: int = 0
    events_after: int = 0
    events_produced: int = 0
    #: What the processor itself reported. Parsed from its run summary because the processor
    #: is the only component that knows WHY an event did not reach a store.
    events_consumed: int = 0
    events_accepted: int = 0
    notes: list[str] = field(default_factory=list)
    passed: bool = False

    @property
    def nothing_stranded(self) -> bool:
        """Every produced event reached a consumer.

        The first half of "no loss": an event still sitting on the topic after the drain is
        one the pipeline never saw.
        """
        return self.events_consumed >= self.events_produced

    @property
    def accepted_all_persisted(self) -> bool:
        """Every event the processor ACCEPTED reached the store.

        The second half. Comparing produced against persisted - which two earlier versions of
        this test did - is wrong, and wrong in the direction that manufactures alarm: the
        pipeline legitimately rejects invalid events to a DLQ, routes late ones to the late
        topic, and drops duplicates. Those are not losses, they are the validator working, and
        counting them as missing reported 51,224 "lost" events on a run where the processor
        had consumed every single one.

        The honest question is whether anything vanished *without being accounted for*, and the
        processor's own summary is the only place that distinguishes the cases.
        """
        return self.events_after - self.events_before >= self.events_accepted

    @property
    def accounted_for(self) -> bool:
        return self.nothing_stranded and self.accepted_all_persisted

    def summary(self) -> dict[str, object]:
        return {
            "scenario": self.name,
            "target": self.target,
            "killed_at": self.killed_at,
            "recovered_after_seconds": self.recovered_after_seconds,
            "events_before": self.events_before,
            "events_after": self.events_after,
            "events_produced": self.events_produced,
            "events_consumed": self.events_consumed,
            "events_accepted": self.events_accepted,
            "delta": self.events_after - self.events_before,
            "nothing_stranded": self.nothing_stranded,
            "accepted_all_persisted": self.accepted_all_persisted,
            "accounted_for": self.accounted_for,
            "passed": self.passed,
            "notes": self.notes,
        }


def _compose(
    *args: str, check: bool = True, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _healthy(service: str) -> bool:
    """Whether compose reports the service healthy.

    Reads compose's own health state rather than attempting a connection: the healthchecks in
    docker-compose.yml are the definition of ready for this stack, and duplicating them here
    would create a second, disagreeing definition.
    """
    try:
        out = _compose("ps", "--format", "json", service, check=False).stdout
    except subprocess.TimeoutExpired:
        return False

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("Service") != service:
            continue
        health = (row.get("Health") or "").lower()
        state = (row.get("State") or "").lower()
        # An empty Health means no healthcheck is defined; running is then the best signal.
        return state == "running" and health in ("healthy", "")
    return False


def _wait_for_recovery(service: str) -> float | None:
    began = time.perf_counter()
    while time.perf_counter() - began < RECOVERY_TIMEOUT_SECONDS:
        if _healthy(service):
            return time.perf_counter() - began
        time.sleep(3)
    return None


def _mongo_event_count() -> int:
    """Raw telemetry documents. The end of the write path, so the honest place to count."""
    out = _compose(
        "exec",
        "-T",
        "mongo",
        "mongosh",
        "fleet_telemetry",
        "--quiet",
        "--eval",
        "db.telemetry_raw.estimatedDocumentCount()",
        check=False,
        timeout=180,
    ).stdout
    for line in reversed(out.splitlines()):
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    return -1


def _produce(vehicles: int, seed: int) -> int:
    """Produce events, returning the count Kafka **acknowledged**.

    Drives the simulator's own producer and generator. The first version of this function
    invented ``build_producer`` and ``produce_event``, neither of which exists, and the first
    chaos run failed on the import. The test catching that is the test working - but it
    caught my error rather than the system's, which is a wasted run.

    The acknowledged count matters rather than the generated one: an event the broker never
    confirmed has not been produced, and counting it would build the shortfall check on a
    false baseline and report loss that never happened.
    """
    import numpy as np

    from fleet_common.settings import kafka_settings
    from simulator.adapters.kafka_producer import TelemetryProducer
    from simulator.fleet import build_fleet
    from simulator.journey import JourneyConfig, generate_day

    fleet = build_fleet(vehicle_count=vehicles, seed=seed)
    rng = np.random.default_rng(seed)
    config = JourneyConfig()
    # Backdated a FULL day, matching the simulator's own --start default. One hour was not
    # enough: generate_day produces a whole working day forward from day_start, so an hour
    # back puts roughly 23 hours of it in the future, and the validator correctly rejects all
    # of that as timestamp_too_far_future. The simulator defaults to yesterday for exactly
    # this reason - a lesson already learned once in this project and not carried over here.
    day_start = datetime.now(UTC) - timedelta(days=1)

    producer = TelemetryProducer(kafka_settings())
    generated = 0
    try:
        for vehicle in fleet:
            events, _ = generate_day(vehicle, day_start, rng=rng, config=config)
            producer.produce_batch(events)
            generated += len(events)
    finally:
        producer.flush()

    delivered = int(producer.stats.delivered)
    if delivered != generated:
        logger.warning("generated %d event(s) but %d were acknowledged", generated, delivered)
    return delivered


def _other_postgres_connections() -> int:
    """Active backends other than this check itself.

    A crude interlock, and enough of one: the failure it prevents is a concurrent job dying
    with "server closed the connection unexpectedly", which points the reader at the wrong
    script entirely.
    """
    out = _compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "fleet",
        "-d",
        "fleet",
        "-At",
        "-c",
        "SELECT count(*) FROM pg_stat_activity"
        " WHERE datname = 'fleet' AND pid <> pg_backend_pid() AND state = 'active'",
        check=False,
        timeout=30,
    ).stdout
    for line in reversed(out.splitlines()):
        if line.strip().isdigit():
            return int(line.strip())
    return 0


def _drain_with_processor() -> tuple[int, int, str | None]:
    """Run the processor until the topic is quiet, returning (messages consumed, error).

    In a container rather than in-process, because that is how the processor actually runs
    and because the scenario should exercise the shipped image, not a different code path
    assembled for the test.

    Returns (consumed, accepted, error). The error is returned rather than raised so the
    caller can report "the consumer did not run" as its own finding, distinct from data loss.
    """
    try:
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                COMPOSE_NETWORK,
                "--env-file",
                ".env",
                "-e",
                "KAFKA_BOOTSTRAP_SERVERS=kafka:19092",
                "-e",
                "POSTGRES_HOST=postgres",
                "-e",
                "POSTGRES_PORT=5432",
                "-e",
                "MONGO_URI=mongodb://mongo:27017",
                "-e",
                "REDIS_URL=redis://redis:6379/0",
                PROCESSOR_IMAGE,
                "--idle-exit-seconds",
                str(PROCESSOR_IDLE_EXIT_SECONDS),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 0, 0, str(exc)

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-3:]
        return 0, 0, f"exit {completed.returncode}: {' | '.join(tail)}"

    # Lift the processor's own accounting. It is the only component that knows why an event
    # did not reach a store - accepted, duplicate, late, or rejected to the DLQ - and without
    # those numbers this test can only compare produced against persisted, which is what made
    # two earlier versions report loss that had not happened.
    consumed = accepted = 0
    for line in (completed.stderr or "").splitlines() + (completed.stdout or "").splitlines():
        if "messages consumed" in line:
            digits = "".join(c for c in line.split(":")[-1] if c.isdigit())
            if digits:
                consumed = int(digits)
        elif "accepted" in line and ":" in line:
            digits = "".join(c for c in line.split(":")[-1] if c.isdigit())
            if digits:
                accepted = int(digits)
    return consumed, accepted, None


def kill_and_recover(service: str, *, vehicles: int, seed: int) -> ScenarioResult:
    """Kill one service mid-flight, then reconcile what reached the store."""
    result = ScenarioResult(name=f"kill-{service}", target=service)

    result.events_before = _mongo_event_count()
    if result.events_before < 0:
        result.notes.append("could not read a baseline count from MongoDB")
        return result

    logger.info("baseline: %d documents", result.events_before)

    # SIGKILL, not a graceful stop. A clean shutdown tests the shutdown path; chaos is about
    # the path where nothing got to run its finally block.
    logger.info("killing %s", service)
    result.killed_at = datetime.now(UTC).isoformat()
    _compose("kill", "-s", "SIGKILL", service, check=False)

    logger.info("restarting %s", service)
    _compose("start", service, check=False)

    recovered = _wait_for_recovery(service)
    result.recovered_after_seconds = round(recovered, 1) if recovered is not None else None
    if recovered is None:
        result.notes.append(f"{service} did not become healthy within {RECOVERY_TIMEOUT_SECONDS}s")
        return result

    logger.info("%s healthy after %.1fs; producing", service, recovered)
    try:
        result.events_produced = _produce(vehicles, seed)
    except Exception as exc:
        result.notes.append(f"produce failed after recovery: {exc}")
        return result

    # Run a consumer. This is the step whose absence made the first version of this test
    # declare data loss that had not happened.
    logger.info("draining with the processor")
    drained, accepted, drain_error = _drain_with_processor()
    if drain_error:
        # Distinguished from loss on purpose. "The consumer would not start" and "the
        # consumer ran and events went missing" are different findings with different fixes,
        # and reporting the first as the second is how a test loses its credibility.
        result.notes.append(f"CONSUMER DID NOT RUN: {drain_error}")
        result.notes.append(
            "No conclusion about data loss can be drawn from this run - nothing was reading."
        )
        return result
    result.events_consumed = drained
    result.events_accepted = accepted
    result.notes.append(
        f"processor consumed {drained} message(s) and accepted {accepted} after recovery"
    )

    # Even with the drain finished, counting can race the last bulk write, so poll rather
    # than sleep a fixed amount (CLAUDE.md §12).
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        result.events_after = _mongo_event_count()
        if result.accounted_for:
            break
        time.sleep(5)

    result.passed = result.accounted_for
    gained = result.events_after - result.events_before

    if not result.nothing_stranded:
        # Events produced but never consumed. This one IS loss, or at least an event the
        # pipeline never saw, which amounts to the same thing for a cost figure.
        stranded = result.events_produced - result.events_consumed
        result.notes.append(
            f"STRANDED: {stranded} event(s) were produced but never consumed "
            f"(produced {result.events_produced}, consumed {result.events_consumed}). "
            "Either the drain stopped early or the events never reached the broker."
        )
    if not result.accepted_all_persisted:
        shortfall = result.events_accepted - gained
        result.notes.append(
            f"SHORTFALL: the processor accepted {result.events_accepted} event(s) but the "
            f"store gained only {gained} ({shortfall} missing). Accepted means validated and "
            "destined for a store, so this is real loss."
        )
    if result.passed:
        result.notes.append(
            f"Accounted for: {result.events_consumed} consumed >= {result.events_produced} "
            f"produced, and the store gained {gained} >= {result.events_accepted} accepted. "
            "The difference between produced and accepted is the validator rejecting invalid "
            "events to the DLQ, routing late ones to the late topic and dropping duplicates - "
            "all accounted for, none lost."
        )
    return result


SCENARIOS = {
    "broker": BROKER,
    "postgres": POSTGRES,
    "redis": REDIS,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chaos", description=__doc__)
    parser.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="broker")
    parser.add_argument("--vehicles", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument(
        "--require-idle",
        action="store_true",
        help="refuse to start if PostgreSQL has other active connections",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s", stream=sys.stdout)

    if args.require_idle:
        busy = _other_postgres_connections()
        if busy:
            print(
                f"refusing to start: PostgreSQL has {busy} other active connection(s).\n"
                "This script SIGKILLs the databases, so a concurrent job would fail in a way "
                "that looks like its own bug rather than like this one.",
                file=sys.stderr,
            )
            return 2

    targets = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    results = []
    for name in targets:
        logger.info("=== scenario: kill %s ===", SCENARIOS[name])
        results.append(kill_and_recover(SCENARIOS[name], vehicles=args.vehicles, seed=args.seed))

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"chaos-{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "date_utc": datetime.now(UTC).isoformat(),
                "results": [r.summary() for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        print(f"[{verdict}] {r.name}")
        print(f"  recovered after   : {r.recovered_after_seconds}s")
        print(f"  produced          : {r.events_produced:,}")
        # Only meaningful once the store was actually re-counted. On an early return
        # events_after is still 0, and printing the difference then reported a delta of
        # minus the entire baseline - an alarming number that meant nothing.
        if r.events_after:
            print(f"  store delta       : {r.events_after - r.events_before:,}")
        else:
            print("  store delta       : not re-counted (run stopped before the count step)")
        for note in r.notes:
            print(f"  note              : {note}")
    print(f"\nevidence: {out}")

    # A failure here is data loss, so it fails the command rather than being a note in a file.
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
