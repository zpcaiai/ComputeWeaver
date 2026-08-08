# Service-level objectives

- API availability: 99.9% monthly, excluding approved maintenance.
- Read API latency: P95 below 300 ms at the certified MVP load.
- MPC cycle: P95 below the configured control interval; timeout activates a tested fallback.
- Event loss: zero for accepted append-only events.
- Recovery point objective: 5 minutes. Recovery time objective: 30 minutes.

These are target contracts, not measured claims, until B20 load and DR evidence passes.

## Error-budget policy

- Page when API availability burns the 30-day budget at 14.4x for 5 minutes or 6x for 30 minutes.
- Block releases while any critical alert is firing or telemetry is absent for 10 minutes.
- Revoke the active certificate when integrity, tenant isolation, guarded execution or evidence
  provenance is compromised.
- Require a new production load report after capacity, topology, solver, database or API changes.
- Require a new restore rehearsal after storage, retention, encryption or migration changes.
