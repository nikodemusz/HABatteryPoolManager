"""Load the integration and its actual entity platforms in an isolated HA instance."""
from pathlib import Path
import shutil
from types import MappingProxyType

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant import loader, bootstrap
from homeassistant.setup import async_setup_component

from custom_components.virtual_battery_pool.const import DOMAIN


@pytest.mark.asyncio
async def test_platform_setup_restart_defaults_and_unload(tmp_path):
    source = Path(__file__).parents[1]/'custom_components'/DOMAIN
    target = tmp_path/'custom_components'/DOMAIN
    shutil.copytree(source, target, ignore=shutil.ignore_patterns('__pycache__'))
    (target.parent/'__init__.py').write_text('')
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    loader.async_setup(hass)
    assert await bootstrap.async_load_base_functionality(hass)
    entry = ConfigEntry(version=1, minor_version=1, domain=DOMAIN, title='Test pool', data={'name':'Test pool'}, options={}, source='user', unique_id=None, discovery_keys=MappingProxyType({}), subentries_data=[])
    hass.config_entries._entries[entry.entry_id] = entry
    try:
        assert await async_setup_component(hass, DOMAIN, {})
        from homeassistant.config_entries import ConfigEntryState
        assert entry.state == ConfigEntryState.LOADED
        await hass.async_block_till_done()
        runtime = hass.data[DOMAIN][entry.entry_id]
        assert not runtime.enabled and not runtime.live
        from homeassistant.helpers import entity_registry as er
        registry = er.async_get(hass)
        entities = er.async_entries_for_config_entry(registry, entry.entry_id)
        assert len(entities) == 6
        assert {e.domain for e in entities} == {'number','switch','sensor'}
        assert all(hass.states.get(e.entity_id) is not None for e in entities)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.entry_id not in hass.data[DOMAIN]
    finally:
        await hass.async_stop()
