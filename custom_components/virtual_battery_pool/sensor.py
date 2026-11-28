from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from .const import DOMAIN
from .entity import PoolEntity


async def async_setup_entry(hass, entry, async_add_entities):
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PoolSensor(runtime, key, name, device_class, unit) for key, name, device_class, unit in (
        ("soc", "Pool state of charge", SensorDeviceClass.BATTERY, "%"),
        ("measured", "Measured AC power", SensorDeviceClass.POWER, "W"),
        ("planned", "Planned AC power", SensorDeviceClass.POWER, "W"),
        ("status", "Control status", None, None))])


class PoolSensor(PoolEntity, SensorEntity):
    def __init__(self, runtime, key, name, device_class, unit):
        super().__init__(runtime, key, name)
        self.key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        if key == "status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["disabled", "dispatch", "inside_deadband", "soc_limit", "direction_reversal_guard", "stale_or_invalid_data", "no_batteries", "fault", "invalid_readings", "invalid_limits", "target_expired"]
        if unit:
            self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        return getattr(self.runtime, self.key)

    @property
    def extra_state_attributes(self):
        if self.key == "status":
            return {"simulation": not self.runtime.live, "mode": self.runtime.mode,
                    "fault": self.runtime.fault, "input_error": self.runtime.input_error, "battery_plan_w": self.runtime.outputs,
                    "target_timeout_seconds": None if self.runtime.manual_direction else 60, "control_interface": self.runtime.control_interface,
                    "operation": self.runtime.operation, "manual_request": self.runtime.manual_direction is not None,
                    "ramp_w_per_minute": self.runtime.controller.config.ramp_w_per_minute if self.runtime.controller else 0}
        return None
