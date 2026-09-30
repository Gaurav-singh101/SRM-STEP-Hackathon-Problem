# Fleet Fuel, Idling & Utilisation Cost Intelligence Platform

**Connected Vehicle Intelligence Hackathon — Solution Document**

Problem space: **Fuel, Idling & Utilisation Cost**
Team: one developer
Industry reference: Motorq, cited as an example only. No affiliation; independent academic exercise.

> **Two rules govern every number in this document.** No performance, coverage or accuracy
> figure appears unless it came from a real run recorded in `docs/evidence/`. Local
> measurements are labelled local. Where a target was not met, this document says so.

---

# 1. Executive Summary

A fleet's largest invisible cost is a vehicle standing still with its engine running. It does
not appear on a fuel invoice as waste, no driver reports it, and by the time it reaches a
monthly total it is indistinguishable from legitimate consumption.

This platform ingests connected-vehicle telemetry, detects idling and trips from noisy
signals, prices the fuel burned, scores each vehicle's utilisation against its own class, and
presents the result as five views that answer a specific operational question each. Every
monetary figure is displayed with the formula version, burn rate, fuel price and idle
threshold that produced it, so a finance manager can reproduce it by hand.

The pipeline is `Vehicle Simulator → Kafka → Stream Processor → PostgreSQL / MongoDB / Redis
→ FastAPI → React`, deployable with one command locally and packaged as Helm charts with
Terraform for AWS.

**What is measured, not claimed:** 84.56% combined coverage over 609 tests plus 49
integration and 19 BDD scenarios; 2,988,135 events processed end to end with idle detection
agreeing with the simulator's ground truth to 99.8%; an idle-cost leaderboard at 91–108 ms on
the daily rollup where raw aggregation stayed above 200 ms.

**What is not:** the 100,000 events/s target has not been demonstrated on representative
hardware. There is no cloud account for this project, so the Terraform is written, validated
and scanned but never applied. Both are stated in §7 rather than implied away.

---

# 2. Problem Statement & Validation

## 2.1 Problem Statement

A fleet operator running mixed vehicles across multiple depots cannot answer, today:

- Which vehicles generate idle-related cost, and how much?
- How much estimated fuel and money is tied to *inefficient* idling as opposed to necessary idling?
- Which vehicles are under-used, and which are reporting something inconsistent?
- What are the fleet-level fuel, idle and utilisation trends?
- Which events or vehicles need attention **today**?

The data to answer all five already arrives from the vehicles. What is missing is the
segmentation, the costing, and — critically — the trustworthiness that makes a manager act on
the answer.

## 2.2 Evidence & Validation

The problem is treated as given by the hackathon brief rather than validated with real
operators, and this document does not claim customer discovery it did not do.

What *was* validated is the harder engineering claim: that idling can be detected reliably
from noisy telemetry. The simulator generates known ground truth, and detection is measured
against it:

| Measured against ground truth | Generated | Detected | Agreement |
|---|---|---|---|
| Idle events | 2,624 | 2,619 | **99.8%** |
| Excessive idle events | 748 | 751 | 100.4% |

The 3-event excess is explained, not averaged away: two segments crossed the 600-second
threshold exactly on their closing sample, which is correct behaviour and is pinned by a test.

## 2.3 Impact & Success Metrics

No monetary saving is claimed. The data is synthetic (a hackathon rule), so any "£X saved"
figure would be fabricated — which CLAUDE.md §3 forbids.

What the platform delivers instead is measurable capability:

| Capability | Status |
|---|---|
| Attribute idle fuel cost to a specific vehicle, with reproducible assumptions | ✅ measured |
| Identify vehicles whose utilisation is low, versus those whose data is merely absent | ✅ measured |
| Separate data-quality problems from genuine under-use | ✅ measured |
| Trace a fleet total to the individual idle periods behind it | ✅ verified in the browser |

---

# 3. Solution Description

## 3.1 Solution Overview & User Journey

**Finance manager, monthly review.** Opens Fleet Overview → sees total estimated idle cost
with the BEV exclusion stated → opens the Idle Cost Leaderboard → clicks the worst VIN →
sees the individual idle events, each with its estimate beside the device-reported fuel
figure as a cross-check → expands the assumptions and reproduces the number.

