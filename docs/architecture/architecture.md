# Architecture

Mermaid rather than images, so the diagrams stay reviewable in a diff and cannot drift from
the code without someone editing them.

## C4 level 1 — context

```mermaid
graph TB
    finance["Fleet Finance Manager<br/><i>which vehicles cost money, and why</i>"]
    ops["Fleet Operations Manager<br/><i>what needs attention today</i>"]
    vehicles["Connected vehicles<br/><i>simulated: 100K+ vehicles</i>"]

    platform["<b>Fleet Fuel, Idling &amp; Utilisation<br/>Cost Intelligence Platform</b>"]

    idp["OIDC provider<br/><i>D4 open; local stub for now</i>"]

    vehicles -->|"telemetry, ~1 sample / 10 s"| platform
    finance -->|"cost, trends, assumptions"| platform
    ops -->|"alerts, utilisation, vehicle detail"| platform
    platform -->|"validate JWT via JWKS"| idp
```

No vendor appears in that diagram on purpose. The auth provider is D4 and remains open, so
the platform speaks generic OIDC and the provider is a configuration value (CLAUDE.md §3).

## C4 level 2 — containers

```mermaid
graph LR
    subgraph edge["Edge"]
        web["<b>web</b><br/>React 18 + TS strict<br/>Vite, TanStack Query"]
    end

    subgraph services["Services"]
        sim["<b>simulator</b><br/>seeded, vectorised<br/>100K+ vehicles"]
        proc["<b>processor</b><br/>validate, dedup,<br/>segment, price"]
        api["<b>api</b><br/>FastAPI, hexagonal"]
        batch["<b>batch</b><br/>rollups, utilisation,<br/>erasure, reconcile"]
        oidc["<b>oidc_stub</b><br/>RS256 + JWKS<br/><i>local/ci only</i>"]
    end

    subgraph stores["Stores"]
        kafka[("Kafka 3.9 KRaft<br/>96 partitions")]
        pg[("PostgreSQL 16<br/>3NF + rollups<br/>RLS")]
        mongo[("MongoDB 7<br/>raw, 48h TTL")]
        redis[("Redis 7.2<br/>hot state")]
    end

    sim -->|"telemetry.raw<br/>key = vin"| kafka
    kafka -->|"consumer group<br/>cooperative-sticky"| proc
    proc -->|"telemetry.dlq<br/>+ reason code"| kafka
    proc -->|"telemetry.late"| kafka
    kafka -->|"drain"| batch
    proc -->|"bulk insert"| pg
    proc -->|"insert_many"| mongo
    proc -->|"pipeline"| redis
    batch --> pg
    batch --> mongo
    api --> pg
    api --> redis
    web -->|"REST /api/v1<br/>keyset pagination"| api
    web -->|"dev token"| oidc
    api -->|"JWKS"| oidc
```

## The value chain

Every stage exists to turn the one before it into something a manager can act on.

```mermaid
graph LR
    A["<b>Telemetry</b><br/>vin, ts, ignition,<br/>speed, fuel, lat/lon"]
    B["<b>Behaviour detection</b><br/>idle segmentation<br/>trip segmentation"]
    C["<b>Fuel &amp; cost impact</b><br/>burn rate x duration<br/>x price"]
    D["<b>Fleet decision</b><br/>which vehicle,<br/>what action"]

    A -->|"state machine<br/>hysteresis + gap tolerance"| B
    B -->|"pure domain function<br/>versioned formula"| C
    C -->|"rollups, scores,<br/>leaderboard, alerts"| D
```

## Request and event paths

### Ingest (hot path)

```mermaid
sequenceDiagram
    participant S as simulator
    participant K as Kafka
    participant P as processor
    participant R as Redis
    participant PG as PostgreSQL
    participant M as MongoDB

    S->>K: produce(topic, key=vin)
    Note over S,K: idempotent producer, acks=all
    P->>K: consume(batch=5000)
    P->>P: decode + validate
    P-->>K: invalid -> telemetry.dlq + reason
    P->>P: dedup (64-bit replay window)
    P-->>K: too late -> telemetry.late
    P->>P: idle + trip state machines
    P->>M: insert_many (upsert on event_id)
    P->>PG: COPY / executemany
    P->>R: pipeline (latest state, watermarks)
    P->>K: commit offsets
    Note over P,K: commit LAST, only after<br/>everything is durable
```

Offsets commit last. That ordering is the whole correctness argument: at-least-once delivery
plus idempotent writes gives an effectively-once outcome, whereas committing first would lose
a batch on any crash.

### Read (API)

