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
    plan = setup_controller().plan(readings(at=NOW - timedelta(seconds=60)), -500, NOW)
    assert plan.reason == "stale_or_invalid_data"
    assert all(command.power_w == 0 for command in plan.commands)


def test_invalid_soc_forces_zero_commands():
    values = readings(c_soc=101)
    plan = setup_controller().plan(values, 500, NOW)
    assert plan.reason == "stale_or_invalid_data"


def test_charge_prefers_lower_soc_and_caps_ramp():
    plan = setup_controller().plan(readings(), -500, NOW)
    assert plan.commands[0].battery == "venus_e"
    assert all(command.direction in (Direction.CHARGE, Direction.IDLE) for command in plan.commands)
    assert sum(command.power_w for command in plan.commands) <= 500


def test_reversal_is_blocked_on_first_opposite_request():
    controller = setup_controller()
    controller.plan(readings(), -500, NOW)
    plan = controller.plan(readings(), 500, NOW + timedelta(seconds=1))
    assert plan.reason == "direction_reversal_guard"
    assert all(command.power_w == 0 for command in plan.commands)