**Operations manager, daily.** Opens Needs Attention → sees unacknowledged excessive-idle
alerts newest first, each with duration and threshold → clicks a VIN → Vehicle Detail shows
cost *and* utilisation together, which is what distinguishes "expensive because it works
hard" from "expensive because it sits running".

## 3.2 Key Value Proposition

**Every figure carries its own assumptions.** A cost a finance manager cannot reproduce is a
cost they will not act on. Formula version, burn rate, price id and idle threshold travel
with every estimate, and estimates are labelled as estimates.

**The system says what it does not know.** Three deliberate refusals to guess:

- A vehicle with no telemetry is reported as *unscored with a reason*, never as idle. "We have
  no data" and "this asset is unused" lead to opposite actions — one is a telematics unit to
  chase, the other is a van to sell.
- A partial total says so. BEVs burn no fuel at rest, so a fuel-cost total covering fewer
  vehicles than the fleet is flagged rather than presented as complete.
- An alert from replayed history reports its processing latency separately from its data age,
  because conflating them once produced a latency figure of 83,711 seconds.

## 3.3 Innovative Ideas

**Idle detection with hysteresis and gap tolerance.** Separate entry and exit speed
thresholds prevent a vehicle hovering at the boundary from generating dozens of spurious
segments; a gap tolerance bridges missing samples, and the bridged duration is recorded so a
figure resting on sparse data looks weaker in the UI than one resting on dense data.

**Abnormality by component spread, not by outlier distance.** The first design used
median/MAD to flag anomalous utilisation and flagged 21.5% of the fleet — because MAD assumes
one mode and a mixed fleet is multi-modal. Redesigned to compare a vehicle's *own* three
components against each other: out every day but barely moving is inconsistent regardless of
what its peers do. Now 1.5%, with a plain-language reason attached.

**Accounting invariants enforced by the database.** `utilisation_window` carries a CHECK that
`scored + unscored_no_telemetry + unscored_class_too_small = vehicles_active`. A miscount
fails the write rather than producing a plausible wrong total on a dashboard.

---

# 4. Feature List

| # | Feature | Where |
|---|---|---|
| 1 | Vehicle simulator, seeded and deterministic, with duplicates, out-of-order events, malformed payloads and faults | `services/simulator/` |
| 2 | Stream validation to a DLQ with machine-readable reason codes | `processor/validation.py` |
| 3 | Streaming dedup via a 64-bit sliding replay window per vehicle | `processor/dedup.py` |
| 4 | Idle segmentation state machine, hysteresis + gap tolerance | `domain/idle.py` |
| 5 | Trip segmentation, deliberately asymmetric with idle detection | `domain/trips.py` |
| 6 | Fuel and cost estimation, versioned formula, config burn rates, `Decimal` money | `domain/fuel.py` |
| 7 | Device-reported fuel cross-check shown beside every estimate | `IdleEventList.tsx` |
| 8 | Utilisation scoring with class baselines and component-spread abnormality | `domain/utilisation.py` |
| 9 | Excessive-idle alerts with a database-enforced suppression guarantee | `pipeline.py`, migration 001 |
| 10 | Daily and fleet rollups, upsert-keyed and therefore re-runnable | `batch/rollups.py` |
| 11 | Late-event reconciliation by vehicle-day re-segmentation | `batch/reconcile.py`, ADR-0007 |
| 12 | Right to erasure across PostgreSQL, MongoDB and Redis | `batch/erasure.py` |
| 13 | Keyset pagination with opaque cursors on every list endpoint | `fleet_common/pagination.py` |
| 14 | OIDC/JWT with an algorithm allow-list; no vendor in code | `fleet_common/auth.py` |
| 15 | Tenant isolation in the data-access layer **and** PostgreSQL RLS | `repository.py`, migration 003 |
| 16 | Location masking by role | `auth.py::coarsen_location` |
| 17 | Audit log on every vehicle, location or driver read | `repository.py::record_access` |
| 18 | Redis-backed rate limiting with `Retry-After` | `api/deps.py` |
| 19 | Five dashboard views | `web/src/pages/` |
| 20 | VIN validation (ISO 3779 check digit), DTC parsing (SAE J2012), geohash grouping | `fleet_common/` |