```mermaid
sequenceDiagram
    participant W as web
    participant A as api
    participant R as Redis
    participant PG as PostgreSQL

    W->>A: GET /api/v1/... + Bearer
    A->>A: verify RS256 via JWKS,<br/>check iss/aud/exp, alg allow-list
    A->>R: rate-limit check
    A->>PG: SET ROLE fleet_app;<br/>set_config('app.tenant_id', ...)
    Note over A,PG: tenant from the TOKEN only,<br/>never the request
    A->>PG: keyset-paginated query
    A->>PG: INSERT audit_log
    A-->>W: JSON + assumptions + correlation id
```

### Reconciliation (ADR-0007)

```mermaid
graph TB
    L[("telemetry.late")] -->|drain| G["group by<br/>(tenant, vin, day)"]
    G --> T{"day still inside<br/>the Mongo TTL?"}
    T -->|no| U["count as unreconcilable<br/>+ reason"]
    T -->|yes| S["read the day's<br/>complete samples"]
    S --> F["fresh BatchProcessor<br/><i>same code as the stream</i>"]
    F --> RP["replace the vehicle-day<br/>in one transaction"]
    RP --> RU["recompute that day's<br/>rollups"]
    U --> LOG[("reconciliation_log")]
    RU --> LOG
```

The recomputation runs the **same** `BatchProcessor` the stream uses. A bespoke
re-segmentation path would be a second definition of an idle event, and two definitions
eventually disagree.

## Layering

Hexagonal, enforced by import direction rather than by convention alone:

```mermaid
graph TB
    subgraph domain["domain — pure, no I/O"]
        d1["idle.py — segmentation state machine"]
        d2["fuel.py — cost estimation"]
        d3["utilisation.py — composite score"]
        d4["trips.py — trip segmentation"]
        d5["vin.py, dtc.py, geohash.py"]
    end
    subgraph app["application — use cases"]
        a1["pipeline.py — batch orchestration"]
        a2["rollups.py, reconcile.py, erasure.py"]
    end
    subgraph adapters["adapters — all I/O"]
        p1["kafka_io.py, sinks.py"]
        p2["repository.py, mongo_samples.py"]
    end
    adapters --> app --> domain
```

Domain code imports no framework and no driver, which is why the algorithms are unit-testable
without infrastructure and why the property tests can run in milliseconds.

## Shared library

`libs/fleet_common` exists so there is one of each thing (CLAUDE.md §7): settings, structured
logging, error types and RFC 7807 responses, keyset pagination, JWT validation and RBAC,
event models generated against `schemas/`, VIN and DTC validators, geohash, and the domain
modules above. The simulator, processor, API, batch job and tests all import the same event
definition — a field name exists in exactly one place.

## Scaling model

```mermaid
graph LR
    subgraph k["Kafka — 96 partitions"]
        direction TB
        p1["p0..p23"]
        p2["p24..p47"]
        p3["p48..p71"]
        p4["p72..p95"]
    end
    c1["processor 1"] --- p1
    c2["processor 2"] --- p2
    c3["processor 3"] --- p3
    c4["processor N"] --- p4
```

Scaling is replicas and partitions, never a code change. 96 partitions comes from a
measurement, not a guess: 4,404 events/s per consumer means ~23 consumers for the 100K
events/s target, and 24 partitions left no headroom for a failure or a rolling deploy. At
100K vehicles the busiest partition carries 1.074x the mean (ADR-0004, ADR-0005).

## Cloud-agnostic boundary

```mermaid
graph TB
    subgraph app["Application — no cloud SDK anywhere"]
        code["services/ + libs/<br/>env config + standard protocols"]
    end
    subgraph portable["Portable runtime"]
        helm["Helm charts<br/>+ K8s manifests"]
    end
    subgraph cloud["Cloud-specific — isolated"]
        tf["infra/terraform/aws"]
        vals["Helm values-aws.yaml"]
    end
    code --> helm --> tf
    helm --> vals
```

Application code depends only on environment configuration and standard protocols — Kafka,
PostgreSQL, MongoDB, Redis, and an S3-compatible API behind an interface. Every AWS specific
lives in `infra/terraform/aws` and Helm values, so switching cloud means new IaC, not new
application code. Verified by the absence of any cloud SDK import in `services/` or `libs/`.

**The Terraform was never applied.** There is no AWS account for this project, and the
hackathon asks for IaC and an architecture deployable on a cloud, not a running cloud
environment. It is written, `validate`-clean and statically scanned; the Helm charts are
verified against a real Kubernetes cluster locally. That is stated plainly here and in the
solution document rather than implied otherwise.
