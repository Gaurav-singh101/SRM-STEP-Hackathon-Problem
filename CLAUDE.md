# CLAUDE.md: Fleet Fuel, Idling & Utilisation Cost Intelligence Platform

Read this file fully at the start of every session. It is the standing contract between the developer (single-member team) and Claude.
If anything here conflicts with a chat message, stop and ask which one wins.

---

## 0. Prime Directives (highest priority)

1. **Depth beats breadth.** One problem space, built to production quality. Do not add features that were not asked for.
2. **Never invent decisions.** Anything marked TBD in section 5 must be proposed, confirmed by the developer, and recorded as an ADR before it is coded as final.
3. **Never fabricate numbers.** No performance, coverage, latency, cost-saving or accuracy figure may appear in code, docs or README unless it comes from a file in `docs/evidence/` produced by a real run. Targets are targets; results are results.
4. **Never hard-code secrets.** No passwords, keys, tokens or credentials in code, config, tests, docs or git history. Use `.env.example` (placeholders only) and env vars / K8s Secrets / AWS Secrets Manager.
5. **Synthetic data only.** No real personal, vehicle-owner or real VIN data anywhere.
6. **Plan before coding.** For any task larger than a small fix: present a short plan (files touched, approach, tests, risks) and wait for approval.
7. **Ask instead of guessing** when a requirement is ambiguous. Ask at most 3 focused questions at a time and offer a recommended default for each.
8. **Reuse before writing.** Search `libs/` and existing modules first (see section 7).
9. **Everything must be traceable:** Requirement, Feature, Implementation, Test, Demonstration (section 15).

---

## 1. Project Snapshot

- **Hackathon:** Connected Vehicle Intelligence Hackathon (industry reference: Motorq; no affiliation, independent academic exercise).
- **Problem space (official):** Fuel, Idling & Utilisation Cost.
- **Primary users:** Fleet Finance Manager, Fleet Operations Manager. **Secondary (to validate):** Fleet Supervisor, Operations Analyst.
- **Value chain:** Telemetry → Behaviour Detection → Fuel/Cost Impact → Fleet Decision.
- **Questions the product must answer:**
  - Which vehicles generate idle-related cost?
  - How much estimated fuel/cost is tied to inefficient idling?
  - Which vehicles show low or abnormal utilisation?
  - What are the fleet-level fuel, idle and utilisation trends?
  - Which events or vehicles need operational attention?
- **Team:** one developer. Keep scope, tooling and process lean, but do not skip required deliverables.

**Pipeline:** `Vehicle Simulator → Kafka → Stream Processing → PostgreSQL / MongoDB / Redis → FastAPI → React`

---

## 2. Source-of-Truth Documents

Located in `docs/source/` (originals are read-only; prefer the `.md` conversions to save tokens):

| File | Purpose |
|---|---|
| `Motorq_Hackathon_Problem_Statement` (PDF) | Official hackathon requirements, NFRs, testing, deliverables, rules |
| `01_PROJECT_OVERVIEW` (docx) | Project objective, approved tech decisions, scope |
| `02_PROBLEM_STATEMENT` (docx) | Selected problem, users, success definition |

**Information classification (label facts this way in docs and ADRs):**
- **Hackathon requirement:** derived from the PDF.
- **Approved project decision:** explicitly chosen (section 4).
- **TBD:** not decided (section 5).
- **Measured result:** only after a real test, with evidence file.

Precedence when documents disagree: Hackathon PDF > 01/02 docs > this file > chat suggestions. Report any conflict; do not silently resolve it.

---

## 3. Hard Rules (Never Do)

- Never add an unapproved technology to the core stack without an ADR and developer approval (approved list: section 4).
- Never implement ML/agentic-AI/vector features unless the developer explicitly approves them (they are out of scope by default).
- Never build authentication against one specific vendor. Code to generic OIDC/JWT (section 5).
- Never write per-event database round trips in the hot path. Always batch.
- Never trust `tenant_id`, role or user id from request bodies or query strings; take them from the validated token.
- Never use offset pagination on large tables. Use keyset pagination.
- Never log raw precise locations, tokens, or secrets. Mask or omit them.
- Never bypass, weaken or delete a failing test to get green. Fix the cause or ask.
- Never commit generated data files, build output, `.env`, or Terraform state.
- Never claim "no data loss", "exactly-once", "99.9%" or any guarantee without the test that proves it and its evidence file.
- Never use AWS-specific SDKs inside business logic (section 13).

