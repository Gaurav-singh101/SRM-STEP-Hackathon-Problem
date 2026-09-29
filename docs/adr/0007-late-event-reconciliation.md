# ADR-0007: Late-event reconciliation

- **Status:** Accepted
- **Date:** 2026-09-30
- **Resolves:** D6 (the reconciliation half; the cold-tier store remains open)
- **Classification:** Approved project decision

## Context

CLAUDE.md §8 says late events "are reconciled by the batch job. **Never silently
drop.**" The processor holds up its end: an event arriving beyond
`ALLOWED_LATENESS_SECONDS` past its vehicle's watermark is published to
`telemetry.late` rather than discarded, with the reason recorded.

**Nothing consumed that topic.** Events accumulated there and reached no store, so
in practice the guarantee was not met — the data was not dropped, but it was not
reconciled either, which is the same outcome for anyone reading a cost figure.

The difficulty is that a late event is a *raw telemetry sample*, while the thing
that would change is a *derived segment*. An idle event is produced by a state
machine folding over an ordered run of samples for one vehicle. A late sample
arriving after that segment was written cannot simply be inserted: it may extend a
segment, split one, merge two, or create a new one. Feeding one isolated sample
back through the state machine would produce nonsense.

So reconciliation has to mean **recomputing the affected vehicle-day from the
complete set of samples**, which requires those samples to still exist.

They do not always. Raw telemetry lives in MongoDB under
`MONGO_RAW_TTL_SECONDS`, currently **48 hours** — a hot tier sized by cost, not by
convenience. At the measured 479 bytes/event, 100,000 events/s is **4.14 TB/day**,
so raw retention is a money decision (ADR-0004, CLAUDE.md §8).

## Options considered

1. **Re-segment inside the TTL window; count and record anything older.** A late
   event whose vehicle-day still has its raw samples is reconciled properly. One
   older than that is written to a reconciliation log with a reason code and a
   count, and reported. Bounded work, honest about its own limit.

2. **Raise the Mongo TTL so every late event is reconcilable.** 30 days of raw
   telemetry makes option 1's gap disappear. It also multiplies the hot tier by
   15, which at fleet scale is roughly 124 TB. Storage buys a correctness
   guarantee, which is a legitimate trade — but it is a trade the operator should
   make deliberately, not one this ADR should make on their behalf.

3. **Persist late events without re-segmenting**, flagging the affected
   vehicle-days as stale. Cheapest. Leaves derived figures knowingly wrong and
   pushes the problem to whoever reads them.

## Decision

**Option 1.**

Late events are consumed from `telemetry.late` by a batch subcommand, grouped by
`(tenant_id, vin, event day)`, and each affected vehicle-day is handled as a unit:

1. **Upsert the late samples into MongoDB** by `event_id`, so the raw record is
   complete regardless of what happens next.
2. **Read that vehicle-day's full sample set back**, ordered by event time.
3. **If the day's samples are incomplete** — because the TTL has already expired
   part of the window — stop, and record the vehicle-day as *unreconcilable* with
   a reason and a count. Do not recompute from a partial set: a segment rebuilt
   from half its samples is worse than a stale one, because it looks current.
4. **Otherwise re-run idle and trip segmentation** over the complete ordered set
   and upsert the resulting segments. Every derived write in this system is an
   upsert keyed on the segment key, which is precisely what makes this safe to
   repeat (`ON CONFLICT (vin, started_at)`).
5. **Recompute that day's rollups**, since `vehicle_daily` and `fleet_daily` are
   derived from the segments and the leaderboard now reads them.
6. **Record the run** — events consumed, vehicle-days reconciled, segments
   changed, events unreconcilable — in a `reconciliation_log` table.

Three things this decision is deliberately strict about:

**The gap is counted, never hidden.** "We could not reconcile 412 events because
their raw samples had expired" is a statement an operator can act on: raise the
TTL, or accept it. "Reconciliation complete" over the same data is a lie. The log
makes the first one available and the second one impossible to write honestly.

**Offsets commit only after the vehicle-day is durable**, matching the main
processor. At-least-once delivery plus idempotent upserts means a re-delivered
late event corrects rather than duplicates.

