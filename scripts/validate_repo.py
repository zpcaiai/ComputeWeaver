from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = (
    "apps/api/main.py",
    "apps/web/src/App.vue",
    "apps/web/src/api.ts",
    "apps/web/src/oidc.ts",
    "apps/web/main.py",
    "apps/worker/main.py",
    "apps/simulator/main.py",
    "packages/domain/units.py",
    "packages/contracts/events.py",
    "packages/topology/registry.py",
    "packages/compute/snapshot.py",
    "packages/compute/health.py",
    "packages/admission/service.py",
    "packages/tariffs/calculator.py",
    "packages/energy/power_balance.py",
    "packages/ingestion/normalize.py",
    "packages/simulation/engine.py",
    "packages/simulation/session.py",
    "packages/scenarios/compiler.py",
    "packages/forecasting/models.py",
    "packages/forecasting/service.py",
    "packages/scheduling/strategies.py",
    "packages/scheduling/serialization.py",
    "packages/optimization/engine.py",
    "packages/mpc/controller.py",
    "packages/mpc/repository.py",
    "packages/policy/engine.py",
    "packages/execution/action_guard.py",
    "packages/persistence/operations.py",
    "packages/explain/service.py",
    "packages/iam/service.py",
    "packages/multisite/optimizer.py",
    "packages/certification/service.py",
    "packages/certification/source.py",
    "packages/certification/preflight.py",
    "packages/certification/images.py",
    "packages/certification/suite.py",
    "packages/certification/attestations.py",
    "packages/certification/lifecycle.py",
    "packages/certification/signing.py",
    "scripts/run_production_preflight.py",
    "scripts/run_external_gate_suite.py",
    "scripts/manage_image_bundle.py",
    "scripts/export_image_bundle.py",
    "deploy/kubernetes/production-gates.yaml",
    "deploy/kubernetes/postgres-backup.yaml",
    "ops/alerts.yaml",
    "ops/BACKUP_RESTORE.md",
    "ops/INCIDENT_RESPONSE.md",
    "ops/RELEASE_ROLLBACK.md",
)


def main() -> None:
    missing = [path for path in REQUIRED if not (ROOT / path).is_file()]
    syntax_errors: list[str] = []
    for path in ROOT.glob("packages/**/*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            syntax_errors.append(f"{path.relative_to(ROOT)}:{error.lineno}:{error.msg}")
    result = {"required_files": len(REQUIRED), "missing": missing, "syntax_errors": syntax_errors}
    print(json.dumps(result, indent=2))
    if missing or syntax_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
