"""Shared entity lifecycle."""
from homeassistant.helpers.entity import Entity, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from .const import DOMAIN


class PoolEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, runtime, key, name):
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.entry.entry_id)}, name=runtime.entry.title, manufacturer="Virtual Battery Pool", model="AC battery pool")

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self.async_write_ha_state))
