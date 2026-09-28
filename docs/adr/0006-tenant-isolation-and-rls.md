# ADR-0006: Multi-tenancy model and tenant isolation

- **Status:** Accepted
- **Date:** 2026-09-29
- **Resolves:** the tenancy half of D11
- **Classification:** Approved project decision

## Context

The platform is multi-tenant, and PDF S11 requires "RBAC with tenant isolation".
A cross-tenant leak is the worst failure this system can have: it is
unrecoverable, reportable under GDPR and DPDP, and it destroys the product's
premise. Application-layer filtering alone means one forgotten `WHERE` clause
causes it.

## Options considered

1. **Shared schema, `tenant_id` column** on every business table. One migration
   path, one connection pool, cheap. Isolation rests entirely on query
   correctness unless the database helps.
2. **Schema per tenant.** Strong separation, but every migration runs N times and
   cross-tenant reporting becomes awkward. For a single developer on a four-day
   schedule, N migrations is a real operational risk.
3. **Database per tenant.** Strongest, and the most expensive. Not affordable on
   a free-tier account, and overkill at this scale.

## Decision

**Option 1, with two independent enforcement points.**

1. **Data-access layer.** The repository base refuses to build a query without a
   tenant filter. `tenant_id` is taken from the validated token claim only, never
   from a request body, query string or header (CLAUDE.md S3).
2. **PostgreSQL row-level security**, as defence in depth. Every tenant-scoped
   table has a `tenant_isolation` policy comparing `tenant_id` against
   `current_tenant_id()`, which reads the `app.tenant_id` session setting.

Three details that turned out to matter more than the policy text:

**Fail closed.** `current_tenant_id()` returns `NULL` when the setting is unset or
empty, and every policy compares with `=`. `NULL = anything` is not true, so an
unset context matches **no** rows. The failure mode of forgetting to set the
context is a broken page, not a data breach.

**`FORCE ROW LEVEL SECURITY`, not just `ENABLE`.** `ENABLE` exempts the table
owner, and the application may well connect as the owner of its own schema. FORCE
closes that.

**The application must not connect as a superuser.** PostgreSQL exempts
superusers and `BYPASSRLS` roles from RLS *unconditionally* - FORCE does not
change this. So the policies are only load-bearing if the connection role is
ordinary. The application therefore does `SET ROLE fleet_app` on pool checkout.
`fleet_app` is `NOLOGIN`, reached only via `SET ROLE` from an already
authenticated connection, so this adds **no new credential to manage**
(CLAUDE.md S3).

This last point was found by a failing test, not by design: the first run of the
isolation suite showed a `DELETE` reaching both tenants, because the test
connected as the container's superuser. The tests now run through `app_conn`,
which performs the `SET ROLE`, and two further tests assert the superuser bypass
explicitly so it can never quietly become a surprise.

**Privileges are narrowed beyond isolation:**

| Restriction | Reason |
|---|---|
| `fleet_app` has no `UPDATE`/`DELETE` on `audit_log` or `erasure_log` | PDF S11 wants an audit trail for every access. A trail the application can rewrite is not evidence of anything. |
| `fleet_app` has no write access to `fuel_prices`, `idle_burn_rates`, `powertrains`, `vehicle_classes`, `fuel_types` | Changing a price or burn rate silently restates historical money. That is a migration or an admin action, never a request handler. |
| `fleet_batch` is a separate `BYPASSRLS` role | Rollup rebuilds and partition management genuinely need to cross tenants. Isolating that in one named role makes "this code sees every tenant" a visible property of the deployment rather than an accident. |

## Consequences

- Any new table carrying `tenant_id` needs a policy. A test enumerates
  `pg_attribute` for `tenant_id` columns and fails if one has no policy, so this
  cannot be forgotten. Partitions are excluded: they inherit the parent's policy.
- Tenant context is applied with `set_config('app.tenant_id', $1, false)`, never
  `SET app.tenant_id = ...`. `SET` accepts no bind parameters, so using it would
  mean interpolating a value into SQL - the injection CLAUDE.md S10 forbids.
- RLS adds a predicate to every query. The tenant indexes are all
  `(tenant_id, ...)` leading-column, so the planner uses them rather than
  filtering after the fact; the `EXPLAIN` evidence on Day 3 will confirm it.
- **Deployment constraint:** the API's database user must not be a superuser and
  must not hold `BYPASSRLS`. This belongs in the Terraform and Helm values, and is
  the single most important line in this ADR.
- Implementation: `services/batch/batch/migrations/003_tenant_isolation.sql`.
  Tests: `tests/integration/test_tenant_isolation.py` (18 cases, including
  leakage attempts by primary key, forged-`tenant_id` writes, and unqualified
  `UPDATE`/`DELETE`).