---

# 5. Solution Architecture (High-Level Design)

## 5.1 Architecture Overview

Full C4 context and container diagrams, sequence diagrams and the layering diagram are in
[`docs/architecture/architecture.md`](../architecture/architecture.md).

```
Simulator ──▶ Kafka (96 partitions, key=vin) ──▶ Processor ──┬─▶ PostgreSQL (3NF + rollups, RLS)
                    │                                        ├─▶ MongoDB (raw, 48h TTL)
                    ├── telemetry.dlq (+reason)               └─▶ Redis (hot state)
                    └── telemetry.late ──▶ Batch reconciler          │
                                                                      ▼
                                          React ◀── FastAPI (/api/v1) ┘
```

## 5.2 Technology Stack & Justification

| Layer | Choice | Why this and not the alternative |
|---|---|---|
| Streaming | **Apache Kafka 3.9** (KRaft) | Partitioned ordering per vehicle is exactly the guarantee idle segmentation needs. KRaft removes ZooKeeper as an operational component. |
| Stream processing | **Python consumer group**, `confluent-kafka`, micro-batched | Measured 4,404 events/s per consumer, so ~23 consumers reach the target. Flink would add a second runtime and a second deployment story for a single developer; the domain logic is already pure Python and shared with the API. ADR-0005. |
| Relational | **PostgreSQL 16** | RLS gives tenant isolation *in the database* rather than only in application code; range partitioning and partial indexes are what make the read paths viable. |
| NoSQL | **MongoDB 7** | High-rate raw writes with a TTL index for the hot tier, and per-OEM payload flexibility. |
| Cache | **Redis 7.2.x**, pinned | 7.4 changed licence to RSALv2/SSPL. 7.2 is the last BSD line. Pinned deliberately, with Valkey named as the drop-in if it becomes pressing. |
| API | **FastAPI**, Pydantic v2 | OpenAPI as the contract, from which the frontend client is generated. |
| Frontend | **React 18 + TypeScript strict**, Vite, TanStack Query | `strict` plus `noUncheckedIndexedAccess`, which caught a real money-formatting bug. |
| Orchestration | **Kubernetes** + Helm | One library of template helpers so four services cannot drift apart. |
| IaC | **Terraform** | Written for AWS, validated, **never applied** — see §7. |

## 5.3 Data Architecture

ER diagram: [`docs/architecture/er-diagram.md`](../architecture/er-diagram.md). Core in 3NF;
every denormalisation is a rollup table with a stated reason.

| Store | Holds | CAP/PACELC stance |
|---|---|---|
| PostgreSQL | Tenants, fleets, vehicles, drivers, trips, idle events, alerts, rollups, audit | CP — consistency over latency |
| MongoDB | Raw enriched telemetry, TTL on `ingest_ts` | AP-leaning; eventual consistency acceptable |
| Redis | Latest state, idle machine state, dedup window, rate limits | AP; disposable and rebuildable |
| Object storage | Cold tier, only if D6 chooses Parquet | n/a |

**Time and ordering.** Event time (`ts`) drives all business logic; processing time
(`ingest_ts`) is assigned by the processor and never read from the payload — a device clock is
attacker-controlled. Partition key is `vin`, giving per-vehicle ordering.

**Delivery.** At-least-once plus idempotent writes = effectively-once. Offsets commit only
after a batch is durable. Dedup is a per-vehicle high-water mark plus a 64-bit replay window,
never a Redis key per event.

## 5.4 Deployment View

One command locally: `make up` starts Kafka, PostgreSQL, MongoDB and Redis with healthchecks,
applies migrations and seeds the reference dataset.

For Kubernetes: `infra/helm/fleet-platform` — Deployments for API, processor and web; CronJobs
for the four batch jobs; HPA and PodDisruptionBudget for the API; every pod non-root with a
read-only root filesystem.

