# Cross-Batch Interface Catalog

| Interface | Owner | Primary consumers |
|---|---|---|
| Shared IDs, units, event envelope, errors | B02 | All batches |
| Versioned topology snapshot | B03 | B04, B07, B13, B19 |
| Compute resource snapshot | B04 | B05, B12, B13, B14 |
| Job/SLA/admission contracts | B05 | B12–B18 |
| Tariff/carbon calculation | B06 | B12, B13, B17 |
| Energy state and power-balance validation | B07 | B09, B13, B14, B19 |
| Normalized time series and quality score | B08 | B11, B14, B20 |
| Simulator adapter contract | B09 | B10–B20 |
| Scenario/replay/evaluator API | B10 | B11–B20 |
| Forecast bundle | B11 | B13, B14, B17, B19 |
| Scheduler plan contract | B12 | B13–B17 |
| Optimization run and diagnostics | B13 | B14, B15, B17 |
| MPC cycle output | B14 | B15, B16, B19 |
| Policy decision and plan lifecycle | B15 | B16–B20 |
| Approval and guarded action | B16 | B17–B20 |
| Explanation, what-if and report | B17 | B18, B20 |
| Tenant/IAM/budget/chargeback | B18 | B19, B20 |
| Multi-site and emergency plan | B19 | B20 |
| Certification result | B20 | Release management |

## Contract ownership rule

The owning batch defines the schema, compatibility policy, contract tests and version. Consumers may not fork or duplicate the contract.

## Versioning rule

- Additive compatible change: minor schema version.
- Breaking change: major schema version plus migration and consumer updates.
- Persisted events and released plan/action contracts are never rewritten in place.
