from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.exceptions import HomeAssistantError
from .const import DOMAIN
from .entity import PoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PoolTarget(hass.data[DOMAIN][entry.entry_id])])


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
    def native_value(self):
        return self.runtime.target

    @property
    def extra_state_attributes(self):
        return {"refresh_within_seconds": 60, "used_in_mode": "external"}

    async def async_set_native_value(self, value):
        try:
            await self.runtime.set_target(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