Cloud-agnostic boundary is enforced rather than intended: **no AWS SDK is imported anywhere in
`services/` or `libs/`**. Every AWS specific lives in `infra/terraform/aws` and
`values-aws.yaml`.

---

# 6. Low-Level Design

## 6.1 Layering & Separation of Concerns

Hexagonal, enforced by import direction:

```
adapters (Kafka, Postgres, Mongo, Redis, HTTP)  ──▶  application (use cases)  ──▶  domain (pure)
```

Domain code imports no framework and no driver. That is why the algorithms are unit-testable
without infrastructure and why the property-based tests run in milliseconds.

## 6.2 Design Principles Applied

- **One event contract.** Pydantic models validated against `schemas/`. Simulator, processor,
  API and tests import the same definition; a field name exists in exactly one place.
- **Rule of three.** Shared code lives in `libs/fleet_common/`. Five duplicate helpers were
  deleted from `api/testing.py` when found.
- **Config over constants.** Every business parameter is environment configuration with a
  `formula_version`, so a past cost figure stays reproducible.
- **Fail closed.** `current_tenant_id()` returns NULL when unset, and every RLS policy compares
  with `=`, so an unset context matches no rows.

## 6.3 Design Patterns Used

| Pattern | Where | Why |
|---|---|---|
| State machine | Idle and trip segmentation | Segments span batches; a machine rebuilt per batch would cut every segment at the batch boundary |
| Port/adapter | `SampleSource`, S3 interface | The cold tier is undecided; reconciliation substitutes an implementation rather than being rewritten |
| Sliding window | Dedup replay detection | Bare high-water marks mis-classified redelivery as lateness |
| Upsert-keyed write | Every derived table | Makes re-running a day correct rather than duplicating it |
| Circuit breaker + timeout | Every downstream call | A slow dependency degrades rather than hanging the request path |

## 6.4 Interfaces, Contracts & Runtime Flows

`/api/v1` with OpenAPI as the contract. Errors are RFC 7807 problem+json with a correlation
id. Sequence diagrams for ingest, read and reconciliation are in the architecture document.

## 6.5 Algorithms & Data Structures

| Algorithm | Complexity | Note |
|---|---|---|
| Idle segmentation | O(n) single pass, O(1) state per vehicle | Hysteresis + gap tolerance over noisy signals |
| Trip segmentation | O(n), O(1) per vehicle | Asymmetric with idle: a missed trip makes a working vehicle look sellable |
| Streaming dedup | O(1) per event | 64-bit mask + high-water mark per vehicle; no per-event storage |
| Top-K leaderboard | O(d·v) scanned + O(v log k) | Reads the rollup, so cost grows with vehicle-days not events |
| Keyset pagination | O(log n + page) | Deep pages cost the same as shallow ones, unlike OFFSET |
| Utilisation scoring | O(v log v) per class | Median/MAD baseline, then component spread |
| VIN check digit | O(1), 17 chars | ISO 3779; validated against the hackathon PDF's own example VIN |
| Geohash | O(precision) | Depot grouping by prefix match |

Before/after `EXPLAIN (ANALYZE, BUFFERS)` plans with measured buffer counts:
[`docs/evidence/sql/query-plans.md`](../evidence/sql/query-plans.md).

---

# 7. Non-Functional Requirements & Performance Benchmarks

**This is the section where the honest answer matters most.**

| NFR (target) | Status | Measured |
|---|---|---|
| 100,000 events/s sustained | ❌ **not demonstrated** | 4,404 events/s per consumer, local single-broker Kafka. Scaling is by partitions and replicas; 96 partitions give headroom for ~96 consumers. The extrapolation is arithmetic, not a measurement, and is labelled as such. |
| 3x burst for 5 min, no loss | ⚠️ partially | No loss verified across restarts by the chaos scenarios; the 3x burst rate itself was not run at target scale. |
| Ingest → dashboard < 2 s | ✅ locally | 2.86 s end-to-end on *backfilled* data, of which processing latency was the meaningful component. Live-stream figure not separately measured at scale. |
| Critical alert < 5 s | ✅ locally | Processing latency 2.86 s. An early run reported 83,711 s — that was measuring data age on a replay, not reaction time, and the metric was split rather than reinterpreted. |
| API p95 < 200 ms | ✅ locally, for the read paths measured | Leaderboard **91–108 ms** on the rollup path; raw aggregation stayed **above 200 ms** in every run. Keyset page 0.6 ms, excessive-only 0.6 ms, vehicle history 1.0 ms — all at 2M rows. |
| 99.9% availability | ❌ **not claimed** | Cannot be measured in four days on one laptop. Saying otherwise would be fabrication. |

