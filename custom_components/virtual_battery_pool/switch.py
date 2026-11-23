from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from .const import DOMAIN
from .entity import PoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PoolEnable(runtime), PoolSeparateControls(runtime)])


class PoolEnable(PoolEntity, SwitchEntity):
    _attr_icon = "mdi:battery-lock"

    def __init__(self, runtime):
        super().__init__(runtime, "enabled", "Pool control")

    @property
    def is_on(self):
        return self.runtime.enabled

    async def async_turn_on(self, **kwargs):
        try:
            await self.runtime.set_enabled(True)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs):
        await self.runtime.set_enabled(False)


class PoolSeparateControls(PoolEntity, SwitchEntity):
    _attr_icon = "mdi:tune-variant"

    def __init__(self, runtime):
        super().__init__(runtime, "separate_controls", "Separate charge/discharge controls")

    @property
    def available(self):
        return not self.runtime._closed and self.runtime.mode == "external"

    @property
    def is_on(self):
        return self.runtime.separate_controls

    async def async_turn_on(self, **kwargs):
        try:
            await self.runtime.set_separate_controls(True)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs):
        try:
            await self.runtime.set_separate_controls(False)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
