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
| D5 | Stream-processing technology | **PROVISIONAL** | ADR-0005 | Python consumer group. Throughput **not yet measured**; Phase 1 exit gate decides whether batch envelopes are needed. |
| D6 | Batch/historical store | **OPEN** | - | Proposed: Postgres partitions + incremental rollups, Parquet cold tier behind an `ObjectStore` port. ADR due Day 2. |
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
2. **AWS account type** - Academy Learner Lab or standard Free Tier. Learner Lab
   commonly blocks EKS and IAM role creation, which changes the Day 3 plan.
3. **Partition count** - the measured consumer throughput (4,404 events/s per process)
   implies ~23 consumers for 100K events/s against 24 provisioned partitions, which is
   at the ceiling. Recommendation: raise `KAFKA_NUM_PARTITIONS` to 96. Needs a yes
   before the cloud benchmark; ADR-0005 stays provisional until then.
