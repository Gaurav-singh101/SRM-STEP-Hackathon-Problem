# Progress log

Session handoff record (CLAUDE.md S17). Newest first.

---

## 2026-09-29 - Day 1: foundation and domain core

**Plan approved:** four-day schedule, walking skeleton first. See the approved
plan in the session transcript and `docs/OPEN_DECISIONS.md` for decision status.

### Done and verified

| Area | State | Evidence |
|---|---|---|
| Repo scaffold | Monorepo layout per CLAUDE.md S6, git initialised | `git ls-files` |
| Python workspace | uv workspace, 5 packages, one pinned `uv.lock`, Python 3.12.13 | `uv lock --check` |
| Tooling | ruff (lint+format), mypy `--strict`, pytest, coverage gate at 80% | `./make.ps1 lint` |
| Event contract | `schemas/telemetry.raw.v1.json` + Pydantic model, agreement enforced by tests | 32 contract tests |
| Idle segmentation | Streaming state machine with hysteresis and gap tolerance; batch helper folds over it so the two cannot diverge | 32 tests, 5 property-based |
| Fuel and cost | Config burn rates, `fuel_rate_lph` cross-check, `Decimal` money, BEV excluded loudly | 22 tests |
| Utilisation | Composite score, median/MAD with a MAD floor | 26 tests, 4 property-based |
| VIN validation | ISO 3779 check digit, verified against 3 external oracles including the PDF's own VIN | 19 tests, 4 property-based |
| DTC parsing | SAE J2012 system/subsystem decode, idle-related family flagged | 32 tests, 3 property-based |
| Compose stack | Kafka KRaft, Postgres 16, Mongo 7, Redis 7.2 with healthchecks | YAML validated; **not yet run** |
| Makefile + shim | `Makefile` canonical; `make.ps1` for Windows where `make` is absent | `./make.ps1 verify` green |
| CI | GitHub Actions: quality, contract, security, integration (gated off) | YAML validated |
| ADRs | 0001-0005 written | `docs/adr/` |

**Test status:** 164 passed, **95.63% coverage**, gate 80%. Lint, format and
`mypy --strict` all clean. Run: `./make.ps1 verify`.

### Findings worth keeping

- **The MAD floor in ADR-0003 was found by a failing test, not by design.** On a
  tightly clustered vehicle class the raw MAD is tiny, so an unremarkable vehicle
  computed to -3.6 robust sigma and the `abnormal` band filled with false
  positives. Fixed in the domain, not the test.
- **The VIN check digit validates against the PDF's own example VIN**
  (`1HGCM82633A004352`), which is a strong signal the transliteration and weights
  are right. European VINs legitimately fail this check - documented as a known
  limitation in `vin.py`, not a bug.

### Blockers

| # | Blocker | Impact | Fix |
|---|---|---|---|
| **B1** | **Docker Desktop not installed; WSL registration corrupt** (`REGDB_E_CLASS_NOT_REG`). Windows 10 Home, so Hyper-V is unavailable and Docker requires the WSL2 backend. Virtualization *is* enabled in firmware. | Blocks the compose stack, Testcontainers integration tests, and the end-to-end walking skeleton. Pure domain work is unaffected by design (hexagonal layering). | Elevated PowerShell: `wsl --install`, reboot, install Docker Desktop, `wsl --set-default-version 2`. Needs admin and a reboot. |
| **B2** | `confluent-kafka` native import blocked by Windows Application Control (`An Application Control policy has blocked this file`) | Cannot run the Kafka adapter natively on Windows. Does not affect unit tests or the containerised runtime. | Run the processor in its Linux container. Keep the Kafka adapter import lazy so unit tests never load the DLL. |
| **B3** | AWS account is a 6-month free learning account | **100K events/s cannot be demonstrated on it.** EKS control plane is not free-tier eligible and the rate needs ~100 MB/s sustained. | Agreed approach: measure locally with full provenance, prove horizontal scaling with a replica ladder, publish a clearly labelled projection, and deploy small to AWS only to prove the deployment path. |

### Next: finish Day 1

1. Simulator: seeded, vectorised event generation (pure core, Kafka producer as an adapter).
2. Processor: validate to DLQ, per-vehicle high-water-mark dedup, idle machine, batched writes.
3. Postgres schema and migrations; Redis latest-state; Mongo raw with TTL.
4. FastAPI: three endpoints, keyset pagination, OIDC stub.
5. React: Fleet Overview and Idle Cost Leaderboard.
6. Gate 1: one event visibly becomes one idle event and one cost figure in the UI.

Items 2-5 need B1 fixed to be demonstrated end to end. Their pure logic and unit
tests do not.
