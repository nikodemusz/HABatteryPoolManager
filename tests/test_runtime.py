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
    assert rig.calls == [('number.a', -20), ('number.b', 0)]
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
