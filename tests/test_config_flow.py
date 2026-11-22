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
        'command_mode': 'signed', 'name': 'Garage', 'soc_entity': 'sensor.soc',
        'power_entity': 'sensor.power', 'command_entity': 'number.command',
        **overrides,
    })


@pytest_asyncio.fixture
async def context(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    for entity in ['sensor.soc', 'sensor.power', 'number.command', 'input_number.command']:
        hass.states.async_set(entity, '50')
    entry = SimpleNamespace(options={}, entry_id='pool')
    hass.config_entries = Mock()
    hass.config_entries.async_entries.return_value = []
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
    assert (await flow.async_step_init())['menu_options'] == ['add_battery', 'control_settings']
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
    result = await flow.async_step_add_battery(values(name='Other', command_entity='input_number.command'))
    assert len(result['data']['batteries']) == 2
    assert len(entry.options['batteries']) == 1


@pytest.mark.parametrize('changes', [{'capacity_kwh': 0}, {'min_soc': -1}, {'max_soc': 101}, {'max_charge_w': -1}, {'command_entity': 'switch.command'}])
def test_invalid_schema_values(changes):
    with pytest.raises(vol.Invalid):
        values(**changes)


@pytest.mark.asyncio
async def test_live_requires_confirmed_adapters_and_grid_requires_confirmation(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    args = {'control_mode': 'external', 'live_control': True, 'grid_confirmed': False, 'grid_target_w': 20}
    result = await flow.async_step_control_settings(args)
    assert result['errors']['live_control'] == 'adapters_required'
    args.update(control_mode='autonomous', live_control=False)
    result = await flow.async_step_control_settings(args)
    assert result['errors']['grid_power_entity'] == 'grid_required'
    assert result['errors']['grid_confirmed'] == 'grid_confirmation_required'
    args.update(control_mode='external')
    result = await flow.async_step_control_settings(args)
    assert result['data']['live_control'] is False


@pytest.mark.asyncio
async def test_duplicate_command_entity_is_rejected(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    entry.options = (await flow.async_step_add_battery(values()))['data']
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_add_battery(values(name='Second'))
    assert result['errors']['command_entity'] == 'duplicate_command'



def api_payload(result):
    """Use the exact HTTP response conversion which raised in production."""
    import json
    from homeassistant.helpers.data_entry_flow import _BaseFlowManagerView
    payload = _BaseFlowManagerView(None)._prepare_result_json(result)
    return json.loads(json.dumps(payload, allow_nan=False))


@pytest.mark.asyncio
async def test_all_setup_forms_serialize_through_home_assistant_http(context):
    from custom_components.virtual_battery_pool.config_flow import VirtualBatteryPoolConfigFlow
    config_flow = VirtualBatteryPoolConfigFlow()
    assert api_payload(await config_flow.async_step_user())['step_id'] == 'user'
    entry, new_flow = context
    flow = new_flow()
    api_payload(await flow.async_step_init())
    add = api_payload(await flow.async_step_add_battery())
    numeric = {f['name']: f['selector']['number'] for f in add['data_schema'] if 'number' in f.get('selector', {})}
    assert set(numeric) == {'capacity_kwh', 'min_soc', 'max_soc', 'max_charge_w', 'max_discharge_w'}
    assert numeric['capacity_kwh']['unit_of_measurement'] == 'kWh'
    assert numeric['min_soc']['max'] == 100
    assert numeric['max_charge_w']['min'] == 1
    settings = api_payload(await flow.async_step_control_settings())
    grid = next(f for f in settings['data_schema'] if f['name'] == 'grid_target_w')
    assert grid['selector']['number']['unit_of_measurement'] == 'W'
    entry.options = (await flow.async_step_add_battery(values()))['data']
    battery_id = entry.options['batteries'][0]['id']
    flow = new_flow()
    api_payload(await flow.async_step_init())
    api_payload(await flow.async_step_edit_battery())
    api_payload(await flow.async_step_edit_battery({'battery': battery_id}))
    api_payload(await flow.async_step_battery(values(name='')))
    api_payload(await flow.async_step_remove_battery())
    api_payload(await flow.async_step_remove_battery({'battery': battery_id}))
    api_payload(await flow.async_step_confirm_remove())


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['capacity_kwh', 'min_soc', 'max_soc', 'max_charge_w', 'max_discharge_w'])
@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf'), 'not a number'])
async def test_invalid_numbers_return_serializable_field_errors(context, field, invalid):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    data = values()
    data[field] = invalid
    result = await flow.async_step_add_battery(data)
    assert result['errors'][field] == 'invalid_number'
    api_payload(result)
    assert entry.options == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), -float('inf'), -1, 10001])
async def test_invalid_grid_target_returns_serializable_error(context, invalid):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_control_settings({'control_mode': 'external', 'live_control': False, 'grid_confirmed': False, 'grid_target_w': invalid})
    assert result['errors']['grid_target_w'] == 'invalid_number'
    api_payload(result)
    assert entry.options == {}



