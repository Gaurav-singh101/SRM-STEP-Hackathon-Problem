# STRIDE threat model

Required by CLAUDE.md §11 and the hackathon DevOps pack (§16).

Scope: the Fleet Fuel, Idling & Utilisation Cost platform as built —
`simulator → Kafka → processor → PostgreSQL / MongoDB / Redis → FastAPI → React`.

**Two honesty rules apply throughout.** A mitigation is listed as *implemented* only when
there is code and a test to point at; the row names both. Everything else is **OPEN** or
**PARTIAL**, and a threat with no mitigation is written down rather than left out. An
unlisted threat is the dangerous kind.

The system holds **synthetic data only** (hackathon rule), so nothing here describes a
real exposure. It describes what would be exposed if real fleet data were loaded, which is
the point of doing it before that happens.

---

## Assets, worst first

| Asset | Why it matters if lost |
|---|---|
| **Vehicle location traces** | The most sensitive data in the system. A sequence of positions identifies a driver's home, their customers, and a vehicle worth stealing. Location is personal data under GDPR and India's DPDP Act even though it describes a vehicle. |
| **Driver identity, and the driver↔vehicle link** | "Who was driving this van on Tuesday" identifies a person and supports disciplinary or insurance consequences. |
| **Tenant fleet data** | Fleet size, cost per vehicle and utilisation are commercially sensitive. One operator learning another's cost base is a competitive harm even with no personal data involved. |
| **The audit trail** | Evidence of who read what. Its value is entirely in being unalterable; a rewritable trail is worth less than none, because it invites false confidence. |
| **Cost and fuel figures** | Feed budget and procurement decisions. Silent corruption is worse than an outage: an outage is noticed. |
| **Credentials and tokens** | JWT signing keys, database roles, Kafka credentials. |

## Trust boundaries

| # | Boundary | Crossing |
|---|---|---|
| **B1** | Vehicle / simulator → Kafka | Untrusted device input. A device clock, firmware and identity are all attacker-controllable in the real world. |
| **B2** | Kafka → processor | Internal, but the payload is still untrusted data from B1. |
| **B3** | Processor / batch → data stores | Privileged writes. |
| **B4** | Browser → API | Authenticated user input over the public edge. |
| **B5** | API → data stores | Where tenant isolation is enforced or lost. |
| **B6** | Operator → cluster / cloud | Deploy-time and administrative access. |

---

## S — Spoofing

| # | Threat | Boundary | Status | Mitigation and where it lives |
|---|---|---|---|---|
| S1 | A forged JWT grants access, or an `alg: none` / HMAC-signed token is accepted against the JWKS public key (algorithm confusion) | B4 | **Implemented** | Algorithm allow-list refused at settings load *and* again at verification; `iss`, `aud`, `exp`, `nbf` all checked. `libs/fleet_common/fleet_common/auth.py`; `services/api/tests/test_api.py` auth cases; `test_settings.py` rejects `none` and HMAC. |
| S2 | A caller claims another tenant by putting `tenant_id` in the body or query string | B4 | **Implemented** | Tenant comes only from the verified token claim (`Principal`). `test_tenant_cannot_be_overridden_by_a_query_parameter`. |
| S3 | A rogue producer publishes telemetry for a VIN it does not own | B1 | **OPEN** | The simulator authenticates to the broker, but nothing binds a producer identity to a VIN. A real deployment needs per-device mTLS with the VIN in the certificate subject and a broker-side or processor-side check that the two agree. Documented as a deployment requirement, not built. |
| S4 | A stolen token is replayed | B4 | **PARTIAL** | Short expiry and `jti` are present; no revocation list or token binding. Depends on D4, which is still open — a provider choice brings its own revocation story, and inventing one now would be the wrong order. |
| S5 | An attacker impersonates the OIDC provider by pointing JWKS elsewhere | B4 | **Implemented** | Issuer and JWKS URI come from environment config, never from the token. A token naming a different issuer is refused (`test_token_from_the_wrong_issuer_is_401`). |

