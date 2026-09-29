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

---

## Amendment, 2026-09-29: the ABNORMAL band measures the vehicle against itself

The original design flagged a vehicle as abnormal when any component was more than 3
robust sigma from its **class median**. Run against real data, that was wrong.

### What went wrong

On a 200-vehicle fleet with ten days of telemetry, **21% of vehicles landed in the
ABNORMAL band**. An exception band containing a fifth of the fleet is not an exception;
it is noise that a user learns to ignore.

The cause is a mismatch between the statistic and the data. MAD is robust to outliers
but assumes a roughly **unimodal** distribution. A real fleet is not unimodal: it has
distinct duty tiers - heavy, normal, light, and barely-used assets. MAD then measures
only the spread of the dominant tier and treats every other tier as anomalous. The
observed baselines showed this plainly: 183 of 200 vehicles sat at exactly 90% active
days, giving `mad_active_day_ratio = 0.000`.

Tuning the MAD floor did not fix it either:

| MAD floor fraction | Vehicles flagged abnormal |
|---|---|
| 0.10 | 28% |
| 0.20 | 16% |
| 0.30 | 0% |
| 0.40 | 0% |

The cliff between 0.2 and 0.3 is the signature of a multi-modal distribution: there is
no threshold that separates "busy" from "broken", because the gap the threshold lands
in is the gap *between tiers*, not between normal and anomalous.

The deeper problem is that the test answered the wrong question. **A heavy-duty truck
doing three times the class median is not anomalous, it is working.** Flagging it tells
a fleet manager nothing they can act on.

### Decision

**ABNORMAL now measures a vehicle against itself: the gap between its highest and
lowest normalised component.**

```
component_spread = max(active_day, moving_hours, distance) - min(same)
abnormal when component_spread > 0.6
```

This is what ADR-0003 always *described* - "high distance but near-zero active days" -
and it is immune to the shape of the fleet's distribution, because no peer comparison
is involved. It also works for a small fleet, where MAD from three peers could say
nothing at all.

The threshold was measured, not guessed: 0.6 flags about 7% of vehicles by spread, and
3 of 200 (1.5%) after banding, against 25% at a threshold of 0.4.

### What it catches now

The top result on real data:

> recorded active on 90% of days but reached only 25% of the expected moving time -
> in service daily yet barely used

That is genuinely worth investigating: the vehicle is nominally in service every day
and doing almost nothing. Either it is under-used despite appearing busy, or a signal
is wrong. Both warrant a look, which is exactly what an exception band is for.

The band also explains itself in plain language rather than quoting a statistic, because
a band nobody understands is a band nobody acts on.

### Consequences

- `max_abs_z` is still computed and reported as context - "how far from the class
  median" remains useful to a reader - but it no longer decides the band.
- `mad_floor_fraction` and `abnormal_z` are retained for that reporting path, so the
  earlier analysis is not lost.
- A new `component_spread` column stores the deciding value (migration 005), so a band
  can be explained after the fact rather than only recomputed.
- The minimum-baseline rule still applies to the **score**, which is peer-relative. Only
  the abnormality test became peer-independent.
- Implementation: `libs/fleet_common/fleet_common/domain/utilisation.py`.
  Tests: `libs/fleet_common/tests/test_utilisation.py`, including a regression test that
  a high z-score alone must not make a vehicle abnormal.
