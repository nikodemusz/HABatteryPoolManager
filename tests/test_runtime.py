"""Runtime tests with real HA states/services and a controllable planning clock."""
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.virtual_battery_pool.const import BatteryConfig, DOMAIN
from custom_components.virtual_battery_pool.runtime import PoolRuntime
import custom_components.virtual_battery_pool.runtime as runtime_module


@pytest_asyncio.fixture
async def rig(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    clock = SimpleNamespace(now=dt_util.utcnow() + timedelta(seconds=1))
    monkeypatch.setattr(runtime_module, 'dt_util', SimpleNamespace(utcnow=lambda: clock.now))
    batteries = [BatteryConfig('A', 5, 'sensor.a_soc', 'sensor.a_power', 'number.a', 'number.set_value', adapter_confirmed=True),
                 BatteryConfig('B', 5, 'sensor.b_soc', 'sensor.b_power', 'number.b', 'number.set_value', adapter_confirmed=True)]
    attrs = {'unit_of_measurement': 'W', 'min': -1000, 'max': 1000, 'step': 1}
    for b in batteries:
        hass.states.async_set(b.soc_entity, '50', {'unit_of_measurement': '%'})
        hass.states.async_set(b.power_entity, '0', {'unit_of_measurement': 'W'})
        hass.states.async_set(b.command_entity, '0', attrs)
    calls = []
    fail = set()
    async def write(call):
        entity, value = call.data['entity_id'], call.data['value']
        calls.append((entity, value))
        if entity in fail and value != 0:
            raise ValueError('Device rejected command')
        hass.states.async_set(entity, str(value), attrs)
    hass.services.async_register('number', 'set_value', write)
    entry = SimpleNamespace(entry_id='pool', title='Pool', options={'batteries': [asdict(b) for b in batteries]})
    r = PoolRuntime(hass, entry)
    hass.data[DOMAIN] = {'pool': r}
    yield SimpleNamespace(hass=hass, clock=clock, runtime=r, entry=entry, calls=calls, fail=fail, attrs=attrs)
    await r.shutdown()
    await hass.async_stop()


def live(rig):
    rig.runtime.live = True


@pytest.mark.asyncio
async def test_default_simulation_and_no_restart_restore(rig):
    r = rig.runtime
    assert not r.enabled and not r.live
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    assert r.planned > 0 and rig.calls == []
    assert not PoolRuntime(rig.hass, rig.entry).enabled


@pytest.mark.asyncio
async def test_live_dispatch_caps_and_disable_zero(rig):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.calls.clear()
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert sum(v for _, v in rig.calls) == 25
    assert r.status == 'dispatch'
    await r.set_enabled(False)
    assert rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]
    assert r.target == 0 and not r.enabled


@pytest.mark.asyncio
async def test_expired_target_stale_sensor_and_unknown_stop(rig):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    rig.hass.states.async_set('sensor.a_soc', 'unavailable', {'unit_of_measurement': '%'})
    await r.update()
    assert r.planned == 0 and rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]
    assert r.status == 'invalid_readings'
    rig.clock.now += timedelta(seconds=70)
    await r.update()
    assert r.planned == 0


@pytest.mark.asyncio
async def test_target_timeout_with_fresh_sensors(rig, monkeypatch):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    rig.clock.now += timedelta(seconds=61)
    original = r._read
    def fresh(entity, unit, now):
        # Test target expiry independently from sensor freshness.
        state = rig.hass.states.get(entity)
        return float(state.state), now
    monkeypatch.setattr(r, '_read', fresh)
    await r.update()
    assert r.status == 'target_expired' and r.planned == 0
    assert rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]


@pytest.mark.asyncio
async def test_service_failure_stops_all_and_latches(rig):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    r.batteries = tuple(replace(b, max_discharge_w=25) for b in r.batteries)
    from custom_components.virtual_battery_pool.controller import PoolController
    from custom_components.virtual_battery_pool.const import ControllerConfig
    r.controller = PoolController(ControllerConfig(r.batteries))
    rig.fail.add('number.b')
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    rig.clock.now += timedelta(seconds=5)
    await r.update()
    assert r.fault and r.status == 'fault'
    assert rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]
    rig.calls.clear()
    await r.update()
    assert all(v == 0 for _, v in rig.calls)


