from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from packages.tariffs.calculator import CostBreakdown, MeterInterval, TariffCalculator
from packages.tariffs.models import TariffPlan


class RegionPack(ABC):
    @abstractmethod
    def validate(self, tariff: TariffPlan) -> None: ...

    @abstractmethod
    def normalize(self, tariff: TariffPlan) -> TariffPlan: ...

    @abstractmethod
    def calculate(self, tariff: TariffPlan, intervals: tuple[MeterInterval, ...]) -> CostBreakdown: ...

    @abstractmethod
    def explain(self, result: CostBreakdown) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class StandardRegionPack(RegionPack):
    region: str

    def validate(self, tariff: TariffPlan) -> None:
        if tariff.tax_rate > 1:
            raise ValueError("tax rate cannot exceed 100%")

    def normalize(self, tariff: TariffPlan) -> TariffPlan:
        self.validate(tariff)
        return tariff

    def calculate(self, tariff: TariffPlan, intervals: tuple[MeterInterval, ...]) -> CostBreakdown:
        self.validate(tariff)
        return TariffCalculator().calculate(tariff, intervals)

    def explain(self, result: CostBreakdown) -> tuple[str, ...]:
        return (
            f"energy={result.energy.amount} {result.energy.currency}",
            f"demand={result.demand.amount} {result.demand.currency}",
            f"total={result.total.amount} {result.total.currency}",
        )
