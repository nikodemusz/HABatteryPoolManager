"""Selectable signed or separate manual power requests for the virtual pool."""
from datetime import timedelta

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, TARGET_TIMEOUT
from .entity import PoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PoolTarget(runtime), PoolDirectionTarget(runtime, "charge"), PoolDirectionTarget(runtime, "discharge")])


class PoolTarget(PoolEntity, NumberEntity):
    _attr_native_min_value = -100000
    _attr_native_max_value = 100000
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-sync"

    def __init__(self, runtime):
        super().__init__(runtime, "target", "Power request (+ discharge / − charge)")

    @property
    def available(self):
        return not self.runtime._closed and self.runtime.mode == "external" and not self.runtime.separate_controls

    @property
    def native_value(self):
        return self.runtime.target

    @property
    def extra_state_attributes(self):
        expires = self.runtime.target_at + timedelta(seconds=TARGET_TIMEOUT) if self.runtime.target_at else None
        return {"refresh_within_seconds": TARGET_TIMEOUT, "used_in_mode": "external",
                "control_interface": self.runtime.control_interface,
                "request_expires_at": expires.isoformat() if expires else None}

    async def async_set_native_value(self, value):
        try:
            await self.runtime.set_target(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err


class PoolDirectionTarget(PoolTarget):
    _attr_native_min_value = 0

    def __init__(self, runtime, direction):
        PoolEntity.__init__(self, runtime, f"{direction}_target", f"Requested {direction} power")
        self.direction = direction
        self._attr_icon = "mdi:battery-arrow-up" if direction == "charge" else "mdi:battery-arrow-down"

    @property
    def available(self):
        return not self.runtime._closed and self.runtime.separate_controls

    @property
    def native_value(self):
        return self.runtime.manual_charge_w if self.direction == "charge" else self.runtime.manual_discharge_w

    async def async_set_native_value(self, value):
        try:
            await self.runtime.set_manual_power(self.direction, value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def extra_state_attributes(self):
        return {"active": self.runtime.operation == self.direction, "expires": False,
                "start_with": "operation", "simulation": not self.runtime.live}