@pytest.mark.asyncio
async def test_unconfirmed_adapter_or_invalid_range_blocks_activation(rig):
    live(rig)
    r = rig.runtime
    r.batteries = (replace(r.batteries[0], adapter_confirmed=False),)
    with pytest.raises(ValueError, match='contract'):
        await r.set_enabled(True)
    assert not r.enabled and not rig.calls
    r.batteries = (replace(r.batteries[0], adapter_confirmed=True),)
    rig.hass.states.async_set('number.a', '0', {**rig.attrs, 'min': 0})
    with pytest.raises(ValueError, match='range'):
        await r.set_enabled(True)
    assert not r.enabled and not rig.calls


@pytest.mark.asyncio
async def test_charge_sign_and_reversal_guard(rig):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-1000)
    assert any(v < 0 for _, v in rig.calls)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert r.status == 'direction_reversal_guard'
    assert rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]


@pytest.mark.asyncio
async def test_grid_balancing_and_missing_grid_stop(rig):
    live(rig)
    r = rig.runtime
    r.mode = 'autonomous'
    rig.entry.options.update(grid_power_entity='sensor.grid', grid_confirmed=True, grid_target_w=20)
    rig.hass.states.async_set('sensor.grid', '500', {'unit_of_measurement': 'W'})
    await r.set_enabled(True)
    assert r.planned == 25
    rig.hass.states.async_set('sensor.grid', 'unavailable', {'unit_of_measurement': 'W'})
    rig.clock.now += timedelta(seconds=5)
    await r.update()
    assert r.planned == 0 and r.status == 'invalid_readings'
    assert rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]


@pytest.mark.asyncio
async def test_cross_pool_collision_blocks_activation(rig):
    live(rig)
    r = rig.runtime
    other = PoolRuntime(rig.hass, rig.entry)
    other.enabled = other.live = True
    rig.hass.data[DOMAIN]['other'] = other
    with pytest.raises(ValueError, match='another pool'):
        await r.set_enabled(True)
    assert not rig.calls


@pytest.mark.asyncio
async def test_shutdown_cancels_and_zeroes(rig):
    live(rig)
    r = rig.runtime
    r.start()
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    await r.shutdown()
    assert not r.enabled and rig.calls[-2:] == [('number.a', 0), ('number.b', 0)]
    rig.calls.clear()
    await r.set_target(1000)
    assert not rig.calls


@pytest.mark.asyncio
async def test_inverted_adapter_sign_and_step_rounds_toward_zero(rig):
    live(rig)
    r = rig.runtime
    r.batteries = tuple(replace(b, positive_discharge=False) for b in r.batteries)
    rig.attrs['step'] = 10
    for b in r.batteries:
        rig.hass.states.async_set(b.command_entity, '0', rig.attrs)
    await r.set_enabled(True)
    rig.calls.clear()
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    assert rig.calls == [('number.b', 0), ('number.a', -20)]
    assert r.planned == 25


@pytest.mark.asyncio
async def test_failed_stop_retries_after_switch_off(rig, monkeypatch):
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    original = r._write
    async def offline(b, value):
        if b.name == 'A':
            raise ValueError('offline')
        await original(b, value)
    monkeypatch.setattr(r, '_write', offline)
    await r.set_enabled(False)
    assert not r.enabled and r._stop_pending and r.fault
    monkeypatch.setattr(r, '_write', original)
    rig.calls.clear()
    await r.update()
    assert rig.calls == [('number.a', 0), ('number.b', 0)]
    assert not r._stop_pending



def separate_adapter(rig, **changes):
    rig.attrs.update(min=0)
    b = replace(rig.runtime.batteries[0], command_mode='separate', command_entity='', command_service='',
                charge_command_entity='number.charge', discharge_command_entity='number.discharge',
                max_charge_entity='number.charge_limit', max_discharge_entity='number.discharge_limit', **changes)
    for entity, value in [('number.charge', 0), ('number.discharge', 0), ('number.charge_limit', 120), ('number.discharge_limit', 80)]:
        rig.hass.states.async_set(entity, str(value), rig.attrs)
    rig.runtime.batteries = (b,)
    rig.entry.options['batteries'] = [asdict(b)]
    return b


