from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.evaluator.metrics import Evaluation, evaluate_events
from packages.simulation.engine import SimulationConfig, Simulator

from .schema import FaultSpec, validate_document


@dataclass(frozen=True, slots=True)
class CompiledScenario:
    name: str
    version: str
    seed: int
    duration_hours: int
    faults: tuple[FaultSpec, ...]


def compile_scenario(document: dict[str, Any]) -> CompiledScenario:
    validated = validate_document(document)
    for fault in validated.faults:
        if fault.step >= validated.duration_hours * 4:
            raise ValueError("fault occurs outside scenario")
    return CompiledScenario(
        validated.name,
        validated.version,
        validated.seed,
        validated.duration_hours,
        validated.faults,
    )


def run_scenario(scenario: CompiledScenario) -> tuple[list[dict[str, Any]], Evaluation]:
    simulator = Simulator(SimulationConfig(seed=scenario.seed, duration_hours=scenario.duration_hours))
    schedule = {fault.step: fault.kind for fault in scenario.faults}
    events = simulator.run(schedule)
    return events, evaluate_events(events)