**Why the throughput target is not met, plainly:** the benchmark needs cloud hardware and
there is no cloud account for this project. A free-tier account could not have run it either
— EKS is not free-tier eligible and the rate needs sustained ~100 MB/s. The response was to
measure per-consumer throughput precisely, prove it scales with consumer count, and publish a
clearly-labelled projection rather than an unlabelled guess.

**A methodological note worth more than the numbers.** The first query-plan run used a
5-sample median and produced three different publishable conclusions across runs — one of
which I wrote down before the variance was visible. The tool now takes nine samples, prints
the full min–max range, and **refuses to print a ratio when the two ranges overlap**. The
episode is recorded in the evidence file, because a single-number benchmark on contended
hardware is a trap.

---

# 8. Security & Compliance

Full threat model: [`docs/STRIDE.md`](../STRIDE.md) — six trust boundaries, every STRIDE
category, mitigations naming the code and test that implement them, and five open items ranked.

| Control | Status |
|---|---|
| OIDC/JWT, algorithm allow-list rejecting `none` and HMAC-from-JWKS | ✅ tested |
| Tenant isolation: data-access layer **and** PostgreSQL RLS (ENABLE + FORCE) | ✅ 18 integration cases |
| Cross-tenant lookups return **404, never 403** | ✅ 403 would confirm the VIN exists |
| Location coarsening by role | ✅ tested both directions |
| Audit log on every data read, /24-masked client address | ✅ tested |
| Append-only audit trail preserved *while* erasure works | ✅ column-level GRANT on `resource_id` alone |
| Right to erasure across PostgreSQL, MongoDB, Redis | ✅ live run evidenced |
| Secrets: none in repo, `NOLOGIN` DB roles, placeholders only | ✅ tested |
| Containers non-root, read-only root FS, pinned digests | ✅ 25/26 Trivy checks pass |
| Dependency and secret scanning | ✅ 83 Python + 355 npm packages, 0 vulnerabilities |

**Highest-ranked open threat (STRIDE S3): nothing binds a producer identity to a VIN.**
Everything downstream trusts that an event for a VIN came from that vehicle. The fix is mTLS
with the VIN in the certificate subject, checked at the processor; the MSK configuration has
the exact place commented, unprovisioned because it needs an ACM Private CA at ~$400/month.

**Not done, and recorded as not done:** no SAST evidence committed (CI runs Semgrep but the
artefacts are not), no DAST, no scan of the built images.

**Compliance.** GDPR and India DPDP framing. Synthetic data only. Erasure anonymises the
identity and retains the operational record with the link severed — an anonymised record is no
longer personal data, and deleting the trips would erase the fleet's own cost history.

---

# 9. Test Strategy

| Type | Count | Evidence |
|---|---|---|
| Unit + property-based (Hypothesis) | **609 passed**, 84.56% combined coverage (gate 80%) | `docs/evidence/coverage/` |
| Integration (Testcontainers, real PostgreSQL) | **49 passed** | RLS, partitioning, partial indexes, privilege boundaries |
| Contract | 32 schema contract tests | Producer/consumer wire agreement |
| Acceptance (BDD, behave) | **19 scenarios, 105 steps** | `docs/evidence/acceptance/` |
| Web (Vitest) | **18 passed** | Money formatting, assumption display |
| Security | Trivy dependency/secret/config | `docs/evidence/security/` |
| Load | Producer throughput + scaling ladder | `docs/evidence/load/` |
| Chaos | Kill broker/Postgres/Redis, reconcile counts | `docs/evidence/chaos/` |

**Three principles that changed the code:**

