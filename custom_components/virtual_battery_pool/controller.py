from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

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
    """Stateless safety and dispatch planner; the HA layer owns service calls."""

    def __init__(self, config: ControllerConfig) -> None:
        if not config.batteries:
            raise ValueError("At least one battery is required")
        self.config = config
        self._last_direction = Direction.IDLE
        self._direction_since: datetime | None = None
        self._last_commands: dict[str, float] = {}
        self._last_command_at: datetime | None = None

    def plan(
        self,
        readings: dict[str, BatteryReading],
        grid_power_w: float,
        now: datetime,
        target_power_w: float | None = None,
    ) -> Plan:
        """Grid import is positive; positive battery target means discharge."""
        pool_soc = self._pool_soc(readings)
        if not self._readings_safe(readings, now):
            return self._zero("stale_or_invalid_data", pool_soc)

        request = target_power_w if target_power_w is not None else -grid_power_w
        direction = (Direction.CHARGE if request < 0 else Direction.DISCHARGE) if abs(request) >= self.config.deadband_w else Direction.IDLE
        if direction is Direction.IDLE:
            return self._zero("inside_deadband", pool_soc)

        if self._last_direction not in (Direction.IDLE, direction):
            self._direction_since = now
            self._last_direction = direction
            return self._zero("direction_reversal_guard", pool_soc)
        if self._last_direction is Direction.IDLE:
            self._last_direction = direction
            self._direction_since = now
        if self._direction_since and now - self._direction_since < timedelta(seconds=self.config.reversal_guard_seconds):
            return self._zero("direction_reversal_guard", pool_soc)

        remaining = abs(request)
        candidates = self._eligible(direction, readings)
        candidates.sort(key=lambda item: self._priority(item, direction, readings))
        commands: list[BatteryCommand] = []
        for battery in candidates:
            reading = readings[battery.name]
            limit = battery.max_charge_w if direction is Direction.CHARGE else battery.max_discharge_w
            if direction is Direction.CHARGE:
                limit = min(limit, max(0, (battery.max_soc - reading.soc) * battery.capacity_kwh * 1000 / 0.25))
            else:
                limit = min(limit, max(0, (reading.soc - battery.min_soc) * battery.capacity_kwh * 1000 / 0.25))
            amount = min(remaining, limit)
            amount = min(amount, self.config.ramp_w_per_minute * self.config.command_interval_seconds / 60)
            amount = self._hysteresis(battery.name, amount, now)
            commands.append(BatteryCommand(battery.name, round(amount, 1), direction if amount else Direction.IDLE))
            remaining -= amount
        if not commands:
            return self._zero("soc_limit", pool_soc)
        self._last_command_at = now
        self._last_commands = {command.battery: command.power_w for command in commands}
        return Plan(tuple(commands), "dispatch", pool_soc)

    def _readings_safe(self, readings: dict[str, BatteryReading], now: datetime) -> bool:
        for battery in self.config.batteries:
            reading = readings.get(battery.name)
            if reading is None or not 0 <= reading.soc <= 100:
                return False
            age = now - reading.updated_at
            if age < timedelta(0) or age > timedelta(seconds=self.config.stale_after_seconds):
                return False
        return True

    def _eligible(self, direction: Direction, readings: dict[str, BatteryReading]) -> list[BatteryConfig]:
        return [b for b in self.config.batteries if (readings[b.name].soc < b.max_soc if direction is Direction.CHARGE else readings[b.name].soc > b.min_soc)]

    def _priority(self, battery: BatteryConfig, direction: Direction, readings: dict[str, BatteryReading]) -> float:
        soc = readings[battery.name].soc
        return soc if direction is Direction.CHARGE else -soc

    def _pool_soc(self, readings: dict[str, BatteryReading]) -> float | None:
        values = [(b.capacity_kwh, readings[b.name].soc) for b in self.config.batteries if b.name in readings]
        total = sum(capacity for capacity, _ in values)
        return round(sum(capacity * soc for capacity, soc in values) / total, 1) if total else None

    def _hysteresis(self, battery: str, amount: float, now: datetime) -> float:
        previous = self._last_commands.get(battery, 0)
        if self._last_command_at and now - self._last_command_at < timedelta(seconds=self.config.command_interval_seconds):
            if abs(amount - previous) < self.config.hysteresis_w:
                return previous
        return amount

    def _zero(self, reason: str, pool_soc: float | None) -> Plan:
        return Plan(tuple(BatteryCommand(b.name, 0, Direction.IDLE) for b in self.config.batteries), reason, pool_soc)
