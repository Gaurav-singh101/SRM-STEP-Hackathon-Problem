# Fleet Fuel, Idling & Utilisation Cost Intelligence Platform

Turns high-volume connected-vehicle telemetry into the fuel and utilisation cost
figures a fleet finance team can act on.

Built for the Connected Vehicle Intelligence Hackathon, problem space **Fuel,
Idling & Utilisation Cost**. Motorq is referenced only as an industry example;
this is an independent academic exercise with no affiliation. All data is
synthetic.

> **Status: Day 1 of 4.** The domain core, event contract, tooling and CI are
> complete and verified (164 tests, 95.63% coverage). The services and web UI are
> in progress. Nothing in this repository claims a performance figure that has not
> been measured - see [`docs/PROGRESS.md`](docs/PROGRESS.md) for exactly what is
> done, what is planned, and the three open blockers.

## The questions it answers

- Which vehicles generate idle-related cost, and how much?
- How much estimated fuel and money is tied to inefficient idling?
- Which vehicles show low or abnormal utilisation?
- What are the fleet-level fuel, idle and utilisation trends?
- Which events or vehicles need operational attention right now?

## Architecture

```
Vehicle Simulator ──▶ Kafka ──▶ Stream Processor ──┬──▶ PostgreSQL  (3NF core, rollups, audit)
  100K+ vehicles     24 parts    validate, dedup,  ├──▶ MongoDB     (raw telemetry, TTL hot tier)
  seeded, bursty     key=vin     idle detection,   └──▶ Redis 7.2   (latest state, dedup window)
  out-of-order                   aggregation                │
                                                            ▼
                                            FastAPI  ──▶  React dashboard
                                         OIDC/JWT, RBAC,   5 views
                                         keyset pagination
```

Full diagrams land in `docs/architecture/` on Day 4.

## Quick start