@pytest.mark.asyncio
@pytest.mark.parametrize(('requested_power', 'active', 'limit'), [(-1000, 'number.charge', 120), (1000, 'number.discharge', 80)])
async def test_separate_setpoints_respect_independent_limits_without_writing_them(rig, requested_power, active, limit):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.calls.clear()
    for _ in range(8):
        rig.clock.now += timedelta(seconds=5)
        await r.set_target(requested_power)
    assert abs(r.planned) == limit
    assert all(entity in ('number.charge', 'number.discharge') for entity, _ in rig.calls)
    assert all(0 <= value <= limit for _, value in rig.calls)
    assert any(entity == active and value == limit for entity, value in rig.calls)
    assert rig.hass.states.get('number.charge_limit').state == '120'
    assert rig.hass.states.get('number.discharge_limit').state == '80'
    state = {'number.charge': 0, 'number.discharge': 0}
    for entity, value in rig.calls:
        state[entity] = value
        assert not all(v > 0 for v in state.values())
    await r.set_enabled(False)
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]


@pytest.mark.asyncio
async def test_separate_direction_switch_zeroes_other_endpoint_first(rig):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-1000)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert r.planned == 0 and r.status == 'direction_reversal_guard'
    rig.clock.now += timedelta(seconds=30)
    rig.calls.clear()
    await r.update()
    assert rig.calls == [('number.charge', 0), ('number.discharge', 25)]


@pytest.mark.asyncio
async def test_limit_zero_disables_only_its_direction_and_changes_take_effect(rig):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    rig.hass.states.async_set('number.charge_limit', '0', rig.attrs)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-1000)
    assert r.planned == 0
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert r.planned == 25
    rig.hass.states.async_set('number.discharge_limit', '10', rig.attrs)
    rig.clock.now += timedelta(seconds=5)
    await r.update()
    assert r.planned == 10


@pytest.mark.asyncio
@pytest.mark.parametrize('value', ['unavailable', 'nan', '-10'])
async def test_invalid_limit_stops_both_setpoints(rig, value):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    rig.hass.states.async_set('number.discharge_limit', value, rig.attrs)
    await r.update()
    assert r.status == 'invalid_limits' and r.planned == 0
    assert r.input_error
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]


@pytest.mark.asyncio
async def test_unitless_controls_need_explicit_watt_confirmation(rig):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    rig.attrs.pop('unit_of_measurement')
    for entity in ('number.charge', 'number.discharge', 'number.charge_limit', 'number.discharge_limit'):
        old = rig.hass.states.get(entity)
        rig.hass.states.async_set(entity, old.state, rig.attrs)
    with pytest.raises(ValueError, match='not in W'):
        await r.set_enabled(True)
    assert rig.calls == []
    r.batteries = tuple(replace(b, power_unit_w_confirmed=True) for b in r.batteries)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert r.planned == 25
    rig.hass.states.async_set('number.charge', '0', {**rig.attrs, 'unit_of_measurement': 'A'})
    with pytest.raises(ValueError, match='not in W'):
        await r.set_enabled(True)


@pytest.mark.asyncio
async def test_separate_write_failure_stops_both_without_touching_limits(rig):
    separate_adapter(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.fail.add('number.discharge')
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(1000)
    assert r.fault
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]
    assert all(entity not in ('number.charge_limit', 'number.discharge_limit') for entity, _ in rig.calls)


@pytest.mark.asyncio
async def test_limit_command_overlap_blocks_live_activation(rig):
    b = separate_adapter(rig)
    live(rig)
    rig.runtime.batteries = (replace(b, max_charge_entity=b.charge_command_entity),)
    with pytest.raises(ValueError, match='overlap'):
        await rig.runtime.set_enabled(True)
    assert rig.calls == []


@pytest.mark.asyncio
async def test_persistent_number_limits_do_not_expire_but_dynamic_sensor_limits_do(rig):
    b = separate_adapter(rig)
    rig.clock.now += timedelta(seconds=70)
    assert rig.runtime._effective_batteries(rig.clock.now)[0].max_discharge_w == 80
    rig.hass.states.async_set('sensor.dynamic_limit', '80', {'unit_of_measurement': 'W'})
    rig.runtime.batteries = (replace(b, max_discharge_entity='sensor.dynamic_limit'),)
    with pytest.raises(ValueError, match='stale'):
        rig.runtime._effective_batteries(rig.clock.now)


