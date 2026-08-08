# Codex Execution Protocol

## One batch per implementation thread

Codex should receive one `SKILL.md` plus access to the repository and completed dependency evidence.

## Required first response

Before editing, Codex must return:

1. Dependency status and evidence checked.
2. Existing repository paths relevant to the batch.
3. Concrete files to create or modify.
4. Interface mismatches or blockers.
5. Test and evidence commands it intends to run.

## Required final response

Codex must return:

1. Changed files.
2. Implemented behaviors mapped to SKILL sections.
3. Exact commands run.
4. Tests passed/failed/skipped.
5. Evidence paths.
6. Unimplemented or mock-only behavior.
7. Every DoD item marked PASS or FAIL.
8. Final status: `COMPLETE`, `EVIDENCE_PENDING` or `BLOCKED`.

## Forbidden completion shortcuts

Codex must not:

- mark checkboxes complete without commands and evidence;
- create only interfaces, docs or mocks and call the feature complete;
- replace failed tests with skipped tests;
- weaken a hard constraint to make optimization feasible;
- report simulated external writes as real integration;
- claim production readiness before B20 certification;
- use file count or package validation as proof of runtime functionality.

## Recommended git discipline

- One branch per batch.
- Small commits by coherent behavior.
- No unrelated refactors.
- Commit tests and evidence-generation scripts with implementation.
- Evidence outputs may be stored outside git if CI links and immutable hashes are recorded.