The development OIDC stub (`services/oidc_stub/`) issues real RS256 tokens to anyone.
That is safe only because it **refuses to start outside `local`/`ci`** — asserted in its own
tests. It is the single largest spoofing risk in the repository if that guard is ever
removed.

## T — Tampering

| # | Threat | Boundary | Status | Mitigation |
|---|---|---|---|---|
| T1 | A device backdates `ts` to move cost into another billing period, or forward-dates it to poison the watermark | B1 | **Implemented** | `ingest_ts` is processor-assigned and never read from the payload (ADR-0004). Events beyond the lateness window go to `telemetry.late`, not into the stream. Forward-dated events are rejected to the DLQ — found in practice when 72% of a run was future-dated and correctly quarantined. |
| T2 | Impossible values (negative fuel, speed of 900 km/h) corrupt aggregates | B2 | **Implemented** | Schema and range validation before any write; failures go to the DLQ with a reason code and never crash the consumer. `services/processor/processor/validation.py`. |
| T3 | SQL injection through a sort, filter or band parameter | B4/B5 | **Implemented** | Parameterised queries throughout; sort and filter fields are allow-listed, not interpolated. The utilisation `band` filter is an allow-list whose rejection is tested (`test_a_band_filter_is_allow_listed`). |
| T4 | The audit trail is rewritten to hide a read | B3/B5 | **Implemented** | `UPDATE` and `DELETE` on `audit_log` are revoked from **every** application role (migration 003). The erasure job gets a **column-level** grant on `resource_id` alone (migration 007), so a subject reference can be erased while actor, action, timestamp and correlation id stay immutable. Privilege-boundary tests in `tests/integration/test_erasure.py`. |
| T5 | A replayed Kafka batch double-counts fuel cost | B2/B3 | **Implemented** | At-least-once delivery plus idempotent writes: a unique index on `(vin, started_at)` with `ON CONFLICT DO NOTHING`, and a 64-bit sliding replay window per vehicle. Offsets commit only after the batch is durable. |
| T6 | A cost figure is silently wrong because a burn rate or price was mis-entered | B3 | **PARTIAL** | Every figure carries `formula_version`, the burn rate and the price id, and the device-reported fuel rate is shown beside the estimate as a cross-check (D2). There is no approval workflow on the price table — a mis-entry is *visible* but not *prevented*. |
| T7 | A container image is swapped between build and deploy | B6 | **PARTIAL** | Base images are pinned by digest and containers run non-root. Image signing and admission-time verification are not in place. |

## R — Repudiation

| # | Threat | Boundary | Status | Mitigation |
|---|---|---|---|---|
| R1 | A user denies having read a driver's location | B4 | **Implemented** | Every endpoint that reads vehicle, location or driver data writes an audit entry with actor, action, tenant, resource, correlation id and a /24-masked client address. `repository.py::record_access`; `test_every_data_read_writes_an_audit_entry`, and the two new views are covered by their own audit assertions. |
| R2 | An erasure is claimed but never happened, or happened and cannot be shown | B3 | **Implemented** | The erasure record is opened **before** anything is deleted, completed with a per-store outcome, and can never be deleted by the role that writes it. Verified by reading the data back through a *different* role rather than trusting the eraser. `docs/evidence/compliance/erasure-local.md`. |
| R3 | A processing decision cannot be reconstructed later | B2 | **Implemented** | Rejections carry a reason code to the DLQ; `formula_version` is stored on every derived row, so a figure can be re-derived with the rules that produced it. |
| R4 | Administrative actions at the cluster level are unattributable | B6 | **OPEN** | No audit of deploys or direct database access. Would come from cloud-provider audit logging (CloudTrail equivalent) and is out of scope for the current account — see PROGRESS blocker B3. |

## I — Information disclosure

