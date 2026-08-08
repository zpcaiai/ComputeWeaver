# Implementation Order and Dependency Graph

## Milestone 1 — Executable foundation

```text
B01 → B02
       ├→ B03 → B04 → B05
       └→ B06 → B07
```

Exit: reproducible repo, canonical schemas, topology, compute, jobs, tariffs and energy physics.

## Milestone 2 — Data and deterministic verification

```text
B04 + B07 → B08 → B09 → B10
```

Exit: normalized ingestion, deterministic simulator, 10 scenario packs, replay and evaluator.

## Milestone 3 — Intelligence and optimization

```text
B08 + B10 → B11
B05 + B06 + B07 + B10 + B11 → B12 → B13 → B14
```

Exit: forecasts, three baselines, MILP and rolling MPC.

## Milestone 4 — Governance and execution

```text
B05 + B13 + B14 → B15 → B16
B12 + B13 + B15 + B16 → B17
B05 + B08 + B15 + B17 → B18
```

Exit: governed plans, approval, guarded execution, explanations and enterprise controls.

## Milestone 5 — Sovereignty and release

```text
B14 + B16 + B18 → B19
B01–B19 → B20
```

Exit: multi-site resilience and certified production release.

## Mandatory architecture rules

- The simulator and live connectors implement the same contracts.
- The optimizer never writes directly to external systems.
- Action Guard is the only route for state-changing external actions.
- Hard constraints cannot be weakened by prompt, policy weight or LLM output.
- All savings are calculated against a named, reproducible baseline.
- B20 is the only authority allowed to label a release production-certified.
