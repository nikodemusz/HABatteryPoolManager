"""Configuration and conservative defaults for AC battery adapters."""
from dataclasses import dataclass

DOMAIN = "virtual_battery_pool"
CONF_BATTERIES = "batteries"
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_MODE = "mode"
MODE_AUTONOMOUS = "autonomous"
MODE_EXTERNAL = "external"
MODE_SIMULATION = "simulation"
PLATFORMS = ["sensor", "number", "switch", "select"]
TICK_SECONDS = 5
TARGET_TIMEOUT = 60


@dataclass(frozen=True, slots=True)
class BatteryConfig:
    name: str
    capacity_kwh: float
    soc_entity: str
    power_entity: str
    command_entity: str = ""
    command_service: str = ""
    min_soc: float = 20.0
    max_soc: float = 80.0
    max_charge_w: float = 300.0
    max_discharge_w: float = 300.0
    positive_discharge: bool = True
    adapter_confirmed: bool = False
    command_mode: str = "signed"
    charge_command_entity: str = ""
    discharge_command_entity: str = ""
    max_charge_entity: str = ""
    max_discharge_entity: str = ""
    power_unit_w_confirmed: bool = False
    mode_entity: str = ""
    charge_mode: str = ""
    discharge_mode: str = ""
    idle_mode: str = ""

    @property
    def command_entities(self):
        if self.command_mode == "separate":
            return (self.charge_command_entity, self.discharge_command_entity)
        return (self.command_entity,)

    @property
    def control_entities(self):
        return self.command_entities + ((self.mode_entity,) if self.mode_entity else ())

    @property
    def limit_entities(self):
        return tuple(e for e in (self.max_charge_entity, self.max_discharge_entity) if e)



@dataclass(frozen=True, slots=True)
class ControllerConfig:
    batteries: tuple[BatteryConfig, ...]
    deadband_w: float = 50.0
    ramp_w_per_minute: float = 300.0
    stale_after_seconds: int = 60
    command_interval_seconds: int = TICK_SECONDS
    reversal_guard_seconds: int = 30
    hysteresis_w: float = 0.0