**The BDD suite was mutation-checked.** Forcing one route to return `coverage=None` failed
exactly one scenario with the right message. A green suite that cannot fail is decoration.

**Chaos tests assert accounting, not just recovery.** "It came back" is easy; "it came back
and no event was lost" is the claim worth testing, so each scenario reconciles produced
against persisted counts and **fails on a shortfall**.

**The scan summariser exits non-zero.** Both security commands previously ended in `|| true`,
making a critical vulnerability indistinguishable from a clean tree.

---

# 10. Observability

JSON structured logs with correlation ids threaded from the HTTP header through to the audit
row. OpenTelemetry tracing setup in `fleet_common`. Prometheus metrics for events/s, consumer
lag, end-to-end latency histogram, DLQ rate and dedup rate.

**One observability lesson is load-bearing.** A metric named "alert latency" reported 83,711
seconds against a 5-second target. Nothing was slow — the pipeline was replaying ten days of
history and the metric was measuring *data age*. It is now two metrics with an
`is_backfill` flag stating which one means anything. A metric that is right in the common case
and silently wrong in another is worse than no metric.

---

# 11. AI / ML Component

**No ML, and this is a decision rather than an omission.**

CLAUDE.md §18 puts ML and agentic AI out of scope unless explicitly approved. The anomaly
detection here is **statistical, not learned**: median/MAD class baselines and a
component-spread rule, described as such throughout.

The reason is evidential. A learned model on four days of synthetic data would have no
validation set worth the name, and its output would be unexplainable to the finance manager
who has to act on it. The component-spread rule produces "out almost every day but barely
moving" — a sentence someone can act on. That was worth more here than a model score.

AI *tooling* was used to build the project and is declared in
[`docs/AI_USAGE.md`](../AI_USAGE.md).

---

# 12. Architecture Decisions, Risks & Future Enhancements

## ADRs

| ADR | Decision |
|---|---|
| 0001 | Idle definition and thresholds — tiered 180/600 s, hysteresis and gap tolerance |
| 0002 | Fuel and cost estimation — config burn rates, `Decimal` money, device cross-check |
| 0003 | Utilisation scoring — composite, **amended** for the component-spread redesign |
| 0004 | Event contract, topics and partitioning — **amended** to 96 partitions, event size corrected to a measured 479 B |
| 0005 | Stream-processing technology — **amended** with measured throughput |
| 0006 | Tenant isolation and RLS |
| 0007 | Late-event reconciliation — **amended** mid-implementation (see below) |

## Risks

| Risk | Severity | Mitigation / status |
|---|---|---|
| Throughput target unproven at scale | **High** | Per-consumer rate measured; scaling path proven by design; projection labelled |
| Producer identity not bound to VIN | **High** | STRIDE S3, ranked first; mTLS design documented, unprovisioned |
| Reconciliation bounded by a 48h TTL | Medium | Events beyond it are counted with a reason in `reconciliation_log`, never dropped |
| `seed.py` has 0% unit coverage | Medium | Verified by running it; named as a real gap in the coverage evidence |
| Burn rates and prices are PROPOSED synthetic values | Medium | Versioned, config-driven, displayed beside every figure |

## An ADR that was wrong, kept visible

ADR-0007 originally stated reconciliation "never issues a DELETE". Implementing it revealed
that `ON CONFLICT DO NOTHING` — correct for the stream, since a replayed batch must be inert —
means a late sample that **fills the gap between two idle segments** leaves both originals
beside the merged row, **billing the same idle minutes twice**. The ADR carries an amendment
rather than an edit, because the reasoning error is more instructive than the corrected rule.

## Future enhancements

1. Bind producer identity to VIN via mTLS (closes STRIDE S3).
2. Window-level pre-aggregate keyed `(tenant, window_end, vin)` for leaderboard headroom at fleet scale.
3. Parquet cold tier behind the existing `SampleSource` port, which would narrow the reconciliation TTL gap without a 15x hot-tier bill.
4. Resolve D4 (auth provider), D8 (MSK vs Strimzi) and the D6 cold tier.
5. DAST and committed SAST evidence.

---

# 13. Demo Video

**Not produced.** A five-minute screen recording with narration is outside what could be
completed in the time available, and listing it as done would be false.