@pytest.mark.asyncio
async def test_separate_controls_and_read_only_limits_round_trip(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    data = values(command_mode='separate', charge_command_entity='number.command', discharge_command_entity='input_number.command', max_charge_entity='sensor.soc', max_discharge_entity='sensor.power')
    # The inactive signed field is discarded rather than mistaken for a third command.
    result = await flow.async_step_add_battery(data)
    battery = result['data']['batteries'][0]
    assert battery['command_mode'] == 'separate'
    assert 'command_entity' not in battery and 'command_service' not in battery
    entry.options = result['data']
    flow = new_flow()
    await flow.async_step_init()
    edit = api_payload(await flow.async_step_edit_battery({'battery': battery['id']}))
    defaults = {f['name']: f.get('default', f.get('description', {}).get('suggested_value')) for f in edit['data_schema']}
    assert defaults['command_mode'] == 'separate'
    assert defaults['charge_command_entity'] == 'number.command'
    assert defaults['max_discharge_entity'] == 'sensor.power'


@pytest.mark.asyncio
async def test_separate_controls_are_required_distinct_and_not_limits(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_add_battery(values(command_mode='separate'))
    assert result['errors']['charge_command_entity'] == 'command_required'
    assert result['errors']['discharge_command_entity'] == 'command_required'
    api_payload(result)
    data = values(command_mode='separate', charge_command_entity='number.command', discharge_command_entity='number.command')
    assert (await flow.async_step_add_battery(data))['errors']['discharge_command_entity'] == 'duplicate_command'
    data.update(discharge_command_entity='input_number.command', max_charge_entity='number.command')
    result = await flow.async_step_add_battery(data)
    assert result['errors']['max_charge_entity'] == 'duplicate_command'
    assert entry.options == {}


@pytest.mark.asyncio
async def test_old_signed_mapping_keeps_signed_mode(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_add_battery(values())
    entry.options = result['data']
    entry.options['batteries'][0].pop('command_mode')
    flow = new_flow()
    await flow.async_step_init()
    result = api_payload(await flow.async_step_edit_battery({'battery': entry.options['batteries'][0]['id']}))
    assert next(f for f in result['data_schema'] if f['name'] == 'command_mode')['default'] == 'signed'



@pytest.mark.asyncio
async def test_optional_maximum_entity_can_be_cleared_when_editing(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    entry.options = (await flow.async_step_add_battery(values(max_charge_entity='sensor.power')))['data']
    battery = entry.options['batteries'][0]
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_edit_battery({'battery': battery['id']})
    submitted = result['data_schema'](values())
    assert 'max_charge_entity' not in submitted
    result = await flow.async_step_battery(submitted)
    assert 'max_charge_entity' not in result['data']['batteries'][0]


def add_mode_entity(flow):
    flow.hass.states.async_set('select.force_mode', 'standby', {'options': ['standby', 'charge', 'discharge']})


@pytest.mark.asyncio
async def test_mode_mapping_uses_actual_options_and_saves_only_after_mapping(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    add_mode_entity(flow)
    result = await flow.async_step_add_battery(values(mode_entity='select.force_mode'))
    assert result['step_id'] == 'battery_modes' and entry.options == {}
    payload = api_payload(result)
    for field in payload['data_schema']:
        assert field['selector']['select']['options'] == ['standby', 'charge', 'discharge']
    result = await flow.async_step_battery_modes({'charge_mode': 'charge', 'discharge_mode': 'discharge', 'idle_mode': 'standby'})
    battery = result['data']['batteries'][0]
    assert battery['mode_entity'] == 'select.force_mode'
    assert battery['idle_mode'] == 'standby'
    entry.options = result['data']
    flow = new_flow()
    await flow.async_step_init()
    await flow.async_step_edit_battery({'battery': battery['id']})
    result = await flow.async_step_battery(values(mode_entity='select.force_mode'))
    payload = api_payload(result)
    assert next(f for f in payload['data_schema'] if f['name'] == 'idle_mode')['default'] == 'standby'


@pytest.mark.asyncio
async def test_invalid_mode_mappings_return_serializable_errors(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    add_mode_entity(flow)
    await flow.async_step_add_battery(values(mode_entity='select.force_mode'))
    result = await flow.async_step_battery_modes({'charge_mode': 'missing', 'discharge_mode': 'discharge', 'idle_mode': 'standby'})
    assert result['errors']['charge_mode'] == 'mode_option_invalid'
    api_payload(result)
    result = await flow.async_step_battery_modes({'charge_mode': 'charge', 'discharge_mode': 'charge', 'idle_mode': 'standby'})
    assert result['errors']['base'] == 'distinct_modes'
    api_payload(result)
    flow.hass.states.async_set('select.force_mode', 'unavailable', {'options': ['standby', 'charge', 'discharge']})
    result = await flow.async_step_battery_modes({'charge_mode': 'charge', 'discharge_mode': 'discharge', 'idle_mode': 'standby'})
    assert result['errors']['base'] == 'mode_unavailable'
    api_payload(result)
    assert entry.options == {}


@pytest.mark.asyncio
async def test_mode_control_must_not_be_shared(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    add_mode_entity(flow)
    await flow.async_step_add_battery(values(mode_entity='select.force_mode'))
    entry.options = (await flow.async_step_battery_modes({'charge_mode': 'charge', 'discharge_mode': 'discharge', 'idle_mode': 'standby'}))['data']
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_add_battery(values(name='Other', command_entity='input_number.command', mode_entity='select.force_mode'))
    assert result['errors']['mode_entity'] == 'duplicate_command'


@pytest.mark.asyncio
async def test_mode_mapping_can_be_removed_without_retaining_stale_options(context):
    entry, new_flow = context
    flow = new_flow()
    await flow.async_step_init()
    add_mode_entity(flow)
    await flow.async_step_add_battery(values(mode_entity='select.force_mode'))
    entry.options = (await flow.async_step_battery_modes({'charge_mode': 'charge', 'discharge_mode': 'discharge', 'idle_mode': 'standby'}))['data']
    flow = new_flow()
    await flow.async_step_init()
    result = await flow.async_step_edit_battery({'battery': entry.options['batteries'][0]['id']})
    submitted = result['data_schema'](values())
    result = await flow.async_step_battery(submitted)
    saved = result['data']['batteries'][0]
    assert not {'mode_entity', 'charge_mode', 'discharge_mode', 'idle_mode'} & saved.keys()
