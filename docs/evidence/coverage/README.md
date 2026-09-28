# Coverage evidence - Day 1

**Measured, not claimed.** Produced by a real run of the command below.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-28 22:23:10 |
| Commit | recorded by the commit that adds this file |
| Command | `uv run pytest -m "not integration" --cov --cov-fail-under=80` |
| Interpreter | Python 3.12.13 (uv-managed) |
| Platform | win32, local developer laptop |
| Scope | `libs/` + `domain/` + `application/` (ruling G7); adapters report-only |

## Result

| Metric | Value | Gate |
|---|---|---|
| **Combined line + branch (the gated figure)** | **95.63%** | **80%** |
| Line coverage only | 97.01% | - |
| Branch coverage only | 88.98% | - |
| Tests passed | **164** | all must pass |

Three numbers, not one, because they are different measurements and quoting only
the highest would be misleading. `--cov-fail-under` compares against coverage.py's
combined line-and-branch figure (95.63%), which is the one the gate enforces and
the one to cite. The 97.01% and 88.98% are the separate `line-rate` and
`branch-rate` attributes in `coverage.xml`.

## Per-module

| File | Line coverage |
|---|---|
| `domain/fuel.py` | 97.83% |
| `domain/idle.py` | 96.45% |
| `domain/utilisation.py` | 95.07% |
| `dtc.py` | 100.00% |
| `events.py` | 98.85% |
| `testing.py` | 100.00% |
| `vin.py` | 96.00% |

## Caveats

- This is a **local** measurement on a developer laptop, labelled as such per
  CLAUDE.md S9. It is a coverage figure, not a performance figure - no throughput
  or latency number exists yet.
- Adapter and infrastructure code is deliberately outside the gate. At Day 1 the
  services contain no logic yet, so the figure reflects `libs/fleet_common` only.
- Raw machine-readable report: `coverage.xml` in this directory.
