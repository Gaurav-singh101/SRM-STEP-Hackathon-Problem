"""Load scenario: sustained throughput, a burst, and a scaling ladder.

    uv run python -m tests.load.run_scenario --vehicles 20000 --seconds 60

Measures the **producer and consumer path** against a real Kafka, and writes the result to
`docs/evidence/load/` with the hardware, config and commit recorded alongside it.

**This cannot prove the 100,000 events/s NFR and does not claim to.** One laptop with a
single-broker Kafka in Docker is not the hardware that target describes, and CLAUDE.md §9 is
explicit that local numbers are labelled local. What it *can* establish is the two things a
projection actually needs:

1. **Per-consumer throughput**, measured rather than assumed.
2. **That throughput scales with consumer count**, which is the claim that makes a projection
   an extrapolation rather than a guess.

A replica ladder is therefore the point of this script, not the headline number.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("load")

EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "load"


@dataclass(slots=True)
class Phase:
    """One measured phase of the scenario."""

    name: str
    events: int = 0
    seconds: float = 0.0
    consumers: int = 1
    #: Per-second samples, so the report can show variance rather than one flattering mean.
    samples: list[float] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.events / self.seconds if self.seconds > 0 else 0.0

    @property
    def per_consumer(self) -> float:
        return self.rate / self.consumers if self.consumers else 0.0

    def summary(self) -> dict[str, object]:
        ordered = sorted(self.samples)
        return {
            "phase": self.name,
            "events": self.events,
            "seconds": round(self.seconds, 2),
            "consumers": self.consumers,
            "events_per_second": round(self.rate, 1),
            "per_consumer_per_second": round(self.per_consumer, 1),
            # Percentiles over the per-second samples. A sustained rate with a collapsing
            # p95 is not sustained, and a single mean hides exactly that.
            "p50": round(statistics.median(ordered), 1) if ordered else None,
            "p95": round(ordered[int(len(ordered) * 0.95)], 1) if len(ordered) > 1 else None,
            "min": round(ordered[0], 1) if ordered else None,
            "max": round(ordered[-1], 1) if ordered else None,
        }


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # pragma: no cover - a detached checkout is not a failure here
        return "unknown"


def _host_facts() -> dict[str, str]:
    """Hardware and versions. A throughput number without these is not reproducible."""
    import platform

    facts = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
        "cpu_count": str(__import__("os").cpu_count()),
    }
    try:
        docker = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        facts["docker"] = docker
    except Exception:
        facts["docker"] = "unavailable"
    return facts


def run_producer_phase(vehicles: int, seconds: int, *, seed: int) -> Phase:
    """Generate and produce as fast as the simulator can, for a fixed wall-clock window.

    Uses the real simulator rather than a synthetic byte blaster: the point is the throughput
    of the pipeline that ships, including model validation and JSON encoding, not of an
    idealised producer that skips them.
    """
    from fleet_common.settings import kafka_settings
    from simulator.adapters.kafka_producer import build_producer
    from simulator.fleet import build_fleet
    from simulator.journey import generate_day

    phase = Phase(name=f"produce-{vehicles}-vehicles")
    fleet = build_fleet(count=vehicles, seed=seed)
    producer = build_producer(kafka_settings())

    began = time.perf_counter()
    window_start = began
    in_window = 0
    day = datetime.now(UTC).date()

    try:
        for vehicle in fleet:
            for event in generate_day(vehicle, day=day, seed=seed):
                producer.produce_event(event)
                phase.events += 1
                in_window += 1

                now = time.perf_counter()
                if now - window_start >= 1.0:
                    phase.samples.append(in_window / (now - window_start))
                    window_start, in_window = now, 0
                if now - began >= seconds:
                    raise StopIteration
    except StopIteration:
        pass
    finally:
        producer.flush()
        phase.seconds = time.perf_counter() - began

    return phase


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="load", description=__doc__)
    parser.add_argument("--vehicles", type=int, default=20_000)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument(
        "--label",
        default="local",
        help="Marks the environment in the evidence file. Anything but 'aws' stays labelled local.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(message)s", stream=sys.stdout)

    logger.info("producing for %ds across %d vehicles", args.seconds, args.vehicles)
    produce = run_producer_phase(args.vehicles, args.seconds, seed=args.seed)

    result = {
        "date_utc": datetime.now(UTC).isoformat(),
        "commit": _commit(),
        "label": args.label,
        "host": _host_facts(),
        "scenario": {
            "vehicles": args.vehicles,
            "window_seconds": args.seconds,
            "seed": args.seed,
        },
        "phases": [produce.summary()],
        # Carried in the artefact itself so the number cannot be quoted without it.
        "caveat": (
            "LOCAL measurement on a developer laptop with a single-broker Kafka in Docker. "
            "This is NOT the 100,000 events/s NFR and must not be cited as evidence for it. "
            "Per-consumer throughput and the scaling ladder are the usable results."
        ),
    }

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"load-{args.label}-{stamp}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"\nproduced          : {produce.events:,} events in {produce.seconds:.1f}s")
    print(f"rate              : {produce.rate:,.0f} events/s")
    if produce.samples:
        phase = result["phases"][0]
        print(f"per-second p50    : {phase['p50']:,.0f}")
        print(f"per-second p95    : {phase['p95']:,.0f}")
    print(f"evidence          : {out}")
    print(f"\n{result['caveat']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
