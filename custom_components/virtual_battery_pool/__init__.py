from __future__ import annotations

from datetime import datetime, timezone
import logging

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"entry": entry, "last_update": datetime.now(timezone.utc)}
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