| # | Threat | Boundary | Status | Mitigation |
|---|---|---|---|---|
| I1 | One tenant reads another's vehicles, costs or fleet size | B5 | **Implemented** | PostgreSQL row-level security with `ENABLE` **and** `FORCE` on every tenant-scoped table, tenant set via `set_config` from the verified claim. Enforced in the data-access layer, not only in routes. 18 cases in `tests/integration/test_tenant_isolation.py`, plus the rollup tables — `utilisation_window` is tenant-scoped precisely because fleet size is commercially sensitive even though it holds no personal data. |
| I2 | A 403 confirms a VIN exists in another tenant | B4 | **Implemented** | Cross-tenant lookups answer **404**, never 403. Asserted over HTTP and as an acceptance scenario. |
| I3 | Precise location shown to a role that does not need it | B4 | **Implemented** | Coarsened to 2 decimal places (~1.1 km) for finance roles; precise only for operations and supervisor. `auth.py::coarsen_location`; `test_finance_role_receives_coarsened_coordinates`; acceptance scenarios cover both directions. |
| I4 | Precise coordinates, tokens or secrets land in logs | all | **Implemented** | Structured logging masks or omits them; the DSN is never echoed on a database error. Client addresses are truncated to /24 before being stored. |
| I5 | A stack trace or internal detail leaks in an error response | B4 | **Implemented** | RFC 7807 problem documents with a correlation id and no internals. `test_problem_document_has_the_rfc_7807_shape`. |
| I6 | A superuser database connection bypasses RLS entirely | B5 | **Implemented, with a stated caveat** | PostgreSQL exempts superusers and `BYPASSRLS` roles from RLS *unconditionally*; `FORCE` closes the table-owner gap, not that one. The application therefore connects as an ordinary role and `SET ROLE fleet_app`. Two tests assert the bypass exists so nobody rediscovers it as a surprise; recorded in ADR-0006. |
| I7 | Secrets in the repository or in an image layer | B6 | **Implemented** | No credentials in code, config, tests or history; `.env.example` holds placeholders only, database roles are `NOLOGIN`. `test_no_migration_contains_a_hardcoded_password`. |
| I8 | A token is stolen from browser storage via XSS | B4 | **Implemented for now** | The access token is held in memory, never `localStorage`, so one XSS does not become a durable stolen session. The final flow depends on D4. |
| I9 | Raw telemetry in MongoDB is readable without tenant scoping | B3/B5 | **OPEN** | Mongo has no row-level security and the collection is keyed by VIN. Nothing in the API reads it today, so there is no exposed path — but a future reader would have to enforce tenancy in application code, which is exactly the weaker position RLS was chosen to avoid. Named here so it is a decision when it happens rather than an oversight. |

## D — Denial of service

| # | Threat | Boundary | Status | Mitigation |
|---|---|---|---|---|
| D1 | A caller exhausts the API with expensive queries | B4 | **Implemented** | Redis-backed per-tenant rate limiting returning `429` with `Retry-After`; hard maximum page size; keyset pagination so deep pages cost the same as shallow ones; every query bounded by `LIMIT`. An uncapped `limit` is rejected (`test_the_alert_limit_is_capped`). |
| D2 | A 30-day leaderboard aggregation over raw events stalls the API | B5 | **Implemented, after measurement** | The leaderboard reads the `vehicle_daily` rollup rather than aggregating raw events per request. This was a real defect: measured at 200,000 rows the index bought only 1.07x because a month of one tenant's events is a large fraction of the table, so the planner correctly chose a sequential scan. See `docs/evidence/sql/query-plans.md`. |
| D3 | A slow or dead dependency hangs the request path | B5 | **Implemented** | Timeouts on every external call, circuit breakers on downstream calls, `/readyz` reports degraded rather than failing, and the API serves partial or stale data with a flag instead of a 500 storm. |
| D4 | A malformed event crashes the consumer and stalls a partition | B2 | **Implemented** | Bad input goes to the DLQ; the consumer never dies on payload content. Bounded queues and pause/resume apply back-pressure rather than growing memory without limit. |
| D5 | Unbounded storage growth becomes a cost denial-of-service | B3 | **PARTIAL** | Raw telemetry has a TTL index in MongoDB and Postgres large tables are partitioned by time with automated creation. At the measured 479 bytes/event, 100K events/s is **4.14 TB/day**, so retention is a cost control and not a preference. Partition *drop* automation and a cold tier are pending D6. |
| D6 | A flood of duplicate alerts buries the real ones | B3 | **Implemented** | A unique suppression index on `(vin, alert_type, idle_event_started_at)` makes an alert storm impossible even if application logic regresses. This is why `alerts` is deliberately not partitioned — PostgreSQL requires a unique index on a partitioned table to include the partition key, which would permit exactly the duplicates the index exists to stop. |
| D7 | The rate limiter itself fails when Redis is down | B4 | **PARTIAL** | The API degrades to serving without rate limiting rather than refusing all traffic. That is the right availability choice and the wrong security choice, and it is a deliberate trade — stated here rather than discovered during an incident. |

