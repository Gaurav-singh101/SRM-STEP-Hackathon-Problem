# Terraform — AWS

**This has never been applied.** There is no AWS account for this project. The hackathon asks
for Terraform for at least one cloud and an architecture deployable on at least one cloud —
not a running cloud environment — and this is written, formatted and `validate`-clean against
that requirement. Stating it plainly here rather than letting a reader assume a deployment
happened.

What that means precisely:

| Command | Needs credentials | Run here |
|---|---|---|
| `terraform fmt -check` | no | ✅ |
| `terraform init` | no (downloads the provider) | ✅ |
| `terraform validate` | no | ✅ |
| `terraform plan` | **yes** — refreshes against real APIs | ❌ |
| `terraform apply` | **yes** | ❌ never |

So this is reviewed, syntactically and semantically valid IaC with a correct provider
version, and it is unproven against a live account. Those are different claims and only the
first is being made.

## What it provisions

| Area | Resource | Note |
|---|---|---|
| Network | VPC, 3 private + 3 public subnets, IGW, **one** NAT gateway | Three AZs because replication factor 3 needs three failure domains. One NAT is a deliberate cost trade — see below. |
| Compute | EKS 1.31, two node groups | System on on-demand, processor on **spot** and tainted. |
| Streaming | MSK 3.9, 3 brokers, TLS, RF3, `min.insync.replicas=2` | 96 default partitions, from ADR-0004's measurement. |
| Relational | RDS PostgreSQL 16, gp3, encrypted, `rds.force_ssl` | Never publicly accessible. |
| Cache | ElastiCache Redis **7.2** | Pinned for licence reasons, not inertia. |
| Cold tier | S3, encrypted, versioned, lifecycle to Glacier IR | Only if D6 chooses Parquet. |
| Secrets | Secrets Manager + a generated password | No credential is ever a Terraform output. |
| Guardrail | AWS Budget at 50/80/100% | Not optional on a personal account. |

## Decisions visible in the code

**One NAT gateway, not three.** Roughly $32/month each, which on a $50 budget is the largest
single line item. The consequence is honest: losing that AZ costs outbound internet for
private subnets until it recovers. It is the wrong trade for production, and
`var.environment == "prod"` is where that should change.

**Spot for the processor only.** Safe there specifically: a terminated consumer causes a
cooperative-sticky rebalance, offsets commit only after a batch is durable, and delivery is
at-least-once with idempotent writes — so a reclaim costs a reprocessed batch, not data. The
node group is tainted so the API cannot schedule onto it and turn a reclaim into a 502.

**No `0.0.0.0/0` on the Kubernetes API.** `allowed_admin_cidrs` has a validation block that
refuses it, and an empty list means private-only access rather than open access.

**Least-privilege secret read.** The node role can read exactly the ARNs this platform owns,
named explicitly. `secretsmanager:*` would let one compromised pod read every secret in the
account.

**No remote backend configured.** State belongs in S3 with DynamoDB locking for shared use,
but committing a bucket name would either name someone else's bucket or invent one that does
not exist. The omission is deliberate and this line is the record of it.

## Known gaps

- **mTLS on the device-to-broker path is not provisioned.** CLAUDE.md §11 asks for it and
  MSK supports it, but it needs an ACM Private CA at roughly $400/month. This is the same gap
  as **S3 in `docs/STRIDE.md`**, the highest-ranked open threat there: nothing currently binds
  a producer identity to a VIN. Commented in `data-stores.tf` at the exact place it would go.
- **MongoDB is absent.** DocumentDB is not wire-compatible enough for the time-series and TTL
  features in use, and Atlas is a separate vendor with its own account. On a real deployment
  this is a provider decision, not an omission to paper over; locally it is a container.
- **D8 is still open.** MSK is provisioned; Strimzi on EKS is the portable alternative. The
  application cannot tell the difference — it takes a bootstrap server from configuration —
  which is the point of the boundary.

## Verifying it without an account

```bash
make tf-validate
```

Runs `fmt -check`, `init -backend=false` and `validate` in the `hashicorp/terraform` container,
then scans the configuration with Trivy. No credentials are used or needed.
