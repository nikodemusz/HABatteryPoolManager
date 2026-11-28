"""Direct operating control for the complete battery pool."""
from homeassistant.components.select import SelectEntity
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .entity import PoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PoolOperation(hass.data[DOMAIN][entry.entry_id])])


class PoolOperation(PoolEntity, SelectEntity):
    _attr_options = ["standby", "charge", "discharge", "automatic", "external"]
    _attr_icon = "mdi:battery-sync"

    def __init__(self, runtime):
        super().__init__(runtime, "operation", "Battery operation")

    @property
    def current_option(self):
        return self.runtime.operation

    async def async_select_option(self, option):
        try:
            await self.runtime.set_operation(option)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