Requires Docker, and [`uv`](https://docs.astral.sh/uv/) for the Python workspace.

```bash
cp .env.example .env
make up
```

`make up` starts Kafka, PostgreSQL, MongoDB and Redis, creates topics, applies
migrations and brings up the services. The API is then on
<http://localhost:8000/docs> and the dashboard on <http://localhost:5173>.

**On Windows**, `make` is not installed by default. Use the equivalent shim, which
runs the same commands:

```powershell
./make.ps1 up
```

### Commands

| Make | Windows | Does |
|---|---|---|
| `make install` | `./make.ps1 install` | Sync the pinned workspace |
| `make up` / `make down` | `./make.ps1 up` / `down` | Start / stop the full stack |
| `make lint` | `./make.ps1 lint` | ruff, ruff format, mypy --strict |
| `make test` | `./make.ps1 test` | Unit tests with the 80% coverage gate |
| `make verify` | `./make.ps1 verify` | Everything CI enforces |
| `make test-integration` | `./make.ps1 test-integration` | Testcontainers integration tests |
| `make sim N=100000` | `./make.ps1 sim -N 100000` | Run the simulator |
| `make load` | - | Load scenario into `docs/evidence/load/` |
| `make cloud-down` | - | Tear down the AWS environment |

Unit tests need no infrastructure at all: the domain layer is pure by design, so
`make test-unit` runs on a bare checkout.

## Repository layout

| Path | Contents |
|---|---|
| `schemas/` | **Single source of truth** for the event contract (JSON Schema) |
| `libs/fleet_common/` | Shared package: event models, pure domain logic, config, adapters |
| `services/simulator/` | Synthetic 100K+ vehicle generator - part of the solution |
| `services/processor/` | Stream processing: validation, dedup, idle detection, aggregation |
| `services/api/` | FastAPI, hexagonal: `domain` / `application` / `adapters` |
| `services/batch/` | Rollups, late-event reconciliation, retention, migrations |
| `web/` | React + TypeScript dashboard |
| `infra/` | Dockerfiles, Helm charts, Terraform (AWS) |
| `tests/` | Integration, contract, acceptance, load, chaos |
| `docs/` | ADRs, architecture, evidence, traceability, threat model |

## Key design decisions

Recorded as ADRs in [`docs/adr/`](docs/adr/); status of every open question is in
[`docs/OPEN_DECISIONS.md`](docs/OPEN_DECISIONS.md).

- **[ADR-0001](docs/adr/0001-idle-definition-and-thresholds.md)** - tiered idle
  thresholds. Hysteresis and gap tolerance matter more than the numbers: without
  them, GPS jitter shatters one real idle period into fragments that each fall
  below the billing threshold and vanish.
- **[ADR-0002](docs/adr/0002-fuel-and-cost-estimation.md)** - configured burn
  rates with a device-reported cross-check. Money is `Decimal`, never float. Every
  figure carries its formula version, burn rate, price and threshold so a finance
  user can reproduce it by hand.
- **[ADR-0003](docs/adr/0003-utilisation-scoring.md)** - composite score, because
  `moving_time / engine_on_time` scores an unused van at 92%. Outliers use
  median/MAD, not mean/stdev, which outliers distort.
- **[ADR-0004](docs/adr/0004-event-contract-topics-and-partitioning.md)** - event
  schema, partition key `vin`, 24 partitions. The hackathon's illustrative event
  has no ignition or fuel signal, so idle detection is impossible from it as given.
- **[ADR-0005](docs/adr/0005-stream-processing-technology.md)** - Python consumer
  group over Flink, for contract reuse across services. **The throughput that
  justifies this is not yet measured, and the ADR says so.**

## Testing

| Suite | Tool | State |
|---|---|---|
| Unit + property-based | pytest, Hypothesis | 164 tests, **95.63%** coverage (gate 80%) |
| Event contract | jsonschema | 32 tests, runs as its own CI job |
| Integration | Testcontainers | Planned Day 2 |
| Acceptance (BDD) | behave | Planned Day 2 |
| Load / soak | k6 or Locust | Planned Day 3 |
| Security | Semgrep, Trivy, ZAP | Semgrep + Trivy wired into CI |
| Chaos | scripted | Planned Day 3 |

The coverage gate applies to `libs/`, `domain/` and `application/`. Adapters are
measured but report-only, so the gate cannot be satisfied by testing
infrastructure glue instead of business rules.

## Honest limitations

Stated here rather than buried, because the alternative is a claim we cannot back:

- **100,000 events/s has not been demonstrated.** The available AWS account is a
  free learning tier, on which that rate (~100 MB/s sustained at the hackathon's
  1 KB/event model) is not reachable. The plan is to measure locally with full
  provenance, prove horizontal scaling with a consumer-replica ladder, and publish
  a clearly labelled projection. A projection will never be presented as a
  measurement.
- **99.9% availability is a target, not a result.** It cannot be demonstrated in a
  four-day window. Reported instead: the mechanisms that support it, plus measured
  recovery time from chaos tests.
- **Idle thresholds, burn rates and utilisation weights are proposed synthetic
  values**, not industry-validated figures.
- **VIN check-digit validation is North American.** Many genuine European VINs
  legitimately fail it; documented in `vin.py`.

## Security and privacy

- No secrets in the repository. `.env.example` contains placeholders only.
- OIDC/JWT with a JWKS-validated signature and an algorithm allow-list; `none` and
  HS256-from-a-JWKS-issuer are rejected. No auth vendor appears in application code.
- Tenant isolation is enforced in the data-access layer, not only in routes.
- Precise coordinates are never written to logs and are masked for roles that do
  not need them.
- Synthetic data only; no real personal, vehicle-owner or real VIN data.

Threat model: `docs/STRIDE.md`. AI and open-source declaration:
[`docs/AI_USAGE.md`](docs/AI_USAGE.md).

## Licence and attribution

Academic exercise. Open-source components and their licences are declared in
[`docs/AI_USAGE.md`](docs/AI_USAGE.md).
