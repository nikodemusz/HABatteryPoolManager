"""Exercise options against Home Assistant's actual flow and selector classes."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
import voluptuous as vol

from custom_components.virtual_battery_pool.config_flow import BatteryPoolOptionsFlow, battery_schema


def values(**overrides):
    return battery_schema()({
        'name': 'Garage', 'soc_entity': 'sensor.soc',
        'power_entity': 'sensor.power', 'command_entity': 'number.command',
        **overrides,
    })


@pytest_asyncio.fixture
async def context(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    for entity in ['sensor.soc', 'sensor.power', 'number.command', 'input_number.command']:
        hass.states.async_set(entity, '50')
    entry = SimpleNamespace(options={})
    hass.config_entries = Mock()
    hass.config_entries.async_get_known_entry.return_value = entry
    def flow():
        result = BatteryPoolOptionsFlow()
        result.hass = hass
        result.handler = 'pool'
        return result
    yield entry, flow
    await hass.async_stop()


@pytest.mark.asyncio
async def test_add_edit_remove_and_preserve_other_options(context):
    entry, new_flow = context
    entry.options = {'other': True}
    flow = new_flow()
    assert (await flow.async_step_init())['menu_options'] == ['add_battery']
    result = await flow.async_step_add_battery(values())
    entry.options = result['data']
    battery = entry.options['batteries'][0]
    assert battery['command_service'] == 'number.set_value'
    assert entry.options['other'] is True
    flow = new_flow()
    await flow.async_step_init()
    await flow.async_step_edit_battery({'battery': battery['id']})
    result = await flow.async_step_battery(values(name='Renamed', command_entity='input_number.command'))
    assert result['data']['batteries'][0]['id'] == battery['id']
    assert result['data']['batteries'][0]['command_service'] == 'input_number.set_value'
    # An abandoned edit must leave saved mappings untouched.
    assert entry.options['batteries'][0]['name'] == 'Garage'
    entry.options = result['data']
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_remove_battery({'battery': battery['id']})
    assert result['step_id'] == 'confirm_remove'
    assert len(entry.options['batteries']) == 1
    result = await flow.async_step_confirm_remove({})
    assert result['data'] == {'other': True, 'batteries': []}


@pytest.mark.asyncio
async def test_validation_and_multiple_batteries(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_add_battery(values())
    entry.options = result['data']
    flow = new_flow()
    await flow.async_step_init()
    assert (await flow.async_step_add_battery(values(name=' garage ')))['errors']['name'] == 'duplicate_name'
    assert (await flow.async_step_add_battery(values(name='Other', min_soc=90, max_soc=10)))['errors']['min_soc'] == 'invalid_soc_range'
    assert (await flow.async_step_add_battery(values(name='Other', soc_entity='sensor.missing')))['errors']['soc_entity'] == 'entity_missing'
    result = await flow.async_step_add_battery(values(name='Other'))
    assert len(result['data']['batteries']) == 2
    assert len(entry.options['batteries']) == 1


@pytest.mark.parametrize('changes', [{'capacity_kwh': float('nan')}, {'max_charge_w': float('inf')}, {'capacity_kwh': 0}, {'min_soc': -1}, {'max_soc': 101}, {'max_charge_w': -1}, {'command_entity': 'switch.command'}])
def test_invalid_schema_values(changes):
    with pytest.raises(vol.Invalid):
        values(**changes)