def attach_mode(rig, domain='select'):
    b = separate_adapter(rig)
    entity = f'{domain}.force_mode'
    rig.runtime.batteries = (replace(b, mode_entity=entity, charge_mode='charge', discharge_mode='discharge', idle_mode='standby'),)
    rig.entry.options['batteries'] = [asdict(rig.runtime.batteries[0])]
    attrs = {'options': ['standby', 'charge', 'discharge']}
    rig.hass.states.async_set(entity, 'standby', attrs)
    device = SimpleNamespace(events=[], reject=set(), acknowledge=True, entity=entity, attrs=attrs)
    original = rig.runtime._write
    async def logged_power(battery, value):
        device.events.append(('power', battery.command_entity, value))
        await original(battery, value)
    rig.runtime._write = logged_power
    async def mode(call):
        option = call.data['option']
        device.events.append(('mode', call.data['entity_id'], option))
        if option in device.reject:
            raise ValueError('Mode rejected')
        if device.acknowledge:
            rig.hass.states.async_set(entity, option, attrs)
    rig.hass.services.async_register(domain, 'select_option', mode)
    return device


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['select', 'input_select'])
@pytest.mark.parametrize(('power', 'option'), [(-500, 'charge'), (500, 'discharge')])
async def test_mode_is_selected_between_zero_and_power_commands(rig, domain, power, option):
    device = attach_mode(rig, domain)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    device.events.clear()
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(power)
    mode_index = device.events.index(('mode', device.entity, option))
    before = device.events[:mode_index]
    assert ('power', 'number.charge', 0) in before
    assert ('power', 'number.discharge', 0) in before
    assert all(value == 0 for kind, _, value in before if kind == 'power')
    assert any(kind == 'power' and value > 0 for kind, _, value in device.events[mode_index+1:])
    assert not r.fault


@pytest.mark.asyncio
async def test_reversal_waits_in_idle_before_selecting_opposite_mode(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    assert rig.hass.states.get(device.entity).state == 'charge'
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    assert r.status == 'direction_reversal_guard'
    assert rig.hass.states.get(device.entity).state == 'standby'
    rig.clock.now += timedelta(seconds=29)
    await r.update()
    assert rig.hass.states.get(device.entity).state == 'standby'
    rig.clock.now += timedelta(seconds=1)
    await r.update()
    assert rig.hass.states.get(device.entity).state == 'discharge'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['reject', 'unconfirmed'])
async def test_failed_mode_change_never_sends_nonzero_power(rig, failure):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    if failure == 'reject':
        device.reject.add('charge')
    else:
        device.acknowledge = False
    device.events.clear()
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    assert r.fault and r.status == 'fault'
    assert all(value == 0 for kind, _, value in device.events if kind == 'power')
    assert ('mode', device.entity, 'standby') in device.events
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]


@pytest.mark.asyncio
async def test_mode_fault_stop_retries_idle_even_after_switch_off(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    device.reject.add('standby')
    await r.set_enabled(False)
    assert r.fault and r._stop_pending
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]
    device.reject.clear()
    await r.update()
    assert not r._stop_pending
    assert rig.hass.states.get(device.entity).state == 'standby'


