from __future__ import annotations

from dataclasses import dataclass

DOMAIN = "virtual_battery_pool"
CONF_BATTERIES = "batteries"
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_MODE = "mode"
MODE_AUTONOMOUS = "autonomous"
MODE_EXTERNAL = "external"
MODE_SIMULATION = "simulation"


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    name: str
    capacity_kwh: float
    soc_entity: str
    power_entity: str
    command_entity: str
    command_service: str
    min_soc: float = 0.0
    max_soc: float = 100.0
    max_charge_w: float = 800.0
    max_discharge_w: float = 800.0


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    batteries: tuple[BatteryConfig, ...]
    deadband_w: float = 80.0
    ramp_w_per_minute: float = 300.0
    stale_after_seconds: int = 30
    command_interval_seconds: int = 10
    reversal_guard_seconds: int = 30
    hysteresis_w: float = 30.0
