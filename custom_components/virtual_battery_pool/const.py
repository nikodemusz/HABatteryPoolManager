"""Configuration and conservative defaults for AC battery adapters."""
from dataclasses import dataclass

DOMAIN = "virtual_battery_pool"
CONF_BATTERIES = "batteries"
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_MODE = "mode"
MODE_AUTONOMOUS = "autonomous"
MODE_EXTERNAL = "external"
MODE_SIMULATION = "simulation"
PLATFORMS = ["sensor", "number", "switch"]
TICK_SECONDS = 5
TARGET_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    name: str
    capacity_kwh: float
    soc_entity: str
    power_entity: str
    command_entity: str
    command_service: str
    min_soc: float = 20.0
    max_soc: float = 80.0
    max_charge_w: float = 300.0
    max_discharge_w: float = 300.0
    positive_discharge: bool = True
    adapter_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    batteries: tuple[BatteryConfig, ...]
    deadband_w: float = 50.0
    ramp_w_per_minute: float = 300.0
    stale_after_seconds: int = 60
    command_interval_seconds: int = TICK_SECONDS
    reversal_guard_seconds: int = 30
    hysteresis_w: float = 0.0
