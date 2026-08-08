from __future__ import annotations

from decimal import Decimal


def facility_power_kw(it_power_kw: Decimal, pue: Decimal, fixed_load_kw: Decimal) -> Decimal:
    if it_power_kw < 0 or fixed_load_kw < 0 or pue < 1:
        raise ValueError("PUE must be >= 1 and loads non-negative")
    return it_power_kw * pue + fixed_load_kw