---

## 4. Approved Technology Decisions

| Area | Decision | Notes |
|---|---|---|
| Event streaming | Apache Kafka | KRaft mode; idempotent producer, `acks=all`, replication factor 3 in cloud |
| Relational DB | PostgreSQL | Core business data, rollups, audit |
| NoSQL | MongoDB | See proposed data placement (section 8) |
| Cache / hot state | **Redis 7.2 and prior (legacy open source)** | Pin `redis:7.2.x`. Do NOT upgrade: 7.4+ changed licence. No Redis modules (Bloom, JSON, Search) |
| Backend API | FastAPI (Python) | Pydantic v2, async where it helps |
| Frontend | React | TypeScript strictly |
| Cloud | AWS | IaC in Terraform, inside `infra/` only |
| Orchestration | Kubernetes | Helm charts; EKS on AWS |
| Containers | Docker / docker compose | One-command local setup |

**Dev tooling defaults (changeable via ADR):** Python 3.12, `uv` or `pip-tools` with pinned lockfile, `ruff` (lint+format), `mypy --strict` on `libs/` and domain code, `pytest`, Vite + TypeScript + ESLint + Vitest for web, GitHub Actions for CI.

---

## 5. Open Decisions (TBD): Do Not Invent

Track in `docs/OPEN_DECISIONS.md`. Protocol for every item: **(1)** propose 2-3 options with trade-offs and a recommendation, **(2)** wait for the developer's answer, **(3)** record an ADR in `docs/adr/`, **(4)** only then treat it as final.

| # | Decision | Status | Interim rule until decided |
|---|---|---|---|
| D1 | Idle threshold (min stationary duration; engine-on/speed conditions) | TBD | Implement as config parameter `IDLE_MIN_SECONDS`, no magic numbers; default value labelled `PROPOSED` |
| D2 | Fuel-consumption formula (idle burn rate, per fuel type / vehicle class) | TBD | Isolated pure function in `domain/fuel.py`, versioned (`formula_version`), parameters from config |
| D3 | Utilisation formula (what counts as "low/abnormal") | TBD | Isolated pure function in `domain/utilisation.py`, versioned, config-driven |
| D4 | Authentication provider | TBD | Generic OIDC/JWT validation via JWKS + issuer/audience env config. Tests use a local test-only signer. Do not pick a vendor |
| D5 | Stream-processing technology | TBD, must not be silently chosen | Propose options (e.g. Python consumer group with `confluent-kafka` micro-batching vs Flink vs Kafka Streams) with throughput reasoning for 100K events/s |
| D6 | Batch/historical analytics store & method | TBD | Propose (e.g. partitioned PostgreSQL rollups + optional Parquet) |
| D7 | Telemetry event schema fields | TBD | The PDF example has no ignition/RPM/fuel fields; idling needs them. Propose the schema before writing the simulator |
| D8 | Kafka hosting on AWS (self-managed Strimzi vs MSK) | TBD | Prefer portable option; justify |
| D9 | EV handling (idle "fuel" does not apply to BEVs) | TBD | Default proposal: ICE/hybrid in scope for fuel cost; EVs tracked for utilisation only |
| D10 | Definition of "critical alert" (5 s target) | TBD | Propose 1-2 alert types tied to idle/utilisation |
| D11 | Currency, fuel price source, multi-tenant model | TBD | Config-driven; synthetic price table |

If a task depends on an unresolved D-item, stop and ask. Do not proceed on an assumption.

---

## 6. Repository Layout (monorepo)

