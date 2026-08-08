from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from packages.scheduling.contracts import Allocation, ScheduleInput, SchedulePlan, validate_plan

from .engine import OptimizationResult, _job_options, optimize


@dataclass(frozen=True, slots=True)
class ExactSolver:
    name: str = "exact-enumeration"

    def solve(self, request: ScheduleInput, timeout_seconds: float = 10) -> OptimizationResult:
        return optimize(request, timeout_seconds)


class HighsSolver:
    name = "highs"

    @staticmethod
    def available() -> bool:
        try:
            import highspy  # noqa: F401
        except ImportError:
            return False
        return True

    def solve(
        self,
        request: ScheduleInput,
        timeout_seconds: float = 10,
        model_path: Path | None = None,
    ) -> OptimizationResult:
        if not self.available():
            raise RuntimeError("HiGHS is not installed; no silent solver fallback is permitted")
        import highspy
        import numpy as np

        options = [_job_options(request, index) for index in range(len(request.jobs))]
        empty = [request.jobs[index].id for index, choices in enumerate(options) if not choices]
        if empty:
            return OptimizationResult(
                "infeasible",
                None,
                {},
                self.name,
                Decimal(0),
                0,
                (),
                tuple(f"JOB_NO_FEASIBLE_WINDOW:{job_id}" for job_id in empty),
            )

        variables = [
            (job_index, option_index, choice)
            for job_index, choices in enumerate(options)
            for option_index, choice in enumerate(choices)
        ]
        variable_index = {
            (job_index, option_index): index for index, (job_index, option_index, _) in enumerate(variables)
        }
        costs: list[float] = []
        for job_index, _, choice in variables:
            job = request.jobs[job_index]
            cost = sum(
                (
                    request.slots[slot_index].duration_hours
                    * job.request.gpu_count
                    * job.request.power_kw_per_gpu
                    * request.slots[slot_index].price_per_kwh
                    for slot_index in choice
                ),
                Decimal(0),
            )
            costs.append(float(cost))

        lower: list[float] = []
        upper: list[float] = []
        row_indices: list[int] = []
        row_values: list[float] = []
        row_starts = [0]

        # Every job selects exactly one feasible execution window.
        for job_index, choices in enumerate(options):
            for option_index in range(len(choices)):
                row_indices.append(variable_index[(job_index, option_index)])
                row_values.append(1.0)
            lower.append(1.0)
            upper.append(1.0)
            row_starts.append(len(row_indices))

        # GPU and power limits are independent hard rows for every interval.
        for slot in request.slots:
            for variable, (job_index, _, choice) in enumerate(variables):
                if slot.index in choice:
                    row_indices.append(variable)
                    row_values.append(float(request.jobs[job_index].request.gpu_count))
            lower.append(-highspy.kHighsInf)
            upper.append(float(slot.gpu_capacity))
            row_starts.append(len(row_indices))
        for slot in request.slots:
            for variable, (job_index, _, choice) in enumerate(variables):
                if slot.index in choice:
                    job = request.jobs[job_index]
                    row_indices.append(variable)
                    row_values.append(float(job.request.gpu_count * job.request.power_kw_per_gpu))
            lower.append(-highspy.kHighsInf)
            upper.append(float(slot.power_capacity_kw))
            row_starts.append(len(row_indices))

        highs = highspy.Highs()
        highs.setOptionValue("output_flag", False)
        highs.setOptionValue("time_limit", timeout_seconds)
        count = len(variables)
        highs.addVars(count, np.zeros(count), np.ones(count))
        columns = np.arange(count, dtype=np.int32)
        highs.changeColsCost(count, columns, np.asarray(costs, dtype=float))
        highs.changeColsIntegrality(count, columns, np.ones(count, dtype=np.uint8))
        highs.addRows(
            len(lower),
            np.asarray(lower, dtype=float),
            np.asarray(upper, dtype=float),
            len(row_indices),
            np.asarray(row_starts, dtype=np.int32),
            np.asarray(row_indices, dtype=np.int32),
            np.asarray(row_values, dtype=float),
        )
        if model_path is not None:
            model_path.parent.mkdir(parents=True, exist_ok=True)
            highs.writeModel(str(model_path))
        highs.run()
        status = highs.getModelStatus()
        info = highs.getInfo()
        runtime = highs.getRunTime()
        if status not in {highspy.HighsModelStatus.kOptimal, highspy.HighsModelStatus.kTimeLimit}:
            return OptimizationResult(
                "infeasible" if status == highspy.HighsModelStatus.kInfeasible else "failed",
                None,
                {},
                f"highs-{highs.version()}",
                Decimal(0),
                runtime,
                (),
                (f"HIGHS_STATUS:{highs.modelStatusToString(status)}",),
            )
        solution = highs.getSolution()
        selected: dict[int, tuple[int, ...]] = {}
        for variable, (job_index, _, choice) in enumerate(variables):
            if solution.col_value[variable] > 0.5:
                selected[job_index] = choice
        if len(selected) != len(request.jobs):
            return OptimizationResult(
                "timeout",
                None,
                {},
                f"highs-{highs.version()}",
                Decimal(str(info.mip_gap)),
                runtime,
                (),
                ("NO_INTEGER_INCUMBENT",),
            )
        allocations = tuple(
            Allocation(job.id, selected[index], job.request.gpu_count, "MIN_TOTAL_COST")
            for index, job in enumerate(request.jobs)
        )
        energy = sum(
            (
                request.slots[slot_index].duration_hours * job.request.gpu_count * job.request.power_kw_per_gpu
                for index, job in enumerate(request.jobs)
                for slot_index in selected[index]
            ),
            Decimal(0),
        )
        objective = Decimal(str(info.objective_function_value))
        plan = SchedulePlan(
            strategy="milp_highs",
            allocations=allocations,
            unscheduled=(),
            energy_intent_kwh=energy,
            estimated_cost=objective,
            assumptions=("time-indexed binary MILP",),
            violations=(),
            input_hash=request.content_hash(),
        )
        validate_plan(request, plan)
        return OptimizationResult(
            "optimal" if status == highspy.HighsModelStatus.kOptimal else "feasible_timeout",
            plan,
            {"energy_cost": objective, "delay": Decimal(0)},
            f"highs-{highs.version()}",
            Decimal(str(info.mip_gap)),
            runtime,
            plan.assumptions,
            (),
        )
