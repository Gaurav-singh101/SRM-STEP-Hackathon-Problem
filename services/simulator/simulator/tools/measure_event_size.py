"""Measure the wire size of generated telemetry and write the evidence file.

Exists so the figure in ``docs/evidence/load/event-size.md`` is reproducible by
anyone reading it, rather than a number someone once ran in a shell. CLAUDE.md S3
allows a figure in the documentation only when it comes from a real run; this is
that run.

    uv run python -m simulator.tools.measure_event_size
"""

from __future__ import annotations

import argparse
import statistics
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import orjson

from simulator.fleet import build_fleet
from simulator.journey import generate_day

DEFAULT_SEED = 11
DEFAULT_VEHICLES = 200
NFR_EVENTS_PER_SECOND = 100_000
#: The PDF S4 reference model, quoted alongside ours for comparison.
PDF_BYTES_PER_EVENT = 1024

OUTPUT = Path("docs/evidence/load/event-size.md")


def measure(vehicles: int, seed: int) -> list[int]:
    """Serialise one simulated day per vehicle and return every event's size."""
    fleet = build_fleet(vehicle_count=vehicles, seed=seed)
    rng = np.random.default_rng(seed)
    day = datetime(2026, 9, 29, 5, 0, 0, tzinfo=UTC)

    sizes: list[int] = []
    for vehicle in fleet:
        events, _ = generate_day(vehicle, day, rng=rng)
        sizes.extend(len(orjson.dumps(event)) for event in events)
    return sizes


def render(sizes: list[int], vehicles: int, seed: int) -> str:
    n = len(sizes)
    mean = sum(sizes) / n
    ordered = sorted(sizes)
    p95 = ordered[int(n * 0.95)]
    mbs = mean * NFR_EVENTS_PER_SECOND / 1e6
    pdf_mbs = PDF_BYTES_PER_EVENT * NFR_EVENTS_PER_SECOND / 1e6

    return f"""# Event size measurement

**Measured, not estimated.** Reproduce with the command at the bottom.

| Field | Value |
|---|---|
| Date (UTC) | {datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")} |
| Commit | recorded by the commit that adds this file |
| Platform | local developer laptop, win32 |
| Serialiser | orjson, the same call the producer hot path uses |
| Seed | {seed} (deterministic: a rerun gives identical bytes) |
| Sample | {n:,} events from {vehicles} vehicles over one simulated day |

## Result

| Metric | Bytes |
|---|---|
| Mean | **{mean:.1f}** |
| Median | {statistics.median(sizes):.0f} |
| p95 | {p95} |
| Min | {min(sizes)} |
| Max | {max(sizes)} |

The distribution is tight because the field set is fixed; only numeric formatting
and the optional `dtc` list vary.

## Projection to the NFR rate

At the hackathon's {NFR_EVENTS_PER_SECOND:,} events/s target:

| Basis | Bytes/event | Throughput | Per day |
|---|---|---|---|
| **Measured (this system)** | **{mean:.0f}** | **{mbs:.1f} MB/s** | **{mbs * 86400 / 1e6:.2f} TB** |
| PDF S4 reference model | {PDF_BYTES_PER_EVENT} | {pdf_mbs:.1f} MB/s | {pdf_mbs * 86400 / 1e6:.1f} TB |

This is arithmetic on a measured event size, **not** a measured throughput. No
throughput figure exists yet; ADR-0005 states that explicitly.

## Why it matters

It corrects a documented estimate. ADR-0004 originally projected 250-300 B/event
and 2.2-2.6 TB/day from a hand estimate; the real figure is about 60% larger. The
operational conclusion is unchanged - the Mongo hot tier needs a 48 h TTL rather
than week-long retention - but the number is now evidence rather than a guess.

## Reproduce

```bash
uv run python -m simulator.tools.measure_event_size
```
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vehicles", type=int, default=DEFAULT_VEHICLES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="print the summary without rewriting the evidence file",
    )
    args = parser.parse_args(argv)

    sizes = measure(args.vehicles, args.seed)
    report = render(sizes, args.vehicles, args.seed)

    mean = sum(sizes) / len(sizes)
    print(f"{len(sizes):,} events, mean {mean:.1f} bytes")
    print(f"at {NFR_EVENTS_PER_SECOND:,} events/s: {mean * NFR_EVENTS_PER_SECOND / 1e6:.1f} MB/s")

    if not args.stdout_only:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(report, encoding="utf-8")
        print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