```
.
├── CLAUDE.md
├── README.md                  # one-command setup, architecture, how to run tests
├── Makefile                   # single entry point: make up / test / lint / load / down
├── docker-compose.yml         # profiles: core, obs, load
├── .env.example               # placeholders only
├── schemas/                   # SINGLE source of truth for event contracts (JSON Schema/Avro)
├── libs/fleet_common/         # shared Python package (section 7)
├── services/
│   ├── simulator/             # 100K+ vehicle generator (part of the solution)
│   ├── processor/             # stream processing (validation, dedup, idle detection, aggregation)
│   ├── api/                   # FastAPI (hexagonal: domain / application / adapters)
│   └── batch/                 # historical rollups / backfill jobs
├── web/                       # React + TypeScript dashboard
├── infra/
│   ├── docker/                # shared base images
│   ├── helm/                  # umbrella + shared library chart
│   └── terraform/aws/
├── tests/                     # integration, contract, acceptance (behave), load, chaos
├── docs/
│   ├── source/                # original PDF/docx + .md conversions
│   ├── adr/                   # 3-5 minimum, numbered 0001-...
│   ├── architecture/          # architecture diagram, ER diagram (3NF)
│   ├── evidence/              # load/, sql/, security/, chaos/, coverage/
│   ├── solution-document/
│   ├── TRACEABILITY.md
│   ├── OPEN_DECISIONS.md
│   ├── PROGRESS.md            # session handoff log
│   ├── AI_USAGE.md            # declaration of AI/OSS tools used
│   └── STRIDE.md
└── .github/workflows/         # reusable workflows; CI on every push
```

---

## 7. Reusability & Code Quality (critical)

**Structure**
- Shared code lives in `libs/fleet_common/` (installable package): settings, structured logging, OpenTelemetry setup, error types + RFC 7807 problem responses, keyset-pagination helpers, auth/JWT validation, RBAC + tenant guard, Kafka producer/consumer wrappers, retry/backoff, circuit breaker, DB/Redis/Mongo client factories, VIN + DTC validators, event models, time utilities.
- **Rule of three:** duplicate once if needed; on the third use, extract to `libs/`. Before writing any helper, grep `libs/` first.
- **One event contract:** Pydantic models are generated from or validated against `schemas/`. The simulator, processor, API and tests import the same definitions. Never redefine field names or constants elsewhere.
- **Hexagonal layering:** `domain` (pure logic, no I/O) → `application` (use cases) → `adapters` (Kafka, Postgres, Mongo, Redis, HTTP). Domain code never imports frameworks or drivers, so it is easy to unit test.
- **Pure domain functions** for: idle segmentation, fuel/cost estimation, utilisation scoring, VIN validation, DTC parsing, dedup logic. Deterministic, no clock/random inside (inject them).
- **Config via environment only** (`pydantic-settings`), one settings class per service extending a common base. No constants scattered in code; business parameters (thresholds, prices, burn rates) live in config with `formula_version`.
- **Shared infrastructure:** one parameterised Dockerfile pattern (multi-stage, non-root, pinned base digest), one Helm library chart used by all services, reusable GitHub Actions workflows, one Makefile.
- **Frontend reuse:** shared API client (generated from OpenAPI), shared table/filter/chart/KPI-card components, one data-fetching layer (TanStack Query), one design-token file. No per-page copies.

**Quality bar**
- Fully typed (`mypy --strict` on libs/domain; TS `strict`). Small functions with one purpose. No dead code, no commented-out blocks, no TODOs without an issue reference in `docs/PROGRESS.md`.
- Errors: explicit, typed, never swallowed. Every external call has a timeout; retries only for idempotent operations, with jittered backoff and a cap.
- Public functions and modules get short docstrings stating *why*, not restating the code.
- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `perf:`). Small, focused commits. Never commit a red build.
- Prefer boring, well-known solutions. Every added dependency must be justified (licence, maintenance, size) and declared in `docs/AI_USAGE.md` under "Open-source used".

---

## 8. Data & Event Design

**Event contract (schema TBD per D7). Regardless of final fields, every event MUST carry:** `event_id` or (`vin`, `seq`), `vin` (validated), `ts` (event time, UTC ISO-8601), `schema_version`, plus signals needed for idling: ignition/engine-state, speed, and fuel level or rate, location (lat/lon). Include optional `dtc[]` and `evt`.

