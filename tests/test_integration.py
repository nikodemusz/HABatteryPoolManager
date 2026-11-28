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
        assert len(entities) == 11
        assert {e.domain for e in entities} == {'number','switch','sensor','select'}
        assert all(hass.states.get(e.entity_id) is not None for e in entities)
        ids = {e.unique_id.removeprefix(entry.entry_id + "_"): e.entity_id for e in entities}
        assert hass.states.get(ids['target']).state == 'unavailable'
        assert hass.states.get(ids['charge_target']).state != 'unavailable'
        assert hass.states.get(ids['operation']).state == 'standby'
        assert hass.states.get(ids['simulation']).state == 'on'
        await hass.services.async_call('switch', 'turn_on', {'entity_id': ids['separate_controls']}, blocking=True)
        await hass.async_block_till_done()
        assert hass.states.get(ids['separate_controls']).state == 'on'
        assert hass.states.get(ids['target']).state == 'unavailable'
        assert hass.states.get(ids['charge_target']).state != 'unavailable'
        await hass.services.async_call('number', 'set_value', {'entity_id': ids['charge_target'], 'value': 250}, blocking=True)
        assert runtime.manual_charge_w == 250 and runtime.target == 0
        await hass.services.async_call('select', 'select_option', {'entity_id': ids['operation'], 'option': 'charge'}, blocking=True)
        await hass.async_block_till_done()
        assert runtime.operation == 'charge' and runtime.enabled
        assert hass.states.get(ids['operation']).state == 'charge'
        await hass.services.async_call('select', 'select_option', {'entity_id': ids['operation'], 'option': 'standby'}, blocking=True)
        assert not runtime.enabled
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        restored = hass.data[DOMAIN][entry.entry_id]
        assert restored.separate_controls and not restored.enabled and restored.target == 0
        assert restored.manual_charge_w == 250 and restored.operation == "standby"
        from dataclasses import asdict
        from custom_components.virtual_battery_pool.const import BatteryConfig
        battery = BatteryConfig('Test battery', 5, 'sensor.test_soc', 'sensor.test_power', 'number.test_command', 'number.set_value', adapter_confirmed=True)
        hass.states.async_set('sensor.test_soc', '50', {'unit_of_measurement': '%'})
        hass.states.async_set('sensor.test_power', '0', {'unit_of_measurement': 'W'})
        hass.states.async_set('number.test_command', '0', {'unit_of_measurement': 'W', 'min': -1000, 'max': 1000, 'step': 1})
        hass.config_entries.async_update_entry(entry, options={'batteries': [asdict(battery)]})
        await hass.async_block_till_done()
        await hass.services.async_call('switch', 'turn_off', {'entity_id': ids['simulation']}, blocking=True)
        await hass.async_block_till_done()
        live_runtime = hass.data[DOMAIN][entry.entry_id]
        assert live_runtime.live and not live_runtime.enabled and live_runtime.operation == 'standby'
        assert entry.options['live_control'] is True
        assert hass.states.get(ids['simulation']).state == 'off'
        await hass.services.async_call('switch', 'turn_on', {'entity_id': ids['simulation']}, blocking=True)
        await hass.async_block_till_done()
        assert not hass.data[DOMAIN][entry.entry_id].live
        assert entry.options['live_control'] is False
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.entry_id not in hass.data[DOMAIN]
    finally:
        await hass.async_stop()