## E — Elevation of privilege

| # | Threat | Boundary | Status | Mitigation |
|---|---|---|---|---|
| E1 | A finance role performs an operations action, or sees operations data | B4 | **Implemented** | RBAC on the verified role claim, with location precision keyed to it. |
| E2 | The application role gains write access to rollups or reference data | B5 | **Implemented** | `fleet_app` has `SELECT` only on `utilisation_window`, `idle_burn_rates` and `fuel_prices`. It could otherwise make its own coverage figure look complete — the exact thing that table was added to prevent. |
| E3 | The batch job's cross-tenant access is used for more than maintenance | B3 | **Accepted and named** | Rollups genuinely cross tenants, so `fleet_batch` is `BYPASSRLS`. It is a **separate, named role** so that "this code sees every tenant" is a visible property of the deployment rather than an accident, and it has `UPDATE`/`DELETE` revoked on `audit_log`. ADR-0006. |
| E4 | The erasure job deletes the fleet's business record | B3 | **Implemented** | `fleet_erasure` has `SELECT` and no `DELETE` on `trips` and `idle_events`. Deleting them would erase the fleet's own record of its fuel cost and make a past finance report irreproducible. |
| E5 | Container escape or lateral movement in the cluster | B6 | **PARTIAL** | Non-root containers, read-only root filesystem where possible, pinned digests. Network policies, pod security admission and a service mesh are not configured. |
| E6 | CI supply-chain compromise injects code or exfiltrates secrets | B6 | **PARTIAL** | Pinned action versions, a locked dependency file, SAST and image scanning in CI. No provenance attestation or dependency-review gate. |

---

## Open items, ranked by what I would fix first

1. **S3 — producer identity is not bound to a VIN.** The largest real gap. Everything
   downstream trusts that an event for a VIN came from that vehicle, and nothing enforces
   it. mTLS with the VIN in the certificate subject, checked at the processor.
2. **I9 — MongoDB has no tenant enforcement.** Harmless today because nothing reads it;
   a trap for the first feature that does.
3. **D7 — the rate limiter fails open.** Defensible, but it should be a configured choice
   with an alert, not a default.
4. **T6 — no approval path on the fuel price table.** A mis-entered price changes every
   published cost figure and is only visible after the fact.
5. **R4 / E5 / E6 — cluster-level audit, network policy and supply-chain attestation.**
   All deployment-time, all blocked on the cloud account constraint rather than on design.

## Method and limits

Threats were enumerated per STRIDE category against each trust boundary, then matched to
code. Categories that produced nothing specific to this system were dropped rather than
padded with generic entries.

This is a **design-time** model. It is not a penetration test and no exploitation was
attempted. The scan evidence that complements it lives in `docs/evidence/security/`, and
where that directory is empty the corresponding claim has not been made.
