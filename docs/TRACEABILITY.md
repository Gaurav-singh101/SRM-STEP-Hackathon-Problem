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

## Pipeline

| Req | Feature | Implementation | Test | Status |
|---|---|---|---|---|
| PDF-6, PDF-8 | 100K+ vehicle simulator with faults | `services/simulator/` | `test_fleet.py`, `test_journey.py` | Done |
| PDF-7 | Bursty, out-of-order, duplicate generation | `simulator/journey.py` | `test_duplicates_are_injected_and_are_exact_copies` | Done |
| PDF-7 | Idempotent, schema-validated intake | `processor/validation.py` | `test_pipeline.py` DLQ cases | Done |
| PDF-7, CM-8 | Streaming dedup, exact within a replay window | `processor/dedup.py` | `test_dedup.py` (7 property-based) | Done |
| PDF-7 | Real-time detection, batched writes | `processor/pipeline.py`, `adapters/sinks.py` | `test_pipeline.py` | Done |
| CM-8 | Late events to a topic, never dropped | `processor/dedup.py`, `adapters/kafka_io.py` | `test_event_beyond_the_lateness_window_is_still_not_dropped` | Done |
| CM-8 | Offsets committed only after persistence | `processor/__main__.py` | end-to-end run | Done |
| D10 | Excessive-idle alert with suppression | `pipeline.py::_excessive_alert` | `test_excessive_idle_raises_exactly_one_alert` | Done |
| PDF-8 | 3NF relational core, partitioned | `services/batch/batch/migrations/` | `test_migrate.py`, integration | Done |
| PDF-13 | Seeded 100K-vehicle dataset | `services/batch/batch/seed.py` | 100,000 rows verified | Done |

## API and web

| Req | Feature | Implementation | Test | Status |
|---|---|---|---|---|
| PDF-6, CM-10 | Secure, paginated, versioned API | `services/api/api/main.py` | `test_api.py` (60 cases) | Done |
| CM-3, CM-10 | Keyset pagination with opaque cursors | `fleet_common/pagination.py` | `test_pagination.py` (4 property-based) | Done |
| CM-10 | RFC 7807 problem responses, no leaked detail | `fleet_common/errors.py` | `test_problem_document_has_the_rfc_7807_shape` | Done |
| PDF-11, D4 | OIDC/JWT with an algorithm allow-list | `fleet_common/auth.py`, `services/oidc_stub/` | `test_api.py` auth cases | Done |
| PDF-11 | RBAC, tenant from the verified token only | `fleet_common/auth.py::Principal` | `test_tenant_cannot_be_overridden_by_a_query_parameter` | Done |
| CM-10 | Tenant isolation in the data-access layer | `api/adapters/repository.py::_scoped` | `test_tenant_isolation.py` (18 cases) | Done |
| PDF-11 | Audit log on every data read | `repository.py::record_access` | `test_every_data_read_writes_an_audit_entry` | Done |
| PDF-11, CM-11 | Location masking by role | `auth.py::coarsen_location` | `test_finance_role_receives_coarsened_coordinates` | Done |
| PDF-11, CM-11 | Right to erasure across PostgreSQL, MongoDB and Redis | `services/batch/batch/erasure.py`, `batch erase-driver` | `test_erasure.py`, `docs/evidence/compliance/erasure-local.md` | Done |
| PDF-11 | Audit trail stays append-only while erasure works | `migrations/007_erasure_role.sql` (column-level grant) | `test_erasure.py` privilege-boundary cases | Done |
| CM-10 | Redis-backed rate limiting with Retry-After | `api/deps.py::RateLimiter` | `test_api.py` | Done |
| CM-10 | Graceful degradation without Redis | `main.py::readyz` | `test_readyz_reports_degraded_without_redis` | Done |
| CM-10 | Health and readiness probes | `main.py::healthz`, `readyz` | `test_api.py` | Done |
| D11 | Money exact end to end, never a float | `schemas.py` serialisers | `test_money_is_serialised_as_a_string` | Done |
| CM-14 | Every figure shows its assumptions | `schemas.py::Assumptions`, `components/common.tsx` | `test_summary_declares_its_assumptions` | Done |
| PDF-12 | BDD acceptance scenarios for the key user stories | `tests/acceptance/features/` | 4 features, 19 scenarios, 105 steps; `docs/evidence/acceptance/` | Done |
| CM-14 | Utilisation coverage stated, never implied | `utilisation_window` table, `UtilisationCoverage` | `test_utilisation_window.py` (7 unit + 7 integration) | Done |
| D10 | Alert latency split from data age | `AlertRow.processing_latency_seconds` | `test_a_replayed_alert_is_flagged_...` | Done |
| D9 | Partial totals declared, BEVs never zeroed | `schemas.py`, `PartialTotalNotice` | `test_summary_flags_a_partial_cost_total` | Done |
| CM-14 | Fleet Overview view | `web/src/pages/FleetOverview.tsx` | - | Done |
| CM-14 | Idle Cost Leaderboard view | `web/src/pages/IdleLeaderboard.tsx` | - | Done |
| CM-14 | Loading, empty and error states everywhere | `web/src/components/common.tsx` | - | Done |
| CM-7 | One API client, shared components | `web/src/api/client.ts` | - | Done |
| CM-14 | Vehicle Detail view | `web/` | - | **Planned (Day 2)** |
| CM-14 | Utilisation view | `web/` | - | **Planned (Day 2)** |
| CM-14 | Attention / Alerts view | `web/` | - | **Planned (Day 2)** |

## Still to build

| Req | Feature | Target | Status |
|---|---|---|---|
| D3 | Utilisation scoring wired into rollups | `services/batch` | **Planned (Day 2)** |
| D6 | Rollups and late-event reconciliation | `services/batch` | **Planned (Day 2)** |
| PDF-9 | Geohash depot grouping, top-K, DTC surfacing | `libs/`, `web/` | **Partial** - DTC parsing done |
| PDF-11 | Load, soak and chaos evidence | `docs/evidence/` | **Planned (Day 3)** |
| PDF-13 | Terraform, Helm, STRIDE | `infra/`, `docs/STRIDE.md` | **Planned (Day 3)** |
| PDF-13 | Solution Document, ER diagram, demo video | `docs/` | **Planned (Day 4)** |

## Infrastructure and process

| Req | Feature | Implementation | Evidence | Status |
|---|---|---|---|---|
| PDF-13 | One-command local setup | `docker-compose.yml`, `Makefile`, `make.ps1` | all four stores healthy | Done |
| PDF-13 | Containerised services | `infra/docker/python-service.Dockerfile` | non-root, multi-stage, builds | Done |
| PDF-12 | CI on every push | `.github/workflows/ci.yml` | quality, contract, security, integration | Done |
| PDF-12 | 80%+ coverage on core logic | `pyproject.toml` gate, CI | **85.75%**, 432 tests | Done |
| PDF-13 | 3-5 ADRs | `docs/adr/0001`-`0006` | 6 written | Done |
| CM-3 | No secrets in the repository | `.env.example`, NOLOGIN DB roles | `test_no_migration_contains_a_hardcoded_password` | Done |
| CM-4 | Redis pinned to 7.2.x for licence reasons | `docker-compose.yml` | ADR-0002 note | Done |