**Time & ordering**
- Distinguish **event time** (`ts`) from **processing time**. Business logic uses event time.
- Partition key = `vin` (ordering per vehicle, even hashing to avoid hot spots). Partition count chosen and justified in an ADR.
- Out-of-order: allowed-lateness window as config; late events beyond the window go to a `telemetry.late` topic and are reconciled by the batch job. Never silently drop.
- Invalid events (schema, VIN check digit, impossible values) go to a DLQ topic with a reason code. Never crash the consumer on bad input.

**Idempotency & delivery**
- Delivery is **at-least-once + idempotent writes** (effectively-once outcome). Consumers commit offsets only after successful batch persistence.
- Dedup approach must scale to 100K events/s. Do not store one Redis key per event. Prefer per-vehicle high-water mark / small recent-window per vehicle plus a unique key (`vin`, `ts`/`seq`) in the sink as final safety net. Record the choice in an ADR.

**Proposed data placement (confirm via ADR, then treat as final):**

| Store | Holds | Why | CAP/PACELC stance |
|---|---|---|---|
| PostgreSQL | Tenants, fleets, vehicles, drivers, users/roles, subscriptions, trips, idle events summary, alerts, rollup tables/materialised views, **audit logs** | ACID, relations, reporting queries. 3NF core | CP; favours consistency over latency |
| MongoDB | Raw/enriched telemetry (time-series collection or bucketed by vehicle+time, TTL for hot tier), flexible per-OEM payload extras | High write rate, schema flexibility | AP-leaning; eventual consistency acceptable |
| Redis (7.2) | Latest vehicle state, per-vehicle idle state machine, dedup window, API response cache, rate-limit counters | Sub-ms hot state | AP; cache is disposable and rebuildable |
| Object storage / Parquet | Cold tier (only if D6 chooses it) | Cheap historical | n/a |

**Relational core:** design in **3NF**; document every deliberate denormalisation (e.g. rollup tables) with reason. Provide ER diagram in `docs/architecture/`.

**Lifecycle (must be documented with retention + cost estimate):** hot (Redis / recent Mongo) → warm (Mongo/Postgres partitions) → cold (compressed/archived). Partition Postgres large tables by time (and vehicle hash if needed); automate partition creation and drop.

---

## 9. Performance Rules

- Targets (Hackathon requirement, not results): sustain 100K+ events/s, survive 3x burst for 5 min with no loss, ingest-to-dashboard < 2 s, critical alert < 5 s, API p95 < 200 ms / p99 < 500 ms, 99.9% availability.
- Batch everything in the hot path: consumer poll batches, bulk writes (`COPY` / `executemany` / `insert_many` / Redis pipelines). Use `orjson`, avoid per-event object churn, avoid blocking calls in async code.
- Scale by consumer-group replicas and partitions, never by code changes.
- Apply back-pressure: bounded queues, pause/resume partitions, no unbounded memory growth. Measure consumer lag.
- Simulator must not become the bottleneck: generate vectorised/batched, deterministic with a seed. Generates bursts, duplicates, out-of-order events, trips with stops and idle periods, faults and noise, at 100K+ vehicles.
- SQL: for every slow query keep **EXPLAIN (ANALYZE, BUFFERS) before and after** in `docs/evidence/sql/` using composite/partial indexes, materialised views, keyset pagination, no ORM N+1. Always report complexity where an algorithm is involved.
- Do not micro-optimise without a measurement. Profile first, and store the profile/benchmark as evidence.
- Local laptop cannot prove 100K events/s. Label local numbers as local, and run the official benchmark on the AWS environment. Record hardware, config, commit SHA and date with every result.

---

## 10. API Rules (FastAPI)

- Versioned under `/api/v1`, OpenAPI is the contract; the frontend client is generated from it.
- Keyset pagination with opaque cursors on all list endpoints; hard maximum page size.
- Rate limiting (Redis-backed) per client/tenant; return `429` with `Retry-After`.
- AuthN: OIDC/JWT (validate signature via JWKS, `iss`, `aud`, `exp`). AuthZ: RBAC + **tenant isolation enforced in the data-access layer**, not only in routes.
- Consistent errors (RFC 7807 problem+json). Never leak stack traces or internal details.
- Validate all input with Pydantic; allow-list sort/filter fields (no dynamic SQL from user input; parameterised queries only).
- Every endpoint that reads vehicle/location/driver data writes an **audit log** entry (who, what, when, tenant, correlation id).
- Cache with explicit invalidation strategy (documented); graceful degradation when Redis/Mongo is down (return partial or stale data with a flag, never a 500 storm). Circuit breakers on downstream calls.
- Health endpoints: `/healthz` (liveness), `/readyz` (readiness, checks dependencies).

