# ADR-0008: EV handling, critical-alert definition, and the remaining tenancy questions

- **Status:** Accepted
- **Date:** 2026-09-30
- **Resolves:** D9, D10, and the parts of D11 that ADR-0002 and ADR-0006 left open
- **Classification:** Approved project decision

Three smaller decisions recorded together because each was implemented against the interim
rule in CLAUDE.md §5 and needs its record closed before submission. Separating them into three
ADRs would be filing, not thinking.

---

## D9 — Electric vehicles

### Context

Idle *fuel* cost does not apply to a battery-electric vehicle. A BEV can be stationary with
systems powered and consume energy, but it burns no fuel, so the entire cost model in ADR-0002
— burn rate × duration × fuel price — is undefined for it.

The naive handling is to compute zero. That is wrong in a way that matters: a fleet total that
silently includes 14 BEVs contributing ₹0 reads as "these vehicles cost nothing to leave
idling", which is a claim about their efficiency rather than a statement about scope.

### Decision

**ICE and hybrid vehicles are in scope for fuel cost. BEVs are in scope for utilisation only,
and their exclusion is always visible.**

Three mechanics enforce it:

1. `Powertrain.burns_fuel` is the single source of truth, read from the `powertrains`
   reference table rather than inferred from a code string.
2. `estimate_idle_cost` raises `BevHasNoIdleFuelError` rather than returning zero. A caller
   cannot accidentally treat "not applicable" as "nothing".
3. Any total covering fewer vehicles than the fleet carries `cost_total_is_partial` and
   `excluded_bev_count`, and the UI renders a notice naming the count.

BEVs still appear in utilisation, the leaderboard's absence of them is stated
(`excludes_bev`), and Vehicle Detail shows a BEV's utilisation score beside "not applicable"
for cost — because the score is the only figure that applies to it.

### Consequences

A reader can never see a fuel-cost total without knowing whether it covers their whole fleet.
The cost is one extra field on several responses and a notice component, which is cheap.

Not addressed: **energy** cost for BEVs. Idle energy draw is real and the platform does not
price it. That is a scope decision, not an oversight, and it is the natural next feature for a
mixed fleet.

---

## D10 — What counts as a critical alert

### Context

The PDF sets a 5-second target for critical alerts. "Critical" needs a definition, and the
definition needs to be narrow: an alert type that fires often is not critical, it is noise, and
a queue nobody trusts is worse than no queue.

### Decision

**Two alert types, both tied to idle cost.**

| Type | Severity | Fires when |
|---|---|---|
| `EXCESSIVE_IDLE_IN_PROGRESS` | warning, or critical past 2× the threshold | An idle segment crosses `IDLE_EXCESSIVE_SECONDS` (600 s) |
| `IDLE_COST_SPIKE` | warning | A vehicle's daily idle cost exceeds its own trailing norm by a configured factor |

Three properties are load-bearing:

**Suppression is enforced by the database, not by application logic.** A unique index on
`(vin, alert_type, idle_event_started_at)` makes a duplicate alert impossible even if the
pipeline regresses. This is why `alerts` is deliberately **not partitioned** — PostgreSQL
requires a unique index on a partitioned table to include the partition key, and adding a
timestamp would permit exactly the duplicates the index exists to prevent. Alerts are low
volume, so losing partition pruning is a cheap price for a guarantee that an alert storm cannot
happen.

**The SLO is measured as processing latency, not end-to-end.** This is the correction that
mattered. The first implementation measured `persisted_at − triggering_event_ts` and reported
**83,711 seconds** against a 5-second target. Nothing was slow: the pipeline was replaying ten
days of history and the metric was measuring *how old the data was*. There are now two
figures, `end_to_end_latency_seconds` and `processing_latency_seconds`, plus an `is_backfill`
flag saying which one means anything. Measured processing latency: **2.86 s**.

**Alerts fire once per segment, on the transition.** The state machine emits when a segment
crosses the threshold, not on every subsequent sample.

### Consequences

The queue stays small enough to be a work queue. A latency figure can never be quoted without
the context that decides whether it is meaningful — which is the property the original metric
lacked.

Not addressed: alert delivery. There is no email, webhook or push integration; alerts surface
in the Needs Attention view and via the API. Routing is a deployment concern and would need a
channel decision this project has not made.

---

## D11 — Currency, fuel price source, and multi-tenancy

ADR-0002 settled money representation and ADR-0006 settled tenant isolation. Two questions
were left open.

### Fuel price source

**Decision: a temporal price table per tenant, never a constant.**

`fuel_prices` is keyed on `(tenant_id, fuel_type)` with `effective_from` / `effective_to`, and
every idle event stores the `price_id` it was costed with. Prices are **appended and closed**,
never updated in place.

The reason is reproducibility. A cost figure published last month must still be derivable
after a price change, and an `UPDATE` would make every historical report silently wrong while
appearing to improve accuracy. The stored `price_id` plus `formula_version` means any figure
can be recomputed with the rules that produced it.

Prices are synthetic and labelled PROPOSED. `fleet_app` has `SELECT` only on the table; only
the batch role writes, so the API cannot alter the basis of its own figures.

### Currency

**Decision: one currency per tenant, from `tenants.currency`, defaulting to INR.**

Not multi-currency per fleet, and not a single global currency. A tenant operating in two
currencies would need conversion rates with their own temporal table, which is a second
reproducibility problem this project does not need to solve. Money is `Decimal` end to end,
`numeric(14,4)` in PostgreSQL, and serialised as a **string** — a JSON number is an IEEE 754
double in a browser, and a rounded currency figure on a finance screen is a defect.

### Multi-tenancy model

**Decision: shared schema with `tenant_id`, enforced twice** — in the data-access layer and by
PostgreSQL RLS. Recorded fully in ADR-0006; restated here so D11 closes.

One addition since: **rollup tables are tenant-scoped too**, including `utilisation_window`.
Easy to miss, because a rollup holds no driver or location data and looks like harmless
aggregate. It is not — it tells a competitor how many vehicles the other tenant runs, and
fleet size is commercially sensitive. Covered by an integration test.

`reconciliation_log` is deliberately **not** tenant-scoped: it is operational metadata about a
job run, holds counts only, and carries no VIN or tenant identifier.

### Consequences

Prices and currency are configuration with versioned provenance, so every figure is
reproducible. The cost is a join and a slightly larger event row.

Not addressed: currency conversion, per-fleet currency, and tax treatment. All three are real
in a commercial product and none is needed to answer the five product questions.
