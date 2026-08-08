from __future__ import annotations

import json
from pathlib import Path

from packages.simulation.engine import SimulationConfig


def main() -> None:
    destination = Path("var/seed.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print("seed already exists; preserving it")
        return
    seed = {
        "tenant": "demo-tenant",
        "site": "demo-site",
        "simulator": {"seed": SimulationConfig().seed, "duration_hours": 24},
    }
    destination.write_text(json.dumps(seed, indent=2) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
