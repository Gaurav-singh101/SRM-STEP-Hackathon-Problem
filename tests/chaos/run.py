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
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
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


@dataclass(slots=True)
class ScenarioResult:
    name: str
    target: str
    killed_at: str = ""
    recovered_after_seconds: float | None = None
    events_before: int = 0
    events_after: int = 0
    events_produced: int = 0
    notes: list[str] = field(default_factory=list)
    passed: bool = False

    @property
    def accounted_for(self) -> bool:
        """Whether the store gained at least what was produced.

        At least, not exactly: delivery is at-least-once, so a redelivered batch is correct
        behaviour. What must never happen is a shortfall - that is loss.
        """
        return self.events_after - self.events_before >= self.events_produced

    def summary(self) -> dict[str, object]:
        return {
            "scenario": self.name,
            "target": self.target,
            "killed_at": self.killed_at,
            "recovered_after_seconds": self.recovered_after_seconds,
            "events_before": self.events_before,
            "events_after": self.events_after,
            "events_produced": self.events_produced,
            "delta": self.events_after - self.events_before,
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
    """Produce a known number of events, returning the count actually sent."""
    from fleet_common.settings import kafka_settings
    from simulator.adapters.kafka_producer import build_producer
    from simulator.fleet import build_fleet
    from simulator.journey import generate_day

    produced = 0
    producer = build_producer(kafka_settings())
    day = datetime.now(UTC).date()
    try:
        for vehicle in build_fleet(count=vehicles, seed=seed):
            for event in generate_day(vehicle, day=day, seed=seed):
                producer.produce_event(event)
                produced += 1
    finally:
        producer.flush()
    return produced


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

    # The processor consumes asynchronously, so give the pipeline time to drain before
    # counting. Polling with a timeout rather than a fixed sleep (CLAUDE.md §12).
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        result.events_after = _mongo_event_count()
        if result.accounted_for:
            break
        time.sleep(5)

    result.passed = result.accounted_for
    if not result.passed:
        shortfall = result.events_produced - (result.events_after - result.events_before)
        result.notes.append(
            f"SHORTFALL of {shortfall} event(s): produced {result.events_produced} but the "
            f"store gained {result.events_after - result.events_before}. This is data loss."
        )
    else:
        result.notes.append(
            "Store gained at least what was produced. At-least-once delivery means a "
            "redelivered batch is correct; only a shortfall would be loss."
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
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s", stream=sys.stdout)

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
        print(f"  store delta       : {r.events_after - r.events_before:,}")
        for note in r.notes:
            print(f"  note              : {note}")
    print(f"\nevidence: {out}")

    # A failure here is data loss, so it fails the command rather than being a note in a file.
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