---

## 11. Security & Compliance

- Follow OWASP Top 10 and OWASP API Security Top 10. Maintain `docs/STRIDE.md`.
- Transport: TLS 1.3 in transit; mTLS for device/simulator-to-broker path (simulate and document); AES-256 at rest (volume/DB encryption via cloud config).
- Secrets: never in repo; K8s Secrets sourced from AWS Secrets Manager (or equivalent external secret store).
- Containers: non-root, read-only root FS where possible, pinned image digests, no `latest` tags.
- **Location privacy:** mask/coarsen location data for roles that do not need precision; never write precise coordinates to logs.
- **Right to erasure:** a tested flow that removes/anonymises a driver's data across Postgres, Mongo, Redis and derived rollups, with an audit record of the erasure itself.
- **Retention:** configurable per data class, enforced by jobs, and documented.
- Compliance frame: GDPR and India DPDP. Synthetic data only (Hackathon rule).

---

## 12. Testing Standards (heavily weighted; CI on every push)

| Type | Minimum evidence | Tools |
|---|---|---|
| Unit | **80%+ coverage** on core services and algorithms; CI fails below the gate | pytest, pytest-cov, Vitest |
| Integration | Real Kafka, Postgres, Mongo, Redis via **Testcontainers** | pytest + testcontainers |
| Contract | Consumer-driven contracts between services (API to web, processor to API) | Pact |
| Acceptance | BDD scenarios for the key user stories | behave |
| Performance/load | 100K events/s scenario; report throughput, p95/p99, consumer lag; **one soak test** | k6 or Locust |
| Security | SAST, DAST, dependency + image scans | Semgrep, OWASP ZAP, Trivy |
| Compliance & chaos | Verify audit trail + erasure; kill a pod and a broker, prove recovery | scripted chaos tests |

Rules:
- Write tests alongside code (same PR/commit series). Test behaviour and edge cases (duplicates, out-of-order, late events, malformed input, empty fleets, tenant leakage attempts).
- Use property-based tests (Hypothesis) for algorithms: idle segmentation, VIN validation, dedup.
- Tests are deterministic (seeded, injected clock). No sleeps for synchronisation; poll with timeout.
- Save reports under `docs/evidence/<type>/` with date and commit SHA.

---

## 13. DevOps, Cloud-Agnostic & Observability

- `make up` starts the whole stack locally with a seeded 100K-vehicle reference dataset (vehicles, fleets, drivers, tariffs). `make test`, `make lint`, `make load`, `make down` exist and are documented in README.
- Cloud-agnostic rule: application code depends only on env config and standard protocols (Kafka, Postgres, Mongo, Redis, S3-compatible API behind an interface). **All AWS specifics live in `infra/terraform/aws` and Helm values.** Switching cloud means new IaC, not app changes.
- CI/CD (GitHub Actions): lint, type-check, unit, integration, contract, security scans, image build + Trivy, coverage upload. Tag the final commit `v1.0-submission`.
- Observability: JSON structured logs with correlation ids; OpenTelemetry traces; Prometheus metrics (events/s, consumer lag, end-to-end latency histogram, DLQ rate, dedup rate, API latency) and Grafana dashboards committed as code. Choose a log stack (e.g. Loki or ELK) via ADR.
- **Cost guardrails (single developer, real money):** ephemeral AWS environments, smallest viable node sizes, spot where safe, budget alarm, `make cloud-down` teardown script, run the 100K events/s benchmark in one time-boxed window and destroy afterwards.

---

## 14. Frontend Rules (React + TypeScript)

