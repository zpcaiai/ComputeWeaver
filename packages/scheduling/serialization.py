from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from packages.workloads.models import Job, ResourceRequest, Sla, WorkloadClass

from .contracts import Allocation, ScheduleInput, SchedulePlan, TimeSlot


def parse_job(data: dict[str, Any], tenant_id: str) -> Job:
    request = data["request"]
    sla = data["sla"]
    return Job(
        id=str(data["id"]),
        tenant_id=tenant_id,
        project_id=str(data["project_id"]),
        workload_class=WorkloadClass(data["workload_class"]),
        request=ResourceRequest(
            gpu_count=int(request["gpu_count"]),
            gpu_model=request.get("gpu_model"),
            cpu_cores=int(request.get("cpu_cores", 0)),
            memory_gb=Decimal(str(request.get("memory_gb", 0))),
            estimated_hours=Decimal(str(request["estimated_hours"])),
            power_kw_per_gpu=Decimal(str(request["power_kw_per_gpu"])),
        ),
        sla=Sla(
            deadline=datetime.fromisoformat(str(sla["deadline"])),
            priority=int(sla.get("priority", 50)),
            max_latency_ms=sla.get("max_latency_ms"),
            availability_target=(
                Decimal(str(sla["availability_target"])) if sla.get("availability_target") is not None else None
            ),
        ),
        submitted_at=datetime.fromisoformat(str(data["submitted_at"])),
        allowed_sites=frozenset(str(item) for item in data.get("allowed_sites", [])),
        data_regions=frozenset(str(item) for item in data.get("data_regions", [])),
        dependencies=frozenset(str(item) for item in data.get("dependencies", [])),
        checkpointable=bool(data.get("checkpointable", False)),
        labels={str(key): str(value) for key, value in dict(data.get("labels", {})).items()},
    )


def parse_schedule_input(data: dict[str, Any], tenant_id: str) -> ScheduleInput:
    jobs = tuple(parse_job(item, tenant_id) for item in data["jobs"])
    slots = tuple(
        TimeSlot(
            index=int(item["index"]),
            starts_at=datetime.fromisoformat(str(item["starts_at"])),
            duration_hours=Decimal(str(item["duration_hours"])),
            gpu_capacity=int(item["gpu_capacity"]),
            power_capacity_kw=Decimal(str(item["power_capacity_kw"])),
            price_per_kwh=Decimal(str(item["price_per_kwh"])),
            carbon_kg_per_kwh=Decimal(str(item.get("carbon_kg_per_kwh", 0))),
        )
        for item in data["slots"]
    )
    return ScheduleInput(
        jobs,
        slots,
        int(data["topology_version"]),
        str(data["forecast_version"]),
        str(data.get("baseline_name", "none")),
    )


def parse_schedule_plan(data: dict[str, Any]) -> SchedulePlan:
    return SchedulePlan(
        strategy=str(data["strategy"]),
        allocations=tuple(
            Allocation(
                job_id=str(item["job_id"]),
                slot_indices=tuple(int(index) for index in item["slot_indices"]),
                gpu_count=int(item["gpu_count"]),
                reason_code=str(item["reason_code"]),
            )
            for item in data.get("allocations", [])
        ),
        unscheduled=tuple(str(item) for item in data.get("unscheduled", [])),
        energy_intent_kwh=Decimal(str(data["energy_intent_kwh"])),
        estimated_cost=Decimal(str(data["estimated_cost"])),
        assumptions=tuple(str(item) for item in data.get("assumptions", [])),
        violations=tuple(str(item) for item in data.get("violations", [])),
        input_hash=str(data["input_hash"]),
    )
