"""Periodic, serialized planning and optional execution of signed watt setpoints."""
import asyncio
from dataclasses import fields
from datetime import timedelta
import logging
import math

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import BatteryConfig, ControllerConfig, CONF_BATTERIES, DOMAIN, TARGET_TIMEOUT, TICK_SECONDS
from .controller import BatteryReading, Direction, PoolController

_LOGGER = logging.getLogger(__name__)


class PoolRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        keys = {f.name for f in fields(BatteryConfig)}
        self.batteries = tuple(BatteryConfig(**{k: v for k, v in b.items() if k in keys})
                               for b in entry.options.get(CONF_BATTERIES, []))
        self.controller = PoolController(ControllerConfig(self.batteries)) if self.batteries else None
        self.live = entry.options.get("live_control", False)
        self.mode = entry.options.get("control_mode", "external")
        self.enabled = False
        self.target = 0.0
        self.target_at = None
        self.status = "disabled"
        self.fault = None
        self.soc = None
        self.measured = None
        self.planned = 0.0
        self.outputs = {}
        self.lock = asyncio.Lock()
        self._cancel = None
        self._closed = False
        self._stop_pending = False
        self.signal = f"{DOMAIN}_{entry.entry_id}"

    @callback
    def notify(self):
        async_dispatcher_send(self.hass, self.signal)

    def start(self):
        self._cancel = async_track_time_interval(self.hass, self._tick, timedelta(seconds=TICK_SECONDS))

    async def _tick(self, now):
        # Skip overlapping ticks instead of building a queue behind slow devices.
        if not self.lock.locked():
            await self.update()

    def _read(self, entity, unit, now):
        if not isinstance(entity, str):
            raise ValueError("No measurement entity configured")
        state = self.hass.states.get(entity)
        if state is None or state.attributes.get("unit_of_measurement") != unit:
            raise ValueError(f"Invalid or missing {unit} entity: {entity}")
        value = float(state.state)
        stamp = state.last_reported
        if not math.isfinite(value) or not 0 <= (now - stamp).total_seconds() <= 60:
            raise ValueError(f"Invalid or stale reading: {entity}")
        return value, stamp

    def _validate_adapters(self):
        if not self.batteries:
            raise ValueError("No batteries configured")
        command_ids = [b.command_entity for b in self.batteries]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("Duplicate command entities")
        for other in self.hass.data.get(DOMAIN, {}).values():
            if other is not self and other.enabled and other.live:
                if set(command_ids) & {b.command_entity for b in other.batteries}:
                    raise ValueError("Command entity already controlled by another pool")
        for b in self.batteries:
            domain = b.command_entity.split(".")[0]
            if not b.adapter_confirmed:
                raise ValueError(f"Adapter contract not confirmed: {b.name}")
            if domain not in ("number", "input_number") or b.command_service != f"{domain}.set_value":
                raise ValueError(f"Unsupported command service: {b.name}")
            if not self.hass.services.has_service(domain, "set_value"):
                raise ValueError(f"Command service unavailable: {b.name}")
            state = self.hass.states.get(b.command_entity)
            if state is None or state.state in ("unknown", "unavailable") or state.attributes.get("unit_of_measurement") != "W":
                raise ValueError(f"Command entity unavailable or not in W: {b.name}")
            if not math.isfinite(float(state.state)):
                raise ValueError(f"Invalid command state: {b.name}")
            minimum, maximum = (float(state.attributes[k]) for k in ("min", "max"))
            charge = -b.max_charge_w if b.positive_discharge else b.max_charge_w
            discharge = b.max_discharge_w if b.positive_discharge else -b.max_discharge_w
            if not all(math.isfinite(v) for v in (minimum, maximum)) or not minimum <= min(0, charge, discharge) <= max(0, charge, discharge) <= maximum:
                raise ValueError(f"Command range does not cover signed power limits and zero: {b.name}")

    async def _write(self, battery, value):
        domain = battery.command_entity.split(".")[0]
        # Service calls are bounded; a lost connection must not block the next stop attempt.
        async with asyncio.timeout(5):
            await self.hass.services.async_call(domain, "set_value", {"entity_id": battery.command_entity, "value": value}, blocking=True)

    async def _stop_outputs(self):
        """Attempt every adapter even if one has failed. No guarantee of physical release."""
        if not self.live:
            return
        failures = []
        for b in self.batteries:
            if not b.adapter_confirmed:
                continue
            try:
                await self._write(b, 0)
            except Exception:
                failures.append(b.name)
                _LOGGER.warning("Could not send zero power to battery %s", b.name)
        self._stop_pending = bool(failures)
        if failures:
            self.fault = "stop_failed: " + ", ".join(failures)

    async def set_enabled(self, enabled):
        async with self.lock:
            if self._closed:
                return
            if not enabled:
                if self.enabled:
                    await self._stop_outputs()
                self.enabled = False
                self.target = 0
                self.target_at = None
                self.planned = 0
                self.outputs = {}
                self.status = "fault" if self.fault else "disabled"
            else:
                self.fault = None
                if self.live:
                    try:
                        self._validate_adapters()
                    except (ValueError, KeyError, TypeError) as err:
                        self.fault = str(err)
                        self.status = "fault"
                        self.notify()
                        raise ValueError(self.fault) from err
                self.enabled = True
                # Every activation starts at zero; previous manual commands are never restored.
                self.target = 0
                self.target_at = None
                self.controller = PoolController(ControllerConfig(self.batteries)) if self.batteries else None
        await self.update()

    async def set_target(self, value):
        if not math.isfinite(value) or abs(value) > 100000:
            raise ValueError("Target must be finite and within ±100000 W")
        async with self.lock:
            self.target = value
            self.target_at = dt_util.utcnow()
        await self.update()

    async def update(self):
        async with self.lock:
            if self._closed:
                return
            now = dt_util.utcnow()
            readings = {}
            error = None
            try:
                for b in self.batteries:
                    soc, soc_at = self._read(b.soc_entity, "%", now)
                    power, power_at = self._read(b.power_entity, "W", now)
                    readings[b.name] = BatteryReading(soc, power, min(soc_at, power_at))
                self.measured = sum(r.power_w * (1 if b.positive_discharge else -1)
                                    for b in self.batteries if (r := readings.get(b.name))) if self.batteries else None
            except (ValueError, TypeError) as err:
                error = str(err)
                self.measured = None
            request = 0
            reason = None
            if self.enabled and not self.fault and not error and self.controller:
                if self.mode == "autonomous":
                    try:
                        if not self.entry.options.get("grid_confirmed", False):
                            raise ValueError("Grid import/export convention not confirmed")
                        grid, _ = self._read(self.entry.options.get("grid_power_entity"), "W", now)
                        request = grid + self.measured - self.entry.options.get("grid_target_w", 0)
                    except (ValueError, TypeError) as err:
                        error = str(err)
                elif self.target_at is not None and (now - self.target_at).total_seconds() <= TARGET_TIMEOUT:
                    request = self.target
                else:
                    reason = "target_expired"
            if self.controller:
                plan = self.controller.plan(readings, request if not error and not self.fault else 0, now)
                self.soc = plan.pool_soc
                self.outputs = {c.battery: c.power_w * (-1 if c.direction == Direction.CHARGE else 1) for c in plan.commands}
                self.planned = sum(self.outputs.values())
                self.status = reason or plan.reason
            else:
                self.soc = None
                self.outputs = {}
                self.planned = 0
                self.status = "no_batteries"
            if error:
                self.status = "invalid_readings"
            if not self.enabled:
                self.status = "disabled"
                if self._stop_pending:
                    await self._stop_outputs()
            if self.enabled and self.live:
                if self.fault:
                    await self._stop_outputs()
                else:
                    try:
                        self._validate_adapters()
                        # First reduce every existing command to zero before any increase or reversal.
                        # All writes are serialized and all adapters receive a heartbeat every tick.
                        targets = {}
                        for b in self.batteries:
                            value = self.outputs.get(b.name, 0) * (1 if b.positive_discharge else -1)
                            state = self.hass.states.get(b.command_entity)
                            step = float(state.attributes.get("step", 1))
                            if not math.isfinite(step) or step <= 0:
                                raise ValueError(f"Invalid command step: {b.name}")
                            # Zero must be supported exactly; integer multiples are rounded toward zero.
                            minimum = float(state.attributes['min'])
                            if abs(minimum / step - round(minimum / step)) > 1e-6:
                                raise ValueError(f"Command step cannot represent zero: {b.name}")
                            targets[b.name] = math.trunc(value / step) * step
                        for b in self.batteries:
                            current = float(self.hass.states.get(b.command_entity).state)
                            target = targets[b.name]
                            if current != 0 and (current * target <= 0 or abs(target) < abs(current)):
                                await self._write(b, 0)
                        for b in self.batteries:
                            await self._write(b, targets[b.name])
                    except Exception as err:
                        self.fault = str(err)
                        await self._stop_outputs()
            if self.fault:
                self.status = "fault"
                self.planned = 0
                self.outputs = {b.name: 0 for b in self.batteries}
            self.notify()

    async def shutdown(self):
        if self._cancel:
            self._cancel()
        async with self.lock:
            self._closed = True
            if self.enabled or self._stop_pending:
                await self._stop_outputs()
            self.enabled = False
