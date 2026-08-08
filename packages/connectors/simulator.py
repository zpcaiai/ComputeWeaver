from __future__ import annotations

from packages.connectors.compute.base import StaticComputeAdapter


class SimulatorComputeAdapter(StaticComputeAdapter):
    """Deterministic adapter used for end-to-end local verification."""