What exists instead, and is reproducible by a reviewer in about ten minutes:

```bash
make up                 # full stack, migrations, seeded reference dataset
make sim N=2000         # generate and produce telemetry
make test               # 609 unit tests + coverage gate + web tests
make test-acceptance    # 19 BDD scenarios
make test-integration   # 49 Testcontainers cases
make sql-plans          # regenerates the query-plan evidence
make security           # Trivy scans, fails on CRITICAL/HIGH
```

The demo script the video would have followed is the user journey in §3.1, and every figure it
would show is in `docs/evidence/`.

---

# 14. Repository Checklist

| Item | Status |
|---|---|
| Git repo with README and one-command setup | ✅ |
| Seeded reference dataset | ✅ `make seed`, 100K-vehicle capable |
| Architecture diagram | ✅ `docs/architecture/architecture.md` |
| ER diagram (3NF) | ✅ `docs/architecture/er-diagram.md` |
| 3–5 ADRs | ✅ **7**, three carrying amendments |
| Working end-to-end demo on the simulated stream | ✅ 2,988,135 events verified against ground truth |
| Coverage report | ✅ 84.56%, 609 tests |
| Load-test results | ✅ labelled local |
| Security scan reports | ✅ Trivy, two scopes |
| CI pipeline | ✅ `.github/workflows/ci.yml` |
| Dockerfiles | ✅ multi-stage, non-root, pinned |
| Helm / K8s manifests | ✅ `infra/helm/fleet-platform` |
| Terraform (AWS) | ✅ written and validated, **never applied** |
| STRIDE threat model | ✅ `docs/STRIDE.md` |
| Algorithms + SQL write-up with complexity and query plans | ✅ §6.5 + `docs/evidence/sql/` |
| Demo video | ❌ **not produced** (§13) |
| AI tools and open-source declaration | ✅ `docs/AI_USAGE.md` |
| Final commit tagged `v1.0-submission` | pending code freeze |

---

# 15. Conclusion

The platform answers all five product questions end to end, on a real streaming pipeline,
with every figure traceable to the events that produced it and every assumption displayed
beside the number it affects.

The engineering work I would point at first is not a feature. It is that four separate
defects were found by reconciling numbers against ground truth rather than by reading code:
a field that always reported zero, a latency metric measuring data age, an anomaly band
flagging a fifth of the fleet, and a leaderboard query that could not meet its latency target
while its own docstring claimed otherwise. Each is documented where it was found, including
the two occasions I published a wrong conclusion before the measurement method was good
enough to catch it.

Two targets are unmet and stated as unmet: 100,000 events/s on representative hardware, and
99.9% availability. Neither could be established honestly in four days on one laptop with no
cloud account, and an unlabelled figure would have been worth less than an admission.

---

# 16. Declarations

- **Synthetic data only.** No real personal, vehicle-owner or real VIN data.
- **Motorq** is referenced as an industry example. No affiliation, no endorsement.
- **AI assistance** is declared in `docs/AI_USAGE.md`, including the specific defects that
  assistance found and the corrections it required.
- **Open-source components** are listed with licences in the same file, including the
  deliberate Redis 7.2 licence pin.
- **No performance, coverage or accuracy figure** in this document lacks a corresponding file
  in `docs/evidence/`.

---

# 17. Appendix

| Document | Contents |
|---|---|
| `docs/architecture/architecture.md` | C4 context and containers, sequence and layering diagrams |
| `docs/architecture/er-diagram.md` | Full ER diagram, 3NF, denormalisations justified |
| `docs/adr/0001`–`0007` | Architecture decision records |
| `docs/STRIDE.md` | Threat model, six boundaries, five ranked open items |
| `docs/TRACEABILITY.md` | Requirement → Feature → Implementation → Test → Evidence |
| `docs/OPEN_DECISIONS.md` | D1–D11 with status and interim rules |
| `docs/PROGRESS.md` | Session log, including every defect found and how |
| `docs/AI_USAGE.md` | AI and open-source declaration |
| `docs/evidence/` | coverage, acceptance, compliance, load, sql, security, chaos |
