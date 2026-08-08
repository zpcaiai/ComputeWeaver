# AI Compute–Energy–Infrastructure Control Plane Skills Package

This package converts the complete system design into 20 executable Codex implementation batches.

## Design intent

The package is deliberately evidence-driven. A batch is not complete because a `SKILL.md` exists, files were generated, or code looks plausible. It is complete only after implementation, tests and machine-generated evidence pass the declared gates.

## Batch map

| Batch | Scope |
|---|---|
| B01 | Platform Foundation, Monorepo and Reproducible Build |
| B02 | Unified Domain Model, Units, Schemas and Event Contracts |
| B03 | Site, Data-Center and Infrastructure Topology Registry |
| B04 | GPU Compute Resource Plane and Scheduler Adapters |
| B05 | AI Workload, SLA, Quota, Reservation and Admission Management |
| B06 | Tariff, Demand-Charge, Energy Contract and Carbon Rule Engine |
| B07 | Energy Assets, PUE and Power-Balance Engine |
| B08 | Connector Framework, Event/Time-Series Storage and Data Quality |
| B09 | Deterministic Compute–Energy Shadow Simulator |
| B10 | Scenario DSL, Fault Injection, Replay and Evaluation Framework |
| B11 | Forecasting Center, Model Registry and Uncertainty Contracts |
| B12 | FIFO, Priority and Price-Aware Baselines with Benchmark Harness |
| B13 | MILP Compute–Energy Co-Optimization Engine |
| B14 | Rolling MPC, Online Replanning and Safe Fallback |
| B15 | Policy, Constraint, Risk and Plan Lifecycle Governance |
| B16 | Human Approval, Action Guard, Execution Adapters and Compensation |
| B17 | Decision Explainability, Counterfactual What-if and Evidence Reporting |
| B18 | Multi-Tenant IAM, Budgets, Chargeback, Notifications and Administration |
| B19 | Multi-Site Optimization, Data Sovereignty, Island Mode and Resilience |
| B20 | End-to-End Integration, Security, Performance and Production Release Gate |

## Recommended execution order

Run B01 through B20 in dependency order. Parallel work is safe only after shared contracts are stable:

- B03 and B06 may proceed after B02.
- B05 and B07 may proceed after their listed dependencies.
- B11 can begin after ingestion and scenarios exist.
- B17 and B18 may overlap after plan governance is stable.
- B20 always runs last.

## Package contents

- `batches/Bxx-*/SKILL.md`: implementation contract for each batch.
- `IMPLEMENTATION_ORDER.md`: milestones and dependency graph.
- `INTERFACE_CATALOG.md`: cross-batch interfaces and ownership.
- `EVIDENCE_STANDARD.md`: proof and completion requirements.
- `IMPLEMENTATION_CHECKLIST.md`: progress checklist.
- `CODEX_EXECUTION_PROTOCOL.md`: how Codex must report work.
- `validate_package.py`: structural package validator.
- `VALIDATION_REPORT.md`: current package validation result.

## Validate

```bash
python validate_package.py
```

This validates package structure only. It does not claim the software itself is implemented.

## Product layers

1. AI Compute Scheduler
2. Compute–Energy Co-Optimizer
3. Infrastructure Shadow Control Plane
4. Multi-site Infrastructure Sovereignty Platform
