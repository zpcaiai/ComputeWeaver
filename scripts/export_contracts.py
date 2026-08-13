from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.main import app
from packages.contracts.schema_cli import MODELS
from scripts.export_web_workflows import OUTPUT_PATH as WEB_WORKFLOW_PATH
from scripts.export_web_workflows import build_catalog

ROOT = Path(__file__).resolve().parents[1]


def rendered_contracts() -> dict[Path, str]:
    contracts = {
        ROOT / "schemas" / "json" / f"{name}.json": model.model_json_schema() for name, model in MODELS.items()
    }
    openapi = app.openapi()
    contracts[ROOT / "schemas" / "openapi" / "openapi.json"] = openapi
    contracts[WEB_WORKFLOW_PATH] = build_catalog(openapi)
    return {path: json.dumps(document, indent=2, sort_keys=True) + "\n" for path, document in contracts.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export or verify deterministic API contracts")
    parser.add_argument("--check", action="store_true", help="fail when committed contracts are stale")
    args = parser.parse_args()
    stale: list[str] = []
    for path, rendered in rendered_contracts().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                stale.append(path.relative_to(ROOT).as_posix())
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    if stale:
        raise SystemExit(f"stale generated contracts: {', '.join(stale)}")
    print("Contract snapshots: PASS" if args.check else "Contract snapshots exported")


if __name__ == "__main__":
    main()
