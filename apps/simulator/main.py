from __future__ import annotations

import argparse
import json

from packages.simulation.engine import SimulationConfig, Simulator


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    simulator = Simulator(SimulationConfig(seed=args.seed, duration_hours=args.hours))
    print(json.dumps(simulator.run(), indent=2, default=str))


if __name__ == "__main__":
    main()
