from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from packages.certification.cli import main as certification_main
from packages.evaluator.cli import main as evaluator_main
from packages.replay.cli import main as replay_main
from packages.scenarios.cli import main as scenario_main
from packages.simulation.engine import SimulationConfig, Simulator


def test_scenario_batch_cli_runs_every_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "scenarios.yaml"
    path.write_text(
        yaml.safe_dump(
            [
                {"name": "normal", "version": "1.0.0", "seed": 1, "duration_hours": 1},
                {
                    "name": "failure",
                    "version": "1.0.0",
                    "seed": 2,
                    "duration_hours": 1,
                    "faults": [{"step": 1, "kind": "gpu_failure", "target": "node"}],
                },
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["scenario", "batch", str(path)])
    scenario_main()
    assert len(json.loads(capsys.readouterr().out)["scenarios"]) == 2


def test_replay_and_evaluator_cli_emit_hash_and_numeric_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = Simulator(SimulationConfig(duration_hours=1, seed=3)).run()
    candidate = Simulator(SimulationConfig(duration_hours=1, seed=3)).run({1: "high_price"})
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["replay", "run", str(baseline_path)])
    replay_main()
    replay_output = json.loads(capsys.readouterr().out)
    assert replay_output["event_count"] == 4
    assert len(replay_output["event_hash"]) == 64

    monkeypatch.setattr(
        sys,
        "argv",
        ["evaluate", "compare", str(baseline_path), str(candidate_path)],
    )
    evaluator_main()
    evaluation = json.loads(capsys.readouterr().out)
    assert "energy_cost" in evaluation["deltas"]


def test_certification_cli_never_promotes_local_restore_log_to_production_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for batch in range(1, 21):
        directory = tmp_path / f"B{batch:02d}"
        directory.mkdir()
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "batch": f"B{batch:02d}",
                    "mandatory_gates": {"build": "PASS", "tests": "PASS"},
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        sys,
        "argv",
        ["certify", "preflight", "--evidence", str(tmp_path), "--commit", "commit-one"],
    )
    certification_main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "NOT_CERTIFIED"
    backup = next(gate for gate in result["gates"] if gate["name"] == "backup_restore")
    assert backup["passed"] is False
    assert "restore rehearsal" in backup["reason"]
