# Project Title

**Fleet Fuel, Idling & Utilisation Cost Intelligence Platform**

# 1. Project Context

This project is developed for the **Connected Vehicle Intelligence Hackathon**, under the official problem space **Fuel, Idling & Utilisation Cost**.

The objective is to build a connected-vehicle platform that converts high-volume vehicle telemetry into actionable information for fleet finance and operations teams. The platform focuses on identifying where fuel and vehicle utilisation costs are being lost, quantifying the impact, and presenting the results through a usable web interface.

# 2. Primary Problem Space

**Fuel, Idling & Utilisation Cost**

The platform is intended to answer questions such as:

- Where is the fleet losing money through unnecessary idling?
- How much fuel/cost is associated with inefficient behaviour?
- Which vehicles have poor utilisation?
- What fleet-level trends require operational attention?

# 3. Primary User

**Fleet Finance / Fleet Operations Manager**

The primary user needs fleet-level and vehicle-level visibility into fuel usage, idling, utilisation and estimated financial impact.

# 4. Project Objective

Build a scalable connected-vehicle analytics platform that:

1. Ingests realistic telemetry at hackathon-scale volume.
2. Handles duplicate and out-of-order events.
3. Processes telemetry for near-real-time insights.
4. Stores business data and telemetry using appropriate persistence models.
5. Calculates fuel, idling and utilisation indicators.
6. Exposes secure APIs.
7. Presents results through a React web interface.
8. Can be containerized and deployed on AWS using Kubernetes.
9. Produces measurable evidence for performance, reliability and testing requirements.

# 5. Approved Technology Decisions

| **Area**                    | **Decision**                             |
|-----------------------------|------------------------------------------|
| Messaging / Event Streaming | Apache Kafka                             |
| Relational Database         | PostgreSQL                               |
| NoSQL Database              | MongoDB                                  |
| Cache                       | Redis 7.2 and prior / Legacy Open Source |
| Backend API                 | FastAPI                                  |
| Frontend                    | React                                    |
| Cloud                       | AWS                                      |
| Container Orchestration     | Kubernetes                               |
| Team                        | Single-member team                       |

# 6. Decisions Not Yet Finalized

The following business definitions remain intentionally open:

- Exact idle threshold.
- Exact fuel-consumption formula.
- Exact utilisation formula.
- Authentication provider.

Authentication details will be supplied during implementation when required. No credentials, passwords, API keys or secrets are hard-coded in this document.

# 7. Conceptual Architecture

Vehicle Simulator → Kafka → Stream Processing → PostgreSQL / MongoDB / Redis → FastAPI → React

The exact stream-processing technology and supporting infrastructure components remain subject to implementation decisions and must not be silently invented.

# 8. Hackathon Scale Targets

The project is designed against the hackathon's stated scale expectations, including:

- 100,000+ vehicles in the simulator.
- Approximately 100,000 events/second as the baseline high-throughput scenario.
- Ability to survive a 3× burst for 5 minutes with no data loss.
- Ingest-to-dashboard target below 2 seconds.
- Critical alert target below 5 seconds.
- API p95 below 200 ms and p99 below 500 ms.
- 99.9% availability target.

These are target requirements; measured results must be reported separately after implementation.

# 9. Scope

## In Scope

- Vehicle telemetry simulation.
- Kafka-based ingestion.
- Telemetry validation and processing.
- Idempotency and duplicate handling.
- Out-of-order event handling.
- Idle detection.
- Fuel/cost estimation.
- Vehicle utilisation analysis.
- Fleet-level aggregation.
- Historical analytics.
- Secure APIs.
- React dashboard.
- Performance/load testing.
- Containerized deployment.
- AWS/Kubernetes deployment.
- Monitoring, logging and recovery evidence.

## Out of Scope Until Explicitly Decided

- Final authentication provider.
- Final idle threshold.
- Final fuel-consumption formula.
- Final utilisation formula.
- Unapproved ML/AI features.
- Any production credential or secret.

# 10. Traceability Principle

Every major requirement should be traceable through:

**Requirement → Feature → Implementation → Test → Demonstration**

# 11. Classification of Information

- **Hackathon requirement:** directly derived from the problem statement.
- **Approved project decision:** explicitly selected for this project.
- **TBD:** intentionally not finalized.
- **Measured result:** must only be added after actual testing.

# 12. Single-Member Ownership

This is a single-member project. All architecture, implementation, testing, documentation and submission artifacts are maintained by one developer.