**Reconciliation replaces a vehicle-day, scoped and transactional.**

This is an **amendment**, made while implementing. The ADR originally said
reconciliation "never issues a `DELETE`", on the reasoning that a bug must not be
able to erase the fleet's business record. That constraint was right in spirit and
wrong in mechanism, and it would have shipped a reconciler that quietly failed at
the main thing it exists to do.

`write_idle_events` upserts with `ON CONFLICT (vin, started_at) DO NOTHING`, which
is exactly right for the stream: a replayed batch must be inert. It is wrong for
reconciliation, in two ways:

- A late sample that **extends** a segment produces the same `started_at` with a
  longer duration. `DO NOTHING` keeps the stale duration and the stale cost, and
  the run reports success having changed nothing.
- A late sample that **fills the gap** which separated two segments produces one
  merged segment where two existed. Inserting the merged row leaves both originals
  behind, so the same idle time is now counted twice.

The second case is not hypothetical — it is the characteristic effect of a late
event on gap-tolerant segmentation, and it inflates cost rather than
under-reporting it.

So reconciliation **deletes and rewrites the vehicle-day it recomputed**, inside
one transaction, bounded by `vin` and that day's time range. The original
constraint survives as a scope rule rather than a prohibition:

- only rows for the exact `(vin, day)` being recomputed, never a broader predicate;
- the delete and the insert are one transaction, so a failure between them cannot
  leave a hole;
- `trips` likewise, and nothing else at all.

There is precedent in this codebase for the same pattern and the same reasoning:
`_clear_window_scores` removes a utilisation window before rewriting it, because a
vehicle that stops reporting would otherwise keep whatever score it last had, and
a stale score looks identical to a current one. A stale idle segment is worse,
because someone is billed for it.

## Consequences

**Good**

- The §8 guarantee becomes true in practice, and demonstrable: the log is the
  evidence.
- Recomputing a whole vehicle-day rather than patching a segment means the result
  is identical to what the stream would have produced had the event arrived on
  time. No special-case code path produces a second definition of an idle event.
- The TTL boundary is now visible. It was always there; it was simply nobody's
  problem before.

**Bad, and accepted**

- Reconciliation is only as good as the hot tier. With a 48-hour TTL, an event
  five days late is counted and not corrected. That is a real limitation and it is
  reported rather than papered over.
- Recomputing a vehicle-day is far more work than inserting an event. This is a
  batch job by design, not a hot path, and late events are rare enough (measured
  well under 1% of a run) that the cost is irrelevant.
- A reconciled day changes numbers that someone may already have read. The
  rollups carry `recomputed_at` for exactly this, and the leaderboard already
  reports `data_through` and `computed_at`.

**Open**

- The **cold tier** stays undecided (the rest of D6). If Parquet-on-object-storage
  lands later, reconciliation could read from it beyond the Mongo TTL and option
  1's gap would narrow without a 15x hot-tier bill. The reconciler is therefore
  written against a *sample source*, not against MongoDB directly, so that
  substitution does not require rewriting it.
- Whether to raise `MONGO_RAW_TTL_SECONDS` is left to the operator, now that the
  log will tell them what it costs them.

## Verification

- Property: for any interleaving of on-time and late events for one vehicle-day,
  reconciliation converges to the same segments as feeding every event in
  timestamp order once. This is the correctness claim, and it is the one worth a
  property-based test.
- **The merge case specifically**: two segments separated by a gap, plus a late
  sample that fills it, must leave **one** segment afterwards and not three. This
  is the case the original no-delete rule would have got wrong, so it is the one
  that needs an integration test against a real database rather than a unit test
  against a fake sink.
- **The extend case**: a late sample after a segment's end must leave the same
  `started_at` with a longer duration and a proportionally larger cost — not the
  original duration.
- A late event inside the TTL changes the affected segments and the day's rollup.
- A late event outside the TTL leaves the segments untouched and increments the
  unreconcilable count with its reason.
- Re-running the reconciler over the same messages changes nothing further
  (idempotence).
- Evidence lands in `docs/evidence/compliance/` alongside the erasure run.