@pytest.mark.asyncio
async def test_mode_does_not_change_in_simulation(rig):
    device = attach_mode(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    assert r.planned < 0
    assert device.events == [] and rig.calls == []
    assert rig.hass.states.get(device.entity).state == 'standby'


@pytest.mark.asyncio
async def test_unavailable_mode_blocks_activation_without_device_actions(rig):
    device = attach_mode(rig)
    live(rig)
    rig.hass.states.async_set(device.entity, 'unavailable', device.attrs)
    with pytest.raises(ValueError, match='Mode entity'):
        await rig.runtime.set_enabled(True)
    assert not rig.runtime.enabled and device.events == []


@pytest.mark.asyncio
async def test_quantized_zero_request_keeps_idle_mode(rig):
    device = attach_mode(rig)
    rig.attrs['step'] = 50
    for entity in ('number.charge', 'number.discharge'):
        rig.hass.states.async_set(entity, '0', rig.attrs)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    assert r.planned == -25
    assert rig.hass.states.get(device.entity).state == 'standby'
    rig.clock.now += timedelta(seconds=5)
    await r.update()
    assert rig.hass.states.get(device.entity).state == 'charge'


@pytest.mark.asyncio
async def test_shutdown_requests_idle_after_zeroing_both_directions(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    device.events.clear()
    await r.shutdown()
    assert device.events == [('power', 'number.charge', 0), ('power', 'number.discharge', 0), ('mode', device.entity, 'standby')]


@pytest.mark.asyncio
async def test_empty_timeout_exception_still_latches_a_fault(rig, monkeypatch):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    original = r._write_mode
    async def timeout(battery, option):
        if option == 'charge':
            raise TimeoutError()
        await original(battery, option)
    monkeypatch.setattr(r, '_write_mode', timeout)
    device.events.clear()
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    assert r.fault == 'TimeoutError' and r.status == 'fault'
    assert all(value == 0 for kind, _, value in device.events if kind == 'power')


@pytest.mark.asyncio
async def test_two_batteries_cannot_share_a_mode_selector(rig):
    device = attach_mode(rig)
    live(rig)
    b = rig.runtime.batteries[0]
    other = replace(b, name='B', command_mode='signed', command_entity='number.b', command_service='number.set_value')
    rig.runtime.batteries = (b, other)
    with pytest.raises(ValueError, match='Duplicate'):
        await rig.runtime.set_enabled(True)
    assert device.events == []


@pytest.mark.asyncio
async def test_failed_reactivation_stops_previous_requests_and_selects_idle(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(-500)
    rig.hass.states.async_set(device.entity, 'unavailable', device.attrs)
    device.events.clear()
    with pytest.raises(ValueError, match='Mode entity'):
        await r.set_enabled(True)
    assert not r.enabled and r.planned == 0
    assert device.events == [('power', 'number.charge', 0), ('power', 'number.discharge', 0), ('mode', device.entity, 'standby')]


@pytest.mark.asyncio
async def test_pool_controls_switch_persists_only_interface_and_clears_request(rig):
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    await r.set_separate_controls(True)
    assert r.separate_controls and r.target == 0 and r.target_at is None
    assert r.planned == 0
    await r.set_direction_target('charge', 500)
    restored = PoolRuntime(rig.hass, rig.entry)
    await restored.async_initialize()
    assert restored.separate_controls and restored.target == 0 and not restored.enabled
    assert rig.calls == []  # Still simulation.


@pytest.mark.asyncio
async def test_pool_separate_requests_are_mutually_exclusive_and_keep_reversal_guard(rig):
    r = rig.runtime
    await r.set_separate_controls(True)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('charge', 500)
    assert r.target == -500 and r.charge_target == 500 and r.discharge_target == 0
    assert r.planned == -25
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('discharge', 800)
    assert r.target == 800 and r.charge_target == 0 and r.discharge_target == 800
    assert r.planned == 0 and r.status == 'direction_reversal_guard'
    rig.clock.now += timedelta(seconds=30)
    await r.update()
    assert r.planned > 0


@pytest.mark.asyncio
async def test_zero_on_inactive_pool_channel_does_not_cancel_or_refresh_other_channel(rig):
    r = rig.runtime
    await r.set_separate_controls(True)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('charge', 500)
    stamp = r.target_at
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('discharge', 0)
    assert r.target == -500 and r.target_at == stamp
    await r.set_direction_target('charge', 0)
    assert r.target == 0 and r.planned == 0


@pytest.mark.asyncio
async def test_layout_switch_zeroes_live_requests_and_device_mode(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    device.events.clear()
    await r.set_separate_controls(True)
    assert r.target == 0 and r.planned == 0
    assert ('power', 'number.charge', 0) in device.events
    assert ('power', 'number.discharge', 0) in device.events
    assert ('mode', device.entity, 'standby') in device.events
    assert not any(kind == 'power' and value > 0 for kind, _, value in device.events)


@pytest.mark.asyncio
async def test_only_selected_pool_interface_accepts_requests(rig):
    r = rig.runtime
    with pytest.raises(ValueError, match='inactive'):
        await r.set_direction_target('charge', 100)
    await r.set_separate_controls(True)
    with pytest.raises(ValueError, match='inactive'):
        await r.set_target(100)
    assert r.target == 0
    r.mode = 'autonomous'
    with pytest.raises(ValueError, match='external'):
        await r.set_direction_target('charge', 100)


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [-1, float('nan'), float('inf'), 100001, 'invalid'])
async def test_pool_direction_requests_reject_invalid_values(rig, value):
    await rig.runtime.set_separate_controls(True)
    with pytest.raises(ValueError):
        await rig.runtime.set_direction_target('charge', value)
    assert rig.runtime.target == 0 and rig.calls == []


@pytest.mark.asyncio
async def test_invalid_unitless_limits_do_not_hide_valid_soc_and_measured_power(rig):
    b = separate_adapter(rig)
    r = rig.runtime
    rig.hass.states.async_set(b.soc_entity, '73', {'unit_of_measurement': '%'})
    rig.hass.states.async_set(b.power_entity, '125', {'unit_of_measurement': 'W'})
    rig.hass.states.async_set(b.max_charge_entity, '120', {'min': 0, 'max': 1000, 'step': 1})
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_target(500)
    assert r.soc == 73 and r.measured == 125
    assert r.status == 'invalid_limits' and r.planned == 0 and r.input_error
    assert rig.calls == []


@pytest.mark.asyncio
async def test_pool_input_entities_follow_selected_interface(rig):
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.virtual_battery_pool.number import PoolTarget, PoolDirectionTarget
    from custom_components.virtual_battery_pool.switch import PoolSeparateControls
    r = rig.runtime
    signed = PoolTarget(r)
    charge = PoolDirectionTarget(r, 'charge')
    discharge = PoolDirectionTarget(r, 'discharge')
    layout = PoolSeparateControls(r)
    assert signed.available and not charge.available and not discharge.available
    await layout.async_turn_on()
    assert layout.is_on and not signed.available and charge.available and discharge.available
    await charge.async_set_native_value(250)
    assert charge.native_value == 250 and discharge.native_value == 0
    await discharge.async_set_native_value(500)
    assert charge.native_value == 0 and discharge.native_value == 500
    with pytest.raises(HomeAssistantError):
        await signed.async_set_native_value(100)
    await layout.async_turn_off()
    assert signed.available and r.target == 0
    r.mode = 'autonomous'
    assert not signed.available and not charge.available and not discharge.available and not layout.available


@pytest.mark.asyncio
async def test_separate_pool_controls_drive_device_modes_and_both_directions(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_separate_controls(True)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('charge', 500)
    assert rig.hass.states.get(device.entity).state == 'charge'
    assert rig.hass.states.get('number.charge').state == '25.0'
    assert rig.hass.states.get('number.discharge').state == '0'
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('discharge', 500)
    assert r.planned == 0 and rig.hass.states.get(device.entity).state == 'standby'
    rig.clock.now += timedelta(seconds=30)
    await r.update()
    assert rig.hass.states.get(device.entity).state == 'discharge'
    assert rig.hass.states.get('number.charge').state == '0'
    assert rig.hass.states.get('number.discharge').state == '25.0'


@pytest.mark.asyncio
async def test_separate_charge_request_stops_at_soc_ceiling_and_on_expiry(rig):
    device = attach_mode(rig)
    live(rig)
    r = rig.runtime
    await r.set_separate_controls(True)
    await r.set_enabled(True)
    rig.clock.now += timedelta(seconds=5)
    await r.set_direction_target('charge', 500)
    rig.hass.states.async_set(r.batteries[0].soc_entity, '80', {'unit_of_measurement': '%'})
    await r.update()
    assert r.planned == 0 and r.status == 'soc_limit'
    assert rig.hass.states.get(device.entity).state == 'standby'
    rig.hass.states.async_set(r.batteries[0].soc_entity, '50', {'unit_of_measurement': '%'})
    r.target_at -= timedelta(seconds=61)
    await r.set_direction_target('discharge', 0)
    await r.update()
    assert r.status == 'target_expired' and r.planned == 0
    assert rig.hass.states.get(device.entity).state == 'standby'
    assert rig.calls[-2:] == [('number.charge', 0), ('number.discharge', 0)]
