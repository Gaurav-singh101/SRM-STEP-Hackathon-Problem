# ADR-0002: Idle fuel and cost estimation

- **Status:** Accepted
- **Date:** 2026-09-29
- **Resolves:** D2, and part of D11 (money representation)
- **Classification:** Approved project decision. Burn rates are PROPOSED synthetic values.

## Context

The product's central claim is a money figure. A finance manager will act on it
only if they can reproduce it, and will stop trusting the whole system the first
time they cannot. Auditability therefore outranks sophistication here.

## Options considered

1. **Configured burn rate** per (powertrain, vehicle class), multiplied by idle
   hours and fuel price. Checkable by hand. Ignores per-vehicle variation.
2. **Displacement/RPM-derived** rate. Sounds more physical, but idle RPM is
   near-constant, so it adds inputs for almost no accuracy gain.
3. **Measured from telemetry** - `fuel_level_pct` delta or `fuel_rate_lph`. Most
   faithful where the signal exists, but tank-level resolution is around 1%, so
   over a three-minute idle the sensor noise can exceed the signal.

## Decision

**Option 1 as the primary estimate**, stamped `formula_version = "idle-fuel-v1"`,
**with option 3 computed as a secondary cross-check** whenever the device reports
`fuel_rate_lph`, and the signed divergence between the two reported alongside.

The cross-check is what makes a misconfigured burn rate visible instead of
silently producing wrong money.

Every estimate carries its full assumption set - `formula_version`,
`burn_rate_lph`, `fuel_price_per_litre`, `price_id`, `idle_threshold_seconds` and
`duration_seconds` - through the domain, the API and into the UI.

**Money is `Decimal`, quantised to four places with half-up rounding**, matching
`numeric(14,4)` in Postgres so nothing rounds at the boundary. Binary floating
point cannot represent most decimal fractions exactly; accumulating float
currency across millions of segments drifts, and a drifting total is worse than
no total. Half-up rather than banker's rounding, because that is what a user gets
when they check the arithmetic by hand.

Summing refuses to mix currencies or formula versions. Either would produce a
meaningless number that still looks like money, so both are hard errors.

## Consequences

- BEVs raise `BevHasNoIdleFuelError` rather than returning zero. Zero would flow
  into a fleet total as if it were measured (see ADR-0009 for the D9 rationale).
- A (powertrain, class) pair with no configured rate raises `KeyError`. Failing
  loudly beats inventing a default that quietly produces money.
- Burn rates are plausible engineering placeholders for a synthetic fleet. They
  are **not** sourced or measured and must never be presented as either.
- Implementation: `libs/fleet_common/fleet_common/domain/fuel.py`.
  Tests: `libs/fleet_common/tests/test_fuel.py` (22 cases).
