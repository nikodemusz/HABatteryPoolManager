from datetime import datetime, timedelta, timezone

from custom_components.virtual_battery_pool.const import BatteryConfig, ControllerConfig
from custom_components.virtual_battery_pool.controller import BatteryReading, Direction, PoolController


NOW = datetime.now(timezone.utc)


def setup_controller() -> PoolController:
    batteries = (
        BatteryConfig("venus_c", 2.56, "sensor.c_soc", "sensor.c_power", "number.c", "number.set_value", max_charge_w=800, max_discharge_w=800),
        BatteryConfig("venus_e", 5.12, "sensor.e_soc", "sensor.e_power", "number.e", "number.set_value", max_charge_w=800, max_discharge_w=800),
    )
    return PoolController(ControllerConfig(batteries, deadband_w=40, ramp_w_per_minute=6000, reversal_guard_seconds=0))


def readings(c_soc=60, e_soc=50, at=NOW):
    return {
        "venus_c": BatteryReading(c_soc, 0, at),
        "venus_e": BatteryReading(e_soc, 0, at),
    }


def test_stale_sensor_forces_zero_commands():
    plan = setup_controller().plan(readings(at=NOW - timedelta(seconds=61)), 500, NOW)
    assert plan.reason == "stale_or_invalid_data"
    assert all(command.power_w == 0 for command in plan.commands)


def test_invalid_soc_forces_zero_commands():
    values = readings(c_soc=101)
    plan = setup_controller().plan(values, 500, NOW)
    assert plan.reason == "stale_or_invalid_data"


def test_charge_prefers_lower_soc_and_caps_ramp():
    plan = setup_controller().plan(readings(), -500, NOW)
    allocations = {command.battery: command.power_w for command in plan.commands}
    assert allocations["venus_e"] == 500
    assert allocations["venus_c"] == 0
    assert all(command.direction in (Direction.CHARGE, Direction.IDLE) for command in plan.commands)
    assert sum(command.power_w for command in plan.commands) <= 500


def test_reversal_is_blocked_on_first_opposite_request():
    controller = setup_controller()
    controller.plan(readings(), -500, NOW)
    plan = controller.plan(readings(), 500, NOW + timedelta(seconds=1))
    assert plan.reason == "direction_reversal_guard"
    assert all(command.power_w == 0 for command in plan.commands)


def test_discharge_respects_per_battery_soc_floor():
    batteries = (
        BatteryConfig("venus_c", 2.56, "sensor.c_soc", "sensor.c_power", "number.c", "number.set_value", min_soc=20),
        BatteryConfig("venus_e", 5.12, "sensor.e_soc", "sensor.e_power", "number.e", "number.set_value", min_soc=0),
    )
    controller = PoolController(ControllerConfig(batteries, deadband_w=40, ramp_w_per_minute=6000, reversal_guard_seconds=0))
    values = readings(c_soc=20, e_soc=0)
    plan = controller.plan(values, 500, NOW)
    assert all(command.power_w == 0 for command in plan.commands)


def test_pool_soc_is_capacity_weighted():
    plan = setup_controller().plan(readings(c_soc=100, e_soc=0), 0, NOW)
    assert plan.pool_soc == 33.3


def test_ramp_accumulates_and_safety_stop_is_immediate():
    controller = PoolController(ControllerConfig(setup_controller().config.batteries))
    a = controller.plan(readings(), 1000, NOW)
    b = controller.plan(readings(), 1000, NOW + timedelta(seconds=5))
    assert sum(c.power_w for c in a.commands) == 25
    assert sum(c.power_w for c in b.commands) == 50
    stop = controller.plan(readings(c_soc=20, e_soc=20), 1000, NOW + timedelta(seconds=6))
    assert all(c.power_w == 0 for c in stop.commands)


def test_reversal_waits_full_zero_interval():
    controller = PoolController(ControllerConfig(setup_controller().config.batteries))
    controller.plan(readings(), -1000, NOW)
    assert controller.plan(readings(), 1000, NOW + timedelta(seconds=5)).reason == 'direction_reversal_guard'
    assert controller.plan(readings(), 1000, NOW + timedelta(seconds=34)).reason == 'direction_reversal_guard'
    assert controller.plan(readings(), 1000, NOW + timedelta(seconds=35)).reason == 'dispatch'


def test_nonfinite_readings_and_requests_stop():
    for value in [float('nan'), float('inf'), -float('inf')]:
        controller = setup_controller()
        assert controller.plan(readings(), value, NOW).reason == 'stale_or_invalid_data'
        rs = readings()
        rs['venus_c'] = BatteryReading(50, value, NOW)
        assert controller.plan(rs, 500, NOW).pool_soc is None


def test_zero_ramp_applies_bounded_request_immediately():
    controller = PoolController(ControllerConfig(setup_controller().config.batteries, ramp_w_per_minute=0))
    plan = controller.plan(readings(), -1300, NOW)
    assert sum(c.power_w for c in plan.commands) == 1300
    assert all(c.power_w <= 800 for c in plan.commands)
    assert controller.plan(readings(), 1300, NOW + timedelta(seconds=1)).reason == 'direction_reversal_guard'
