# Coverage evidence

**Measured, not claimed.** Produced by a real run of the command below. Refreshed whenever
the figure moves, so nothing here is a number from an earlier state of the repository.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-30 |
| Commit | recorded by the commit that adds this file |
| Command | `uv run pytest -m "not integration" --cov --cov-report=xml:docs/evidence/coverage/coverage.xml --cov-fail-under=80` |
| Interpreter | Python 3.12.13 (uv-managed) |
| Platform | win32, local developer laptop |
| Scope | `libs/` + services; adapters measured but report-only (ruling G7) |

## Result

| Metric | Value | Gate |
|---|---|---|
| **Combined line + branch (the gated figure)** | **84.56%** | **80%** |
| Line coverage only | 85.32% | - |
| Branch coverage only | 80.12% | - |
| Tests passed | **609** | all must pass |
| Deselected (integration) | 49 | run separately, see below |
| Duration | 1293 s | Far slower than an earlier 157 s run on the same suite. Machine was freshly restarted; not investigated, and recorded rather than smoothed over. |

Three numbers, not one, because they are different measurements and quoting only the
highest would be misleading. `--cov-fail-under` compares against coverage.py's combined
line-and-branch figure, which is the one the gate enforces and the one to cite. The
85.32% and 80.12% are the separate `line-rate` and `branch-rate` attributes in
`coverage.xml`: 2,535 of 2,971 lines and 407 of 508 branches.

**The figure has fallen over time, from 95.63% on Day 1.** That is expected rather than a
regression: Day 1 was almost entirely pure domain code, which is cheap to cover
exhaustively, and every service added since brings adapter code that a unit test cannot
reach. The gate is what matters, and it is still clear.

## Where coverage is thin, and why

Stated rather than averaged away. These are the modules a reader should be sceptical about.

| Module | Line coverage | Why, and what covers it instead |
|---|---|---|
| `services/oidc_stub/oidc_stub/app.py` | 0.00% | Local development token issuer. It refuses to start outside local/ci, is never deployed, and is exercised end to end every time the dashboard signs in. |
| `services/batch/batch/seed.py` | 0.00% | The reference-dataset seeder. Verified by running it - the 200-vehicle and 100K-vehicle datasets it produced are what every other measurement here is based on - but it has no unit tests. **A real gap.** |
| `services/batch/batch/erasure.py` | 32.41% | Covered by `tests/integration/test_erasure.py`, which needs a real PostgreSQL and is therefore among the 42 deselected cases. See `docs/evidence/compliance/erasure-local.md` for a live run. |
| `services/batch/batch/rollups.py` | 38.98% | Same: the SQL is the logic, so it is covered by integration tests rather than unit tests. The pure accounting it depends on is separately covered by `test_utilisation_window.py`. |
| `services/api/api/deps.py` | 67.16% | Rate limiter and pool lifecycle. The Redis paths need a real Redis. |

The domain modules - where the algorithms live and where a bug would silently change a
cost figure - are all above 96%: `domain/idle.py` 97.09%, `domain/trips.py` 96.92%,
`domain/utilisation.py` 96.82%, `vin.py` 96.00%.

## Suites that do not appear in this figure

| Suite | Count | Why it is excluded here |
|---|---|---|
| Integration (Testcontainers, real PostgreSQL) | 49 | Needs Docker; run by `make test-integration` |
| BDD acceptance (behave) | 19 scenarios, 105 steps | Separate runner; see `docs/evidence/acceptance/` |

## Caveats

- This is a **local** measurement on a developer laptop, labelled as such per CLAUDE.md §9.
  It is a coverage figure, not a performance figure.
- Coverage is not correctness. Three of the bugs found during development - a field that
  always serialised zero, an alert latency that measured data age, and an `abnormal` band
  that flagged 21.5% of the fleet - were all in covered code.
- Raw machine-readable report: `coverage.xml` in this directory.