- Views (minimum, depth over breadth): **Fleet Overview** (KPIs, trend charts), **Idle Cost Leaderboard** (top vehicles/drivers by estimated cost), **Vehicle Detail** (idle events, fuel, utilisation timeline), **Utilisation** (low/abnormal vehicles), **Attention/Alerts** (events needing action).
- Server-side pagination + filtering; virtualised tables for large lists; loading, empty and error states on every view.
- Every displayed cost/fuel figure shows its assumption (formula version, fuel price, idle threshold) so finance users can trust it. Label estimates as estimates.
- Accessible (semantic HTML, keyboard navigation, contrast). Responsive. No secrets or tokens in localStorage beyond what the chosen auth flow requires (decide with D4).
- Reuse shared components and one API client. No inline duplicated fetch logic.

---

## 15. Traceability & Evidence

- Maintain `docs/TRACEABILITY.md`: **Requirement → Feature → Implementation (path) → Test (path) → Demo step**. Update it whenever a feature is added or changed.
- A feature is **not done** until it has: code, tests, traceability row, and (if it affects NFRs) evidence.
- ADRs (3-5 minimum, more is fine): context, options, decision, consequences. Expected topics: stream processor (D5), data placement, dedup/idempotency, partition key and count, auth approach (D4), cloud-agnostic deployment.

---

## 16. Deliverables Checklist (from the hackathon PDF)

- [ ] Solution Document (using the provided template)
- [ ] Git repo with README, one-command docker compose setup, seeded 100K-vehicle dataset
- [ ] Architecture diagram, ER diagram (3NF), 3-5 ADRs
- [ ] Working end-to-end demo on the simulated stream
- [ ] Test evidence: coverage report, load-test results, security scan reports, CI pipeline link
- [ ] DevOps pack: Dockerfiles, Helm/K8s manifests, Terraform (AWS), STRIDE threat model
- [ ] Algorithms + SQL write-up with complexity analysis and before/after query plans
- [ ] Demo video, 5 minutes maximum
- [ ] AI tools and open-source declaration (`docs/AI_USAGE.md`)
- [ ] Final commit tagged `v1.0-submission` (code freeze at deadline)

Algorithms to implement where they fit this problem (do not force them): idle/trip segmentation over noisy signals (state machine or DP), sliding-window aggregates, streaming dedup, top-K idle offenders, geohash for depot/location grouping, VIN validation (17 chars, no I/O/Q, check digit) and DTC parsing.

---

## 17. Working Agreement (how Claude should work)

**At session start:** read this file, `docs/PROGRESS.md`, `docs/OPEN_DECISIONS.md`. Summarise current state in 3-5 lines and confirm the next task.

**During work:**
1. Restate the task and the requirement IDs it serves.
2. Propose the plan; wait for approval on anything non-trivial.
3. Implement in small vertical slices. First a **walking skeleton** (a few thousand vehicles end-to-end: simulator to Kafka to processor to stores to API to UI), then scale up.
4. Write tests with the code. Run lint, type-check and tests before declaring done.
5. Show evidence (test output, query plan, benchmark file path), not claims.
6. Update `docs/PROGRESS.md`, `docs/TRACEABILITY.md` and ADRs as part of the same change.

**Definition of Done:** code + tests green + coverage gate + lint/type clean + docs/traceability updated + no secrets + no unapproved dependency + evidence saved where relevant.

**Stop and ask when:** a TBD is hit, a hackathon requirement conflicts with a decision, a change touches the event contract or DB schema (needs migration + contract update), a dependency or cloud cost is introduced, or scope would grow.

**Communication style:** concise, direct, honest about trade-offs and unknowns. Say "not yet measured" instead of guessing. Flag risks early.

**AI-usage transparency:** the hackathon requires AI tools to be declared. Append notable AI-assisted work to `docs/AI_USAGE.md` (what was assisted, what the developer reviewed and decided). The developer must understand and be able to explain all code and design decisions.

---

## 18. Out of Scope Unless Explicitly Approved

Final auth provider choice, ML/predictive models, agentic AI/LLM features, vector store, MQTT gateway, OEM-format normalisation engine, carbon reporting, multi-region, any production credential. If the developer later approves one, add an ADR first.

---

## 19. Commands (fill in as they are created)

```
make up            # start full local stack + seed data
make down          # stop and clean
make test          # unit + integration
make lint          # ruff, mypy, eslint, tsc
make load          # k6/Locust scenario
make sim N=100000  # run simulator
make cloud-down    # tear down AWS environment
```
