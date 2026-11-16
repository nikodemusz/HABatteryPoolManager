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
    """Distribute an external signed pool power request across batteries."""

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
        requested_power_w: float,
        now: datetime,
    ) -> Plan:
        """Positive requests discharge; negative requests charge."""
        pool_soc = self._pool_soc(readings)
        if not self._configuration_safe() or not self._readings_safe(readings, now):
            return self._zero("stale_or_invalid_data", pool_soc)

        direction = (
            Direction.CHARGE if requested_power_w < 0 else Direction.DISCHARGE
        ) if abs(requested_power_w) >= self.config.deadband_w else Direction.IDLE
        if direction is Direction.IDLE:
            return self._zero("inside_deadband", pool_soc)

        if self._last_direction not in (Direction.IDLE, direction):
            self._direction_since = now
            self._last_direction = direction
            return self._zero("direction_reversal_guard", pool_soc)
        if self._last_direction is Direction.IDLE:
            self._last_direction = direction
            self._direction_since = now
        if self._direction_since and now - self._direction_since < timedelta(
            seconds=self.config.reversal_guard_seconds
        ):
            return self._zero("direction_reversal_guard", pool_soc)

        remaining = min(
            abs(requested_power_w),
            self.config.ramp_w_per_minute * self.config.command_interval_seconds / 60,
        )
        allocations = {battery.name: 0.0 for battery in self.config.batteries}
        candidates = self._eligible(direction, readings)
        candidates.sort(key=lambda battery: self._priority(battery, direction, readings))
        for battery in candidates:
            if remaining <= 0:
                break
            power_limit = battery.max_charge_w if direction is Direction.CHARGE else battery.max_discharge_w
            amount = min(remaining, power_limit)
            allocations[battery.name] = self._hysteresis(battery.name, amount, now)
            remaining -= allocations[battery.name]

        commands = tuple(
            BatteryCommand(
                battery.name,
                round(allocations[battery.name], 1),
                direction if allocations[battery.name] > 0 else Direction.IDLE,
            )
            for battery in self.config.batteries
        )
        if not any(command.power_w for command in commands):
            return self._zero("soc_limit", pool_soc)
        self._last_command_at = now
        self._last_commands = {command.battery: command.power_w for command in commands}
        return Plan(commands, "dispatch", pool_soc)

    def _configuration_safe(self) -> bool:
        return all(
            battery.capacity_kwh > 0
            and 0 <= battery.min_soc < battery.max_soc <= 100
            and battery.max_charge_w > 0
            and battery.max_discharge_w > 0
            for battery in self.config.batteries
        )

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
        return [
            battery
            for battery in self.config.batteries
            if (
                readings[battery.name].soc < battery.max_soc
                if direction is Direction.CHARGE
                else readings[battery.name].soc > battery.min_soc
            )
        ]

    def _priority(self, battery: BatteryConfig, direction: Direction, readings: dict[str, BatteryReading]) -> float:
        soc = readings[battery.name].soc
        return soc if direction is Direction.CHARGE else -soc

    def _pool_soc(self, readings: dict[str, BatteryReading]) -> float | None:
        values = [(battery.capacity_kwh, readings[battery.name].soc) for battery in self.config.batteries if battery.name in readings]
        total_capacity = sum(capacity for capacity, _ in values)
        return round(sum(capacity * soc for capacity, soc in values) / total_capacity, 1) if total_capacity else None

    def _hysteresis(self, battery: str, amount: float, now: datetime) -> float:
        previous = self._last_commands.get(battery, 0)
        if self._last_command_at and now - self._last_command_at < timedelta(seconds=self.config.command_interval_seconds):
            if abs(amount - previous) < self.config.hysteresis_w:
                return previous
        return amount

    def _zero(self, reason: str, pool_soc: float | None) -> Plan:
        return Plan(tuple(BatteryCommand(battery.name, 0, Direction.IDLE) for battery in self.config.batteries), reason, pool_soc)
