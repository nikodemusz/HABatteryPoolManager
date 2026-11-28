"""Periodic planning and optional execution of signed or separate watt setpoints."""
import asyncio
from dataclasses import fields, replace
from datetime import timedelta
import logging
import math

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import BatteryConfig, ControllerConfig, CONF_BATTERIES, DOMAIN, TARGET_TIMEOUT, TICK_SECONDS
from .controller import BatteryReading, Direction, PoolController

_LOGGER = logging.getLogger(__name__)


def control_store(hass, entry_id):
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.controls")


class PoolRuntime:
    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        keys = {f.name for f in fields(BatteryConfig)}
        self.batteries = tuple(BatteryConfig(**{k: v for k, v in b.items() if k in keys})
                               for b in entry.options.get(CONF_BATTERIES, []))
        self.controller = PoolController(self._controller_config()) if self.batteries else None
        self.live = entry.options.get("live_control", False)
        self.mode = entry.options.get("control_mode", "external")
        self.enabled = False
        self.target = 0.0
        self.target_at = None
        self.manual_direction = None
        self.manual_charge_w = 300.0
        self.manual_discharge_w = 300.0
        self.separate_controls = False
        self.control_store = control_store(hass, entry.entry_id)
        self.status = "disabled"
        self.fault = None
        self.input_error = None
        self.soc = None
        self.measured = None
        self.planned = 0.0
        self.outputs = {}
        self.lock = asyncio.Lock()
        self._cancel = None
        self._closed = False
        self._stop_pending = False
        self.signal = f"{DOMAIN}_{entry.entry_id}"

    async def async_initialize(self):
        preferences = await self.control_store.async_load()
        preferences = preferences if isinstance(preferences, dict) else {}
        self.separate_controls = preferences.get("separate_controls", True) is True
        for direction in ("charge", "discharge"):
            try:
                value = self._validated_target(preferences.get(f"manual_{direction}_w", 300), 0)
            except ValueError:
                value = 300.0
            setattr(self, f"manual_{direction}_w", value)

    def _controller_config(self):
        return ControllerConfig(self.batteries, ramp_w_per_minute=float(self.entry.options.get("ramp_w_per_minute", 0)))

    @property
    def operation(self):
        if not self.enabled:
            return "standby"
        return self.manual_direction or ("automatic" if self.mode == "autonomous" else "external")

    async def _save_preferences(self, **changes):
        values = {"separate_controls": self.separate_controls,
                  "manual_charge_w": self.manual_charge_w, "manual_discharge_w": self.manual_discharge_w}
        await self.control_store.async_save({**values, **changes})

    async def set_manual_power(self, direction, value):
        if direction not in ("charge", "discharge"):
            raise ValueError("Direction must be charge or discharge")
        value = self._validated_target(value, 0)
        async with self.lock:
            if self._closed:
                return
            if not self.separate_controls:
                raise ValueError("Enable separate charge/discharge controls first")
            await self._save_preferences(**{f"manual_{direction}_w": value})
            setattr(self, f"manual_{direction}_w", value)
            apply = self.enabled and self.manual_direction == direction
        if apply:
            await self.update()
        else:
            self.notify()

    async def set_operation(self, operation):
        if operation not in ("standby", "charge", "discharge", "automatic", "external"):
            raise ValueError("Unknown battery operation")
        if operation == "standby":
            await self.set_enabled(False)
            return
        async with self.lock:
            if self._closed:
                return
            if operation == "automatic" and (not self.entry.options.get("grid_confirmed") or not self.entry.options.get("grid_power_entity")):
                raise ValueError("Configure and confirm the grid sensor before selecting automatic operation")
            if self.live:
                try:
                    self._validate_adapters()
                except (ValueError, KeyError, TypeError) as err:
                    self.fault = str(err) or type(err).__name__
                    if self.enabled:
                        await self._stop_outputs()
                    self.enabled = False
                    self.manual_direction = None
                    self.target = 0
                    self.target_at = None
                    self.planned = 0
                    self.outputs = {b.name: 0 for b in self.batteries}
                    self.status = "fault"
                    self.notify()
                    raise ValueError(self.fault) from err
            if operation in ("charge", "discharge") and not self.separate_controls:
                await self._save_preferences(separate_controls=True)
                self.separate_controls = True
            self.fault = None
            self.enabled = True
            self.manual_direction = operation if operation in ("charge", "discharge") else None
            self.mode = "autonomous" if operation == "automatic" else "external"
            self.target = 0
            self.target_at = None
            if self.controller is None and self.batteries:
                self.controller = PoolController(self._controller_config())
            elif self.controller:
                self.controller.config = self._controller_config()
        await self.update()

    async def set_simulation(self, simulation):
        if not isinstance(simulation, bool):
            raise ValueError("Simulation must be on or off")
        async with self.lock:
            if self._closed or simulation == (not self.live):
                return
            if not simulation:
                self._validate_adapters()
            if self.enabled or self._stop_pending:
                await self._stop_outputs()
            self.enabled = False
            self.manual_direction = None
            self.target = 0
            self.target_at = None
            self.planned = 0
            self.outputs = {b.name: 0 for b in self.batteries}
            if self._stop_pending:
                self.status = "fault"
                self.notify()
                raise ValueError("Battery stop failed; simulation setting remains unchanged while stop commands are retried")
            # Use HA's config-entry API; the existing listener safely reloads this entry.
            try:
                self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, "live_control": not simulation})
            except Exception as err:
                raise ValueError("Could not save simulation setting; pool remains stopped") from err
            self.live = not simulation
        self.notify()

    @property
    def charge_target(self):
        return max(0, -self.target)

    @property
    def discharge_target(self):
        return max(0, self.target)

    @property
    def control_interface(self):
        return "separate" if self.separate_controls else "signed"

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

    def _power_unit_valid(self, state, battery):
        unit = state.attributes.get("unit_of_measurement")
        return unit == "W" or (unit is None and battery.power_unit_w_confirmed)

    def _effective_batteries(self, now):
        """Read device limits without ever changing their entities."""
        result = []
        for battery in self.batteries:
            limits = {}
            for field, entity in (("max_charge_w", battery.max_charge_entity), ("max_discharge_w", battery.max_discharge_entity)):
                if not entity:
                    continue
                state = self.hass.states.get(entity)
                if state is None or not self._power_unit_valid(state, battery):
                    raise ValueError(f"Device limit unavailable or not in W: {entity}")
                value = float(state.state)
                # Number entities hold persistent configuration, not telemetry.
                # Only a sensor supplying a dynamic limit needs a fresh report.
                stale = entity.startswith("sensor.") and not 0 <= (now - state.last_reported).total_seconds() <= 60
                if not math.isfinite(value) or value < 0 or stale:
                    raise ValueError(f"Invalid or stale device limit: {entity}")
                limits[field] = min(getattr(battery, field), value)
            result.append(replace(battery, **limits))
        return tuple(result)

    def _command_state(self, battery, entity, lower, upper):
        domain = entity.split(".")[0]
        if domain not in ("number", "input_number") or not self.hass.services.has_service(domain, "set_value"):
            raise ValueError(f"Command service unavailable: {entity}")
        state = self.hass.states.get(entity)
        if state is None or state.state in ("unknown", "unavailable") or not self._power_unit_valid(state, battery):
            raise ValueError(f"Command entity unavailable or not in W: {entity}")
        if not math.isfinite(float(state.state)):
            raise ValueError(f"Invalid command state: {entity}")
        minimum, maximum = (float(state.attributes[k]) for k in ("min", "max"))
        if not all(math.isfinite(v) for v in (minimum, maximum)) or not minimum <= lower <= upper <= maximum:
            raise ValueError(f"Command range does not cover configured power limits and zero: {entity}")
        step = float(state.attributes.get("step", 1))
        if not math.isfinite(step) or step <= 0 or abs(minimum / step - round(minimum / step)) > 1e-6:
            raise ValueError(f"Command step cannot represent zero: {entity}")
        return state

    def _validate_adapters(self):
        if not self.batteries:
            raise ValueError("No batteries configured")
        command_ids = [entity for b in self.batteries for entity in b.control_entities]
        limit_ids = {entity for b in self.batteries for entity in b.limit_entities}
        if len(set(command_ids)) != len(command_ids) or set(command_ids) & limit_ids:
            raise ValueError("Duplicate command entities or overlap with device limits")
        for other in self.hass.data.get(DOMAIN, {}).values():
            if other is not self and other.enabled and other.live:
                other_commands = {entity for b in other.batteries for entity in b.control_entities}
                other_limits = {entity for b in other.batteries for entity in b.limit_entities}
                if set(command_ids) & (other_commands | other_limits) or limit_ids & other_commands:
                    raise ValueError("Command entity already controlled by another pool or used as a device limit")
        for b in self.batteries:
            if not b.adapter_confirmed:
                raise ValueError(f"Adapter contract not confirmed: {b.name}")
            if b.mode_entity:
                self._mode_state(b)
            if b.command_mode == "separate":
                self._command_state(b, b.charge_command_entity, 0, b.max_charge_w)
                self._command_state(b, b.discharge_command_entity, 0, b.max_discharge_w)
            elif b.command_mode == "signed":
                domain = b.command_entity.split(".")[0]
                if b.command_service != f"{domain}.set_value":
                    raise ValueError(f"Unsupported command service: {b.name}")
                charge = -b.max_charge_w if b.positive_discharge else b.max_charge_w
                discharge = b.max_discharge_w if b.positive_discharge else -b.max_discharge_w
                self._command_state(b, b.command_entity, min(0, charge, discharge), max(0, charge, discharge))
            else:
                raise ValueError(f"Unknown command mode: {b.name}")

    def _mode_state(self, battery):
        entity = battery.mode_entity
        domain = entity.split(".")[0]
        if domain not in ("select", "input_select") or not self.hass.services.has_service(domain, "select_option"):
            raise ValueError(f"Mode service unavailable: {entity}")
        state = self.hass.states.get(entity)
        options = state.attributes.get("options", []) if state else []
        modes = (battery.charge_mode, battery.discharge_mode, battery.idle_mode)
        if (state is None or state.state in ("unknown", "unavailable") or not isinstance(options, list)
                or state.state not in options or len(set(modes)) != 3 or not all(option and option in options for option in modes)):
            raise ValueError(f"Mode entity or configured options unavailable: {entity}")
        return state

    async def _write_mode(self, battery, option):
        domain = battery.mode_entity.split(".")[0]
        if domain not in ("select", "input_select"):
            raise ValueError(f"Unsupported mode entity: {battery.mode_entity}")
        try:
            async with asyncio.timeout(5):
                await self.hass.services.async_call(domain, "select_option", {"entity_id": battery.mode_entity, "option": option}, blocking=True)
                while True:
                    state = self.hass.states.get(battery.mode_entity)
                    if state is not None and state.state == option:
                        return
                    await asyncio.sleep(0.1)
        except TimeoutError as err:
            raise ValueError(f"Mode change not confirmed within 5 seconds: {battery.mode_entity}") from err

    def _command_targets(self):
        result = []
        for b in self.batteries:
            power = self.outputs.get(b.name, 0)
            if b.command_mode == "separate":
                outputs = ((b.charge_command_entity, max(0, -power)), (b.discharge_command_entity, max(0, power)))
            else:
                outputs = ((b.command_entity, power * (1 if b.positive_discharge else -1)),)
            for entity, value in outputs:
                state = self.hass.states.get(entity)
                step = float(state.attributes.get("step", 1))
                # Preflight has checked zero alignment and a finite positive step.
                value = math.trunc(value / step) * step
                result.append((replace(b, command_entity=entity), value))
        return result

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
            for entity in b.command_entities:
                try:
                    await self._write(replace(b, command_entity=entity), 0)
                except Exception:
                    failures.append(entity)
                    _LOGGER.warning("Could not send zero power to battery command %s", entity)
            if b.mode_entity:
                try:
                    await self._write_mode(b, b.idle_mode)
                except Exception:
                    failures.append(b.mode_entity)
                    _LOGGER.warning("Could not set battery idle mode: %s", b.mode_entity)
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
                self.manual_direction = None
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
                        self.fault = str(err) or type(err).__name__
                        if self.enabled:
                            await self._stop_outputs()
                        self.enabled = False
                        self.planned = 0
                        self.outputs = {b.name: 0 for b in self.batteries}
                        self.status = "fault"
                        self.notify()
                        raise ValueError(self.fault) from err
                self.enabled = True
                # Every activation starts at zero; previous manual commands are never restored.
                self.target = 0
                self.target_at = None
                if self.controller is None and self.batteries:
                    self.controller = PoolController(self._controller_config())
        await self.update()

    @staticmethod
    def _validated_target(value, minimum):
        try:
            value = float(value)
        except (ValueError, TypeError) as err:
            raise ValueError("Power request must be a number") from err
        if not math.isfinite(value) or not minimum <= value <= 100000:
            raise ValueError(f"Power request must be finite and within {minimum} to 100000 W")
        return value

    def _check_request_source(self, separate):
        if self.mode != "external":
            raise ValueError("Manual power requests are only used in external control mode")
        if self.separate_controls != separate:
            raise ValueError("This power control is inactive; check the separate charge/discharge controls switch")

    async def set_target(self, value):
        value = self._validated_target(value, -100000)
        async with self.lock:
            if self._closed:
                return
            self._check_request_source(False)
            self.manual_direction = None
            self.target = value
            self.target_at = dt_util.utcnow()
        await self.update()

    async def set_direction_target(self, direction, value):
        if direction not in ("charge", "discharge"):
            raise ValueError("Direction must be charge or discharge")
        value = self._validated_target(value, 0)
        async with self.lock:
            if self._closed:
                return
            self._check_request_source(True)
            active = self.target < 0 if direction == "charge" else self.target > 0
            if value == 0 and self.target != 0 and not active:
                # A zero on the inactive channel must neither stop the active
                # request nor keep an expired request alive through a heartbeat.
                return
            self.manual_direction = None
            self.target = -value if direction == "charge" else value
            self.target_at = dt_util.utcnow()
        await self.update()

    async def set_separate_controls(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Separate controls must be on or off")
        async with self.lock:
            if self._closed:
                return
            if enabled == self.separate_controls:
                return
            # Persist only the UI preference, never enablement or a power request.
            # A storage failure leaves the existing interface and request unchanged.
            await self._save_preferences(separate_controls=enabled)
            self.separate_controls = enabled
            self.manual_direction = None
            self.target = 0
            self.target_at = None
            self.planned = 0
            self.outputs = {b.name: 0 for b in self.batteries}
            if self.mode == "external":
                if self.controller:
                    self.controller.plan({}, 0, dt_util.utcnow())
                if self.enabled:
                    await self._stop_outputs()
        await self.update()

    async def update(self):
        async with self.lock:
            if self._closed:
                return
            now = dt_util.utcnow()
            readings = {}
            effective_batteries = self.batteries
            error = None
            limit_error = None
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
            try:
                effective_batteries = self._effective_batteries(now)
            except (ValueError, TypeError) as err:
                limit_error = str(err)
            measurement_error = error
            error = error or limit_error
            request = 0
            reason = None
            if self.enabled and not self.fault and not error and self.controller:
                if self.manual_direction:
                    request = -self.manual_charge_w if self.manual_direction == "charge" else self.manual_discharge_w
                elif self.mode == "autonomous":
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
                self.controller.config = replace(self.controller.config, batteries=effective_batteries)
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
                self.status = "invalid_limits" if limit_error and not measurement_error else "invalid_readings"
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
                        targets = self._command_targets()
                        mode_changes = []
                        for b in self.batteries:
                            if not b.mode_entity:
                                continue
                            has_power = any(target != 0 and endpoint.name == b.name for endpoint, target in targets)
                            power = self.outputs.get(b.name, 0)
                            desired = (b.charge_mode if power < 0 else b.discharge_mode) if has_power else b.idle_mode
                            if self._mode_state(b).state != desired:
                                mode_changes.append((b, desired))
                        # Clear both requests before changing this battery's direction mode.
                        for b, _ in mode_changes:
                            for entity in b.command_entities:
                                await self._write(replace(b, command_entity=entity), 0)
                        # Stop/reduce old commands across the entire pool before any increase.
                        for endpoint, target in targets:
                            current = float(self.hass.states.get(endpoint.command_entity).state)
                            if current != 0 and (current * target <= 0 or abs(target) < abs(current)):
                                await self._write(endpoint, 0)
                        # Explicitly zero every inactive endpoint first, even if its reported
                        # state is already zero. This interlocks separate charge/discharge controls.
                        for endpoint, target in targets:
                            if target == 0:
                                await self._write(endpoint, 0)
                        # Service completion AND HA state readback must confirm every mode
                        # before any battery receives a nonzero power command.
                        for b, desired in mode_changes:
                            await self._write_mode(b, desired)
                        for endpoint, target in targets:
                            if target != 0:
                                await self._write(endpoint, target)
                    except Exception as err:
                        self.fault = str(err) or type(err).__name__
                        await self._stop_outputs()
            if self.fault:
                self.status = "fault"
                self.planned = 0
                self.outputs = {b.name: 0 for b in self.batteries}
            self.input_error = error
            self.notify()

    async def shutdown(self):
        if self._cancel:
            self._cancel()
        async with self.lock:
            self._closed = True
            if self.enabled or self._stop_pending:
                await self._stop_outputs()
            self.enabled = False
