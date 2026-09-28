# Traceability matrix

Requirement -> Feature -> Implementation -> Test -> Demo (CLAUDE.md S15).
A feature is **not done** until it has code, tests, a row here, and evidence
where it affects an NFR.

**Source keys:** `PDF-n` = hackathon problem statement section. `CM-n` = CLAUDE.md
section. `Dn` = open decision.

## Domain and algorithms

| Req | Feature | Implementation | Test | Status |
|---|---|---|---|---|
| PDF-9, D1 | Idle segmentation over noisy signals | `libs/fleet_common/fleet_common/domain/idle.py` | `libs/fleet_common/tests/test_idle.py` | Done |
| D1 | Hysteresis prevents segment shattering on jitter | `idle.py::IdleStateMachine._is_idling` | `test_speed_between_bounds_keeps_an_open_segment_alive` | Done |
| D1 | Gap tolerance bridges missing samples | `idle.py::IdleStateMachine.observe` | `test_gap_within_tolerance_is_bridged` | Done |
| D10 | Excessive-idle transition fires once per segment | `idle.py::_check_excessive` | `test_excessive_fires_exactly_once_per_segment` | Done |
| CM-8 | Out-of-order events reported, never dropped | `idle.py::observe` | `test_out_of_order_event_is_reported_not_applied` | Done |
| D2 | Idle fuel and cost estimation | `domain/fuel.py::estimate_idle_cost` | `test_fuel.py` | Done |
| CM-14 | Every figure carries its assumptions | `fuel.py::IdleCostEstimate` | `test_estimate_carries_its_full_assumption_set` | Done |
| D11 | Money never uses floating point | `fuel.py::to_money` | `test_repeated_addition_does_not_drift` | Done |
| D9 | BEVs excluded from fuel cost, loudly | `fuel.py::BevHasNoIdleFuelError` | `test_bev_idle_fuel_raises_rather_than_returning_zero` | Done |
| D9 | Fleet totals declare BEV exclusions | `fuel.py::FleetCostTotal.is_partial` | `test_fleet_total_flags_bev_exclusions` | Done |
| D3 | Utilisation scoring | `domain/utilisation.py::score_utilisation` | `test_utilisation.py` | Done |
| D3 | Efficient-but-unused vehicle surfaces as low | `utilisation.py` composite | `test_efficient_but_barely_used_vehicle_scores_low` | Done |
| D3 | Robust outlier detection (median/MAD + floor) | `utilisation.py::effective_mad` | `test_mad_floor_prevents_a_false_outlier` | Done |
| PDF-9 | VIN validation, 17 chars, no I/O/Q, check digit | `fleet_common/vin.py` | `test_vin.py` | Done |
| PDF-9 | DTC parsing (P0301 family) | `fleet_common/dtc.py` | `test_dtc.py` | Done |

## Contract and data engineering

| Req | Feature | Implementation | Test | Status |
|---|---|---|---|---|
| PDF-8, D7 | Single source of truth for the event contract | `schemas/telemetry.raw.v1.json` | `test_contract_schema.py` | Done |
| G9 | Schema contract tests in place of Pact for processor/API | `test_contract_schema.py` | 32 cases | Done |
| G10 | Both `event_id` and `seq` required | `schemas/...v1.json`, `events.py` | `test_required_fields_match` | Done |
| CM-8 | Event time is tz-aware UTC and unambiguous | `events.py::_tz_aware_utc` | `test_naive_timestamp_is_rejected` | Done |
| CM-8 | Invalid events rejected at the edge | `events.py` validators | `test_out_of_range_values_are_rejected_by_both_sides` | Done |
| PDF-8 | Partition by `vin`, 24 partitions | ADR-0004, `.env.example` | - | Decided, not yet running |

## Platform (Day 1 remaining, Day 2)

| Req | Feature | Implementation | Test | Status |
|---|---|---|---|---|
| PDF-6 | 100K+ vehicle simulator | `services/simulator/` | - | **Planned** |
| PDF-7 | Stream processing, dedup, batched writes | `services/processor/` | - | **Planned** |
| PDF-6 | Secure paginated API | `services/api/` | - | **Planned** |
| PDF-6 | Web interface, 5 views | `web/` | - | **Planned** |
| PDF-8 | 3NF relational core + ER diagram | `services/batch/migrate`, `docs/architecture/` | - | **Planned** |
| PDF-11, D4 | OIDC/JWT, RBAC, tenant isolation in the data layer | `libs/fleet_common` auth, `services/api` | - | **Planned** |
| PDF-11 | Audit log on every vehicle/location/driver read | `services/api` | - | **Planned** |
| PDF-11 | Right to erasure across all stores | `services/batch` | - | **Planned** |
| PDF-12 | Integration tests via Testcontainers | `tests/integration/` | - | **Blocked on B1** |
| PDF-12 | BDD acceptance scenarios | `tests/acceptance/` | - | **Planned** |
| PDF-11 | Load and soak evidence | `tests/load/`, `docs/evidence/load/` | - | **Planned (Day 3)** |
| PDF-13 | Terraform, Helm, STRIDE | `infra/`, `docs/STRIDE.md` | - | **Planned (Day 3)** |

## Infrastructure and process

| Req | Feature | Implementation | Evidence | Status |
|---|---|---|---|---|
| PDF-13 | One-command local setup | `docker-compose.yml`, `Makefile`, `make.ps1` | YAML validated; run blocked by B1 | Partial |
| PDF-12 | CI on every push | `.github/workflows/ci.yml` | YAML validated | Done |
| PDF-12 | 80%+ coverage on core logic | `pyproject.toml` gate, CI | **95.63%**, 164 tests | Done |
| PDF-13 | 3-5 ADRs | `docs/adr/0001`-`0005` | 5 written | Done |
| CM-4 | Redis pinned to 7.2.x for licence reasons | `docker-compose.yml` | ADR-0002 note | Done |
