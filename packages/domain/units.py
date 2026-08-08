from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Self


def _decimal(value: Decimal | int | float | str) -> Decimal:
    if isinstance(value, float):
        value = str(value)
    return Decimal(value)


@dataclass(frozen=True, slots=True)
class Duration:
    hours: Decimal

    def __init__(self, hours: Decimal | int | float | str) -> None:
        value = _decimal(hours)
        if value < 0:
            raise ValueError("duration cannot be negative")
        object.__setattr__(self, "hours", value)

    @classmethod
    def minutes(cls, value: Decimal | int | float | str) -> Self:
        return cls(_decimal(value) / 60)


@dataclass(frozen=True, slots=True)
class Power:
    kw: Decimal

    def __init__(self, kw: Decimal | int | float | str) -> None:
        object.__setattr__(self, "kw", _decimal(kw))

    def __add__(self, other: Power) -> Power:
        if not isinstance(other, Power):
            return NotImplemented
        return Power(self.kw + other.kw)

    def __sub__(self, other: Power) -> Power:
        if not isinstance(other, Power):
            return NotImplemented
        return Power(self.kw - other.kw)

    def energy_for(self, duration: Duration) -> Energy:
        if not isinstance(duration, Duration):
            raise TypeError("power can only be integrated over Duration")
        with localcontext() as context:
            context.prec = max(50, len(self.kw.as_tuple().digits) + len(duration.hours.as_tuple().digits))
            return Energy(self.kw * duration.hours)


@dataclass(frozen=True, slots=True)
class Energy:
    kwh: Decimal

    def __init__(self, kwh: Decimal | int | float | str) -> None:
        object.__setattr__(self, "kwh", _decimal(kwh))

    def __add__(self, other: Energy) -> Energy:
        if not isinstance(other, Energy):
            return NotImplemented
        return Energy(self.kwh + other.kwh)

    def __sub__(self, other: Energy) -> Energy:
        if not isinstance(other, Energy):
            return NotImplemented
        return Energy(self.kwh - other.kwh)

    def average_power(self, duration: Duration) -> Power:
        if duration.hours == 0:
            raise ZeroDivisionError("zero-duration interval")
        with localcontext() as context:
            context.prec = max(50, len(self.kwh.as_tuple().digits) + len(duration.hours.as_tuple().digits))
            return Power(self.kwh / duration.hours)


@dataclass(frozen=True, slots=True)
class Money:
    amount: Decimal
    currency: str

    def __init__(self, amount: Decimal | int | float | str, currency: str = "USD") -> None:
        normalized = currency.upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("currency must be an ISO-like three-letter code")
        object.__setattr__(self, "amount", _decimal(amount))
        object.__setattr__(self, "currency", normalized)

    def _compatible(self, other: Money) -> None:
        if not isinstance(other, Money) or self.currency != other.currency:
            raise ValueError("currency mismatch")

    def __add__(self, other: Money) -> Money:
        self._compatible(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._compatible(other)
        return Money(self.amount - other.amount, self.currency)

    def rounded(self, places: int = 2) -> Money:
        quantum = Decimal(1).scaleb(-places)
        return Money(self.amount.quantize(quantum, rounding=ROUND_HALF_EVEN), self.currency)


@dataclass(frozen=True, slots=True)
class Carbon:
    kg_co2e: Decimal

    def __init__(self, kg_co2e: Decimal | int | float | str) -> None:
        value = _decimal(kg_co2e)
        if value < 0:
            raise ValueError("carbon cannot be negative")
        object.__setattr__(self, "kg_co2e", value)


@dataclass(frozen=True, slots=True)
class Percentage:
    ratio: Decimal

    def __init__(self, ratio: Decimal | int | float | str) -> None:
        value = _decimal(ratio)
        if not 0 <= value <= 1:
            raise ValueError("percentage ratio must be in [0, 1]")
        object.__setattr__(self, "ratio", value)
