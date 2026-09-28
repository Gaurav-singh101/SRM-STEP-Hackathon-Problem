# ADR-0001: Idle definition and thresholds

- **Status:** Accepted
- **Date:** 2026-09-29
- **Resolves:** D1
- **Classification:** Approved project decision. Threshold *values* are PROPOSED synthetic defaults, not measured or sourced figures.

## Context

Every fuel and cost figure in this product derives from idle segments, so the
definition of "idle" is the most load-bearing business rule we have. The
hackathon PDF fixes no definition and its illustrative event carries no ignition,
RPM or fuel signal at all, so the definition and the signals to support it both
had to be designed (see ADR-0004).

Two forces pull against each other. Count too eagerly and every traffic light
becomes a cost line, so the numbers inflate and a finance user stops trusting
them. Count too conservatively and genuine waste hides.

## Options considered

1. **Single threshold** - ignition on and speed below one bound for N seconds.
   Trivial to explain and test, but bills traffic-light and loading-bay idling.
2. **Tiered** - short idle ignored, billable idle past a threshold, excessive idle
   flagged for alerting. Separates actionable waste from noise, and gives the
   real-time alert in ADR-0010 its trigger for free.
3. **Context-aware** - tiered, plus exclusions for depot geofences, active PTO
   (refrigeration, crane, lift) and extreme ambient temperature. Most defensible
   to a finance user, but needs extra signals and much more simulator work.

## Decision

**Option 2**, with option 3's PTO and depot exclusions implemented as
configuration flags that default to off.

| Parameter | Default | Meaning |
|---|---|---|
| `IDLE_MIN_SECONDS` | 180 | Below this, idling is treated as unavoidable and not billed |
| `IDLE_ENTRY_SPEED_KPH` | 2.0 | Speed at or below which a stationary period may begin |
| `IDLE_EXIT_SPEED_KPH` | 5.0 | Speed above which an open segment closes |
| `IDLE_EXCESSIVE_SECONDS` | 600 | Crossing this while still idling raises an alert |
| `IDLE_GAP_TOLERANCE_SECONDS` | 30 | Largest sample gap bridged inside one segment |
| `IDLE_EXCLUDE_PTO_ACTIVE` | false | Optional exclusion for productive stationary work |

Two mechanisms matter more than the numbers:

**Hysteresis.** Entry and exit use different speed bounds. With a single bound,
GPS jitter around the threshold flaps the state machine and shatters one real
idle period into many short ones - each then falling below `IDLE_MIN_SECONDS` and
vanishing from the bill entirely. The guard is enforced in `IdleConfig`, which
refuses an exit bound below the entry bound.

**Gap tolerance.** Samples go missing. Bridging gaps up to a bound keeps one real
idle period intact; a gap beyond the bound closes the segment, because we cannot
honestly claim the engine ran through unobserved time.

Segment duration spans first to last *observed* sample, never wall-clock span.
The estimate is therefore conservative by construction: it under-claims rather
than over-claims, which is the right direction of error for a cost figure.

## Consequences

- Engine-off and accessory-only periods are never idle: neither burns fuel.
- Sub-threshold idling is invisible in cost reporting by design. If a user later
  wants "total stationary time", that is a separate metric, not a threshold change.
- `max_gap_seconds` is carried on every segment so a figure resting on sparse
  samples is visibly weaker than one resting on dense samples.
- The thresholds are synthetic defaults. Presenting them as industry-validated
  would breach CLAUDE.md S3. Any real deployment must re-derive them per fleet.
- Implementation: `libs/fleet_common/fleet_common/domain/idle.py`.
  Tests: `libs/fleet_common/tests/test_idle.py` (32 cases, 5 property-based).
