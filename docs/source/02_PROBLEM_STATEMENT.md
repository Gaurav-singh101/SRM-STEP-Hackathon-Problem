# 1. Problem Space

**Fuel, Idling & Utilisation Cost**

# 2. User Job

Fleet finance and operations teams need to understand how connected-vehicle behaviour affects fuel expenditure and vehicle utilisation. They need actionable information at both vehicle and fleet level rather than raw telemetry alone.

# 3. Core Problem

Connected fleets continuously generate vehicle signals such as location, speed and diagnostic/operational information. At fleet scale, this produces a high-volume stream that must be ingested, processed and converted into decisions within seconds.

For the selected problem space, the central challenge is to identify inefficient idling and utilisation patterns and estimate their financial impact while retaining the ability to process large-scale telemetry reliably.

# 4. Why the Problem Matters

Unnecessary engine-on/vehicle-stationary periods can consume fuel without productive movement. Poor utilisation can also indicate that vehicle assets are not being used efficiently.

The platform therefore needs to connect:

**Telemetry → Behaviour Detection → Fuel/Cost Impact → Fleet Decision**

# 5. Target Users

## Primary

- Fleet Finance Manager
- Fleet Operations Manager

## Secondary

- Fleet Supervisor
- Operations Analyst

Secondary personas are project-level user definitions and must be validated during implementation/demo preparation.

# 6. Proposed Problem Questions

The platform should support questions such as:

1. Which vehicles are generating idle-related cost?
2. How much estimated fuel/cost is associated with inefficient idling?
3. Which vehicles show low or abnormal utilisation?
4. What are the fleet-level fuel, idle and utilisation trends?
5. Which events or vehicles require operational attention?

# 7. Current-State Limitation

A raw telemetry stream does not directly provide financial or operational insight. A useful system needs:

- Reliable ingestion.
- Event validation.
- Duplicate/out-of-order handling.
- Near-real-time processing.
- Historical analysis.
- Aggregation.
- Secure access.
- A usable dashboard.

# 8. Proposed Outcome

The project will provide a web-based fleet intelligence layer that transforms telemetry into:

- Idle events and duration.
- Estimated fuel impact.
- Estimated cost impact.
- Utilisation indicators.
- Vehicle-level insights.
- Fleet-level summaries.
- Alerts/attention indicators where implemented.

# 9. Scope Boundaries

## Included

- Synthetic connected-vehicle telemetry.
- At least 100,000 simulated vehicles.
- Real-time and batch analytics.
- Relational and NoSQL persistence.
- Secure API layer.
- Web interface.
- Containerized deployment.
- AWS/Kubernetes deployment.

## Not Yet Defined

The following must be finalized before business logic is implemented:

- Idle threshold.
- Fuel-consumption formula.
- Utilisation formula.
- Authentication provider.

# 10. Facts vs Project Decisions

| **Item**                                                               | **Classification**                   |
|------------------------------------------------------------------------|--------------------------------------|
| Fuel, Idling & Utilisation Cost is an official hackathon problem space | Hackathon requirement                |
| 100K+ vehicle simulator                                                | Hackathon minimum bar                |
| Real-time + batch analytics                                            | Hackathon minimum bar                |
| Relational + NoSQL storage                                             | Hackathon minimum bar                |
| Kafka                                                                  | Approved project technology decision |
| PostgreSQL                                                             | Approved project technology decision |
| MongoDB                                                                | Approved project technology decision |
| Redis 7.2 and prior / Legacy Open Source                               | Approved project technology decision |
| FastAPI                                                                | Approved project technology decision |
| React                                                                  | Approved project technology decision |
| AWS                                                                    | Approved project technology decision |
| Kubernetes                                                             | Approved project technology decision |
| Single-member team                                                     | Project decision                     |
| Exact idle threshold                                                   | TBD                                  |
| Exact fuel formula                                                     | TBD                                  |
| Exact utilisation formula                                              | TBD                                  |
| Authentication provider                                                | TBD                                  |

# 11. Success Definition

The problem is considered implemented when the system can demonstrate an end-to-end flow from simulated vehicle telemetry through ingestion, processing, persistence, API exposure and dashboard presentation, together with evidence against the relevant performance, reliability, security and testing requirements.

Actual quantitative improvements or measured performance must not be claimed until they are benchmarked.

# 12. Validation Approach

The project should validate its claims using:

- Synthetic telemetry simulation.
- Controlled load tests.
- Before/after query plans where applicable.
- API latency measurements.
- Stream-processing latency measurements.
- Failure/recovery tests.
- Security testing.
- Dashboard/demo evidence.

Competitor or alternative-solution claims should be supported by evidence rather than unsupported assertions.
