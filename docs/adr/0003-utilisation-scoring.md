# ADR-0003: Utilisation scoring

- **Status:** Accepted
- **Date:** 2026-09-29
- **Resolves:** D3
- **Classification:** Approved project decision. Weights and thresholds are PROPOSED.

## Context

"Which vehicles show low or abnormal utilisation?" is one of the five questions
the platform must answer. The obvious metric is wrong in a way that matters.

## Options considered

1. **Time ratio** - `moving_time / engine_on_time`. A van that runs one hour and
   moves 55 minutes of it scores 92% while sitting unused six days a week. That
   inverts the answer the user needs, and it is the asset they should sell.
2. **Composite score** from three normalised components, relative to vehicle class.
3. **Peer percentile only.** Adapts to the fleet automatically but structurally
   cannot say "the whole fleet is under-used", which is a question finance asks.

## Decision

**Option 2.**

```
score = 100 x (0.40 x active_day_ratio
             + 0.35 x moving_hours_ratio
             + 0.25 x distance_ratio)
```

Each ratio is normalised against the *class* median and clamped to 1.0, so a
heavy truck is never judged against a city car and one very busy vehicle cannot
push a score above 100 and distort the leaderboard.

Bands: `low` below 40, `normal` 40-85, `high` above 85, plus `abnormal` for a
statistical outlier on any component - typically high distance with almost no
active days, which is a data-quality or misuse signal rather than low use, so it
is reported separately instead of being lumped into `low`.

**Outlier detection uses median and MAD, not mean and standard deviation.** Mean
and stdev are dragged by the very outliers we are hunting, so one extreme vehicle
raises the bar enough to hide itself.

**The MAD is floored at 10% of the class median.** This was found by testing, not
by design: on a tightly clustered class the raw MAD is tiny, so an unremarkable
vehicle 26 hours below a 132-hour median computed to -3.6 robust sigma and the
`abnormal` band filled with false positives. The floor refuses to claim more
precision than a small, tight sample supports. It is configurable, and setting it
to zero restores the raw behaviour, so the trade-off stays visible.

**Classes with fewer than 5 peers never assert abnormality.** MAD from three
vehicles describes noise, not spread.

## Consequences

- All three components are returned beside the score. A single opaque number does
  not survive a fleet manager asking "why is this 31?".
- The score is a relative measure. A fleet that is uniformly under-used will show
  mid-range scores, because every vehicle matches its peers. The absolute
  `active_day_ratio` component is what stops that from being completely hidden.
- `sample_size` is carried on every baseline, so a weak baseline is visible.
- Implementation: `libs/fleet_common/fleet_common/domain/utilisation.py`.
  Tests: `libs/fleet_common/tests/test_utilisation.py` (26 cases, 4 property-based).
