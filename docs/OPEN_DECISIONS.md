# Open decisions

Protocol (CLAUDE.md S5): propose options with trade-offs and a recommendation,
wait for the developer's answer, record an ADR, and only then treat it as final.

**Legend** - `OPEN`: not decided, do not code against it. `DECIDED`: ADR written.
`PROVISIONAL`: decided but resting on an unmeasured assumption.

| # | Decision | Status | ADR | Note |
|---|---|---|---|---|
| D1 | Idle threshold | **DECIDED** | ADR-0001 | Tiered 180/600 s; hysteresis and gap tolerance are the load-bearing parts. Values are PROPOSED. |
| D2 | Fuel-consumption formula | **DECIDED** | ADR-0002 | Config burn-rate table + `fuel_rate_lph` cross-check. `Decimal` money. Rates are PROPOSED. |
| D3 | Utilisation formula | **DECIDED** | ADR-0003 | Composite score, median/MAD with a 10% MAD floor added after testing. |
| D4 | Authentication provider | **OPEN** | - | Interim: local OIDC stub, RS256, vendor swappable by env config. No vendor in code. ADR due when the stub lands. |
| D5 | Stream-processing technology | **DECIDED** | ADR-0005 | Python consumer group. Throughput measured: 4,404 events/s per consumer. Partitions raised 24 -> 96 for headroom; batch envelopes rejected with a reason. |
| D6 | Batch/historical store | **PARTIAL** | ADR-0007 | Reconciliation **decided** 2026-09-30: re-segment the affected vehicle-day inside the Mongo TTL, count and log anything older. Postgres partitions + incremental rollups in place. **Cold tier still open** - Parquet behind an `ObjectStore` port would narrow the TTL gap. |
| D7 | Telemetry event schema | **DECIDED** | ADR-0004 | Extends the PDF example, which has no ignition/fuel signals. |
| D8 | Kafka hosting on AWS | **OPEN** | - | Proposed: Strimzi on EKS for portability and cost; MSK as a Terraform module flag. Constrained by the free-tier account - see PROGRESS blocker B3. |
| D9 | EV handling | **OPEN** | - | Proposed: ICE/hybrid in scope for fuel cost, BEVs utilisation-only, never silently summed into a fleet total. Partly implemented in ADR-0002; ADR due. |
| D10 | "Critical alert" definition | **OPEN** | - | Proposed: `EXCESSIVE_IDLE_IN_PROGRESS` and `IDLE_COST_SPIKE`. SLO measured as `alert_persisted_ts - triggering_event_ts`. The state machine already emits the transition once per segment. |
| D11 | Currency, fuel price, tenancy | **PARTIAL** | ADR-0002 | Money representation decided (`Decimal`, `numeric(14,4)`, INR default). Temporal price table and shared-schema + RLS tenancy still need an ADR. |

## Rulings carried from review

| Ref | Ruling | Status |
|---|---|---|
| G7 | Coverage gate at 80% on `libs/` + `domain/` + `application/`; adapters report-only | Applied in `pyproject.toml` and CI |
| G9 | Pact replaced by schema contract tests for the processor/API pair; Pact retained for web to API | Applied, `test_contract_schema.py` |
| G10 | Require **both** `event_id` and `seq` on every event | Applied in ADR-0004 and the schema |

## Still needed from the developer

1. ~~The Solution Document Template~~ - **supplied** 2026-09-29, 17 sections confirmed
   at `docs/source/Motorq_Hackathon_Solution_Document_Template.md` (gap G13 closed).
2. ~~AWS account type~~ - **resolved** 2026-09-30: **there is no AWS account.** This
   changes nothing, because the approach in item 4 never applied the Terraform. What is
   lost is only what a free-tier account could not have delivered either: the 100K
   events/s benchmark on cloud hardware (PROGRESS blocker B3). `terraform validate` and
   `fmt` need no credentials; `plan` and `apply` do and are out of scope. The submission
   states this plainly rather than implying a deployment happened.
3. ~~Partition count~~ - **decided** 2026-09-29: raised to 96 (ADR-0005 amendment).
4. **Cloud deployment approach** - **decided** 2026-09-29, reconfirmed 2026-09-30 now
   that no AWS account exists: write and validate Terraform for AWS but do not apply it,
   and verify the Helm charts on a real local Kubernetes cluster (Docker Desktop's
   built-in Kubernetes, chosen 2026-09-30 over k3d because it needs no extra install). The hackathon asks for "Terraform for at least one cloud" and an
   architecture "deployable on at least one cloud", not a running cloud environment.
   The available account is a free learning tier, so applying it is not viable. This
   will be stated plainly in the solution document rather than implied.

5. ~~Late-event reconciliation~~ - **decided** 2026-09-30, ADR-0007: re-segment the
   affected vehicle-day from the complete raw sample set inside the MongoDB TTL; record
   and count anything older as unreconcilable rather than recomputing from partial data.

## Awaiting a developer action (not a decision)

- **Enable Kubernetes in Docker Desktop** (Settings -> Kubernetes -> Enable -> Apply) so
  the Helm charts can be verified against a real cluster rather than only linted.
  `kubectl` v1.36.1 is already present; no cluster context exists yet.
