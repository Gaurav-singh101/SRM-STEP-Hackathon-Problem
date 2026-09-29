# BDD acceptance evidence

**Measured, not claimed.** The JUnit XML beside this file is produced by a real run.

| Field | Value |
|---|---|
| Date (UTC) | 2026-09-29 |
| Commit | recorded by the commit that adds this file |
| Command | `uv run behave --junit --junit-directory docs/evidence/acceptance` |
| Tool | behave 1.2.6, `strict = true` (an undefined step fails the run) |
| Platform | win32, local developer laptop; also runs in CI on every push |

## Result

| Metric | Value |
|---|---|
| Features | **4 passed**, 0 failed |
| Scenarios | **19 passed**, 0 failed |
| Steps | **105 passed**, 0 failed, 0 undefined |
| Duration | 0.19 s |

## What the scenarios cover

One feature per product question in CLAUDE.md §1, plus the cross-cutting conditions
under which any of those answers may be shown to anyone.

| Feature | Question it answers |
|---|---|
| `idle_cost.feature` | Which vehicles generate idle-related cost, and how much fuel and money is tied to it |
| `utilisation.feature` | Which vehicles show low or abnormal utilisation |
| `attention.feature` | What needs operational attention, and the fleet-level trend |
| `trust.feature` | Tenant isolation, location privacy by role, audit trail |

## What they run against

The FastAPI application that ships, through a genuinely RS256-signed token, so routing,
authentication, authorisation, tenant scoping, serialisation and the assumption-reporting
rules are all exercised for real. Only the repository is a double.

That split is deliberate rather than convenient. These scenarios are about the product
questions, and each needs a specific fleet arranged to make the answer unambiguous —
a long-tailed cost distribution, a vehicle whose own signals disagree, a window where most
vehicles could not be scored. Arranging those in PostgreSQL would make the scenarios about
seeding. The schema, the row-level security policies and the partial indexes are covered
against a real PostgreSQL by `tests/integration/` (49 cases).

## Scenarios that exist because of a real failure

Three of them pin mistakes found during development, so they cannot come back:

- **"Nothing scored yet is not the same as everything scored"** — `unscored_no_telemetry`
  was declared in the API schema but never populated, so every response said 0 while the
  batch job had just failed to score 1,808 vehicles.
- **"A replayed alert does not pass its data age off as a reaction time"** — a first run
  reported an alert latency of 83,711 s against a 5 s target. Nothing was slow; the figure
  measured how old the replayed data was.
- **"A vehicle whose own signals disagree is reported separately, not as low"** — the
  `abnormal` band was reached by MAD, which assumes one mode. The fleet is multi-modal, so
  21.5% of vehicles were flagged. Now decided by component spread.

## Verified to discriminate

A passing suite proves nothing unless a break fails it. The coverage scenario was checked
by mutation: forcing the route to return `coverage=None` produced

```
ASSERT FAILED: coverage must be reported, or the table reads as the fleet
18 scenarios passed, 1 failed
```

and reverting the mutation restored 19 passed.
