"""Pure planner: positive pool requests discharge; negative requests charge."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import math

from .const import BatteryConfig, ControllerConfig


class Direction(StrEnum):
    IDLE = "idle"
    CHARGE = "charge"
    DISCHARGE = "discharge"


@dataclass(frozen=True, slots=True)
class BatteryReading:
    soc: float
    power_w: float
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BatteryCommand:
    battery: str
    power_w: float
    direction: Direction


@dataclass(frozen=True, slots=True)
class Plan:
    commands: tuple[BatteryCommand, ...]
    reason: str
    pool_soc: float | None


class PoolController:
    def __init__(self, config: ControllerConfig) -> None:
        if not config.batteries:
            raise ValueError("At least one battery is required")
        self.config = config
        self._last_direction = Direction.IDLE
        self._zero_since: datetime | None = None
        self._last_total = 0.0
        self._last_at: datetime | None = None

    def plan(self, readings: dict[str, BatteryReading], requested_power_w: float, now: datetime) -> Plan:
        valid = self._readings_safe(readings, now)
        soc = self._pool_soc(readings) if valid else None
        if not self._configuration_safe() or not valid or not math.isfinite(requested_power_w):
            return self._zero("stale_or_invalid_data", soc, now)
        direction = Direction.IDLE if abs(requested_power_w) < self.config.deadband_w else (
            Direction.CHARGE if requested_power_w < 0 else Direction.DISCHARGE)
        if direction == Direction.IDLE:
            return self._zero("inside_deadband", soc, now)
        if self._last_direction not in (Direction.IDLE, direction):
            if self._zero_since is None or (now - self._zero_since).total_seconds() < self.config.reversal_guard_seconds:
                return self._zero("direction_reversal_guard", soc, now)
        elapsed = self.config.command_interval_seconds if self._last_at is None else max(0, min(
            (now - self._last_at).total_seconds(), self.config.command_interval_seconds))
        remaining = min(abs(requested_power_w), self._last_total + self.config.ramp_w_per_minute * elapsed / 60)
        allocations = {b.name: 0.0 for b in self.config.batteries}
        candidates = [b for b in self.config.batteries if (
            readings[b.name].soc < b.max_soc if direction == Direction.CHARGE else readings[b.name].soc > b.min_soc)]
        candidates.sort(key=lambda b: readings[b.name].soc if direction == Direction.CHARGE else -readings[b.name].soc)
        for battery in candidates:
            limit = battery.max_charge_w if direction == Direction.CHARGE else battery.max_discharge_w
            allocations[battery.name] = min(remaining, limit)
            remaining -= allocations[battery.name]
        total = sum(allocations.values())
        if total == 0:
            return self._zero("soc_limit", soc, now)
        self._last_direction = direction
        self._zero_since = None
        self._last_at = now
        self._last_total = total
        return Plan(tuple(BatteryCommand(b.name, allocations[b.name], direction if allocations[b.name] else Direction.IDLE)
                          for b in self.config.batteries), "dispatch", soc)

    def _configuration_safe(self):
        c = self.config
        return (math.isfinite(c.ramp_w_per_minute) and c.ramp_w_per_minute > 0
                and c.command_interval_seconds > 0 and c.stale_after_seconds > 0
                and math.isfinite(c.deadband_w) and c.deadband_w >= 0 and c.reversal_guard_seconds >= 0
                and len({b.name for b in c.batteries}) == len(c.batteries)
                and all(all(math.isfinite(v) for v in (b.capacity_kwh, b.min_soc, b.max_soc, b.max_charge_w, b.max_discharge_w))
                        and b.capacity_kwh > 0 and 0 <= b.min_soc < b.max_soc <= 100
                        and b.max_charge_w >= 0 and b.max_discharge_w >= 0 for b in c.batteries))

    def _readings_safe(self, readings, now):
        for battery in self.config.batteries:
            r = readings.get(battery.name)
            if r is None or not math.isfinite(r.soc) or not math.isfinite(r.power_w) or not 0 <= r.soc <= 100:
                return False
            if not 0 <= (now - r.updated_at).total_seconds() <= self.config.stale_after_seconds:
                return False
        return True

    def _pool_soc(self, readings):
        if not self._configuration_safe():
            return None
        return round(sum(b.capacity_kwh * readings[b.name].soc for b in self.config.batteries)
                     / sum(b.capacity_kwh for b in self.config.batteries), 1)

    def _zero(self, reason, soc, now):
        if self._zero_since is None:
            self._zero_since = now
        self._last_total = 0.0
        self._last_at = now
        return Plan(tuple(BatteryCommand(b.name, 0, Direction.IDLE) for b in self.config.batteries), reason, soc)
