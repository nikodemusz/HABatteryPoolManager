from __future__ import annotations

from copy import deepcopy
import math
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_BATTERIES, DOMAIN


def finite_number(value):
    value = vol.Coerce(float)(value)
    if not math.isfinite(value):
        raise vol.Invalid("Expected a finite number")
    return value


BATTERY_NUMBER_BOUNDS = {
    "capacity_kwh": (0.001, None),
    "min_soc": (0, 100),
    "max_soc": (0, 100),
    "max_charge_w": (1, None),
    "max_discharge_w": (1, None),
}


def number_selector(minimum, maximum=None, unit=None):
    config = {"min": minimum, "mode": selector.NumberSelectorMode.BOX, "step": "any"}
    if maximum is not None:
        config["max"] = maximum
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def validate_numbers(data, bounds):
    """Validate on submission, outside the schema serialized for the frontend."""
    errors = {}
    for key, (minimum, maximum) in bounds.items():
        try:
            value = finite_number(data[key])
            if value < minimum or (maximum is not None and value > maximum):
                raise vol.Invalid("Number outside permitted range")
            data[key] = value
        except (vol.Invalid, KeyError):
            errors[key] = "invalid_number"
            # Do not return NaN/infinity as form defaults in a JSON response.
            data.pop(key, None)
    return errors


def battery_schema(values=None):
    values = values or {}
    fields = {}
    definitions = {
        "name": ("", str),
        "capacity_kwh": (5.0, number_selector(0.001, unit="kWh")),
        "soc_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "power_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "command_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number"]))),
        "positive_discharge": (True, selector.BooleanSelector()),
        "adapter_confirmed": (False, selector.BooleanSelector()),
        "min_soc": (20.0, number_selector(0, 100, "%")),
        "max_soc": (80.0, number_selector(0, 100, "%")),
        "max_charge_w": (300.0, number_selector(1, unit="W")),
        "max_discharge_w": (300.0, number_selector(1, unit="W")),
    }
    for key, (default, validator) in definitions.items():
        default = values.get(key, default)
        marker = vol.Required(key) if default is None else vol.Required(key, default=default)
        fields[marker] = validator
    return vol.Schema(fields)


class VirtualBatteryPoolConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BatteryPoolOptionsFlow()

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            name = user_input["name"].strip()
            if name:
                return self.async_create_entry(title=name, data={"name": name})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("name", default="Battery Pool"): str}),
            errors={"name": "required_name"} if user_input is not None else {},
        )


class BatteryPoolOptionsFlow(config_entries.OptionsFlow):
    def __init__(self):
        self._batteries = None
        self._selected = None
        self._settings = {}

    async def async_step_init(self, user_input=None):
        if self._batteries is None:
            self._batteries = deepcopy(self.config_entry.options.get(CONF_BATTERIES, []))
        menu = ["add_battery", "control_settings"]
        if self._batteries:
            menu.extend(["edit_battery", "remove_battery"])
        return self.async_show_menu(step_id="init", menu_options=menu)

    def _save(self):
        return self.async_create_entry(
            title="", data={**self.config_entry.options, CONF_BATTERIES: self._batteries, **self._settings}
        )

    async def async_step_add_battery(self, user_input=None):
        return await self._battery_form("add_battery", user_input)

    async def _battery_form(self, step_id, user_input):
        existing = next((b for b in self._batteries if b["id"] == self._selected), {})
        errors = {}
        if user_input is not None:
            data = dict(user_input)
            errors.update(validate_numbers(data, BATTERY_NUMBER_BOUNDS))
            data["name"] = data["name"].strip()
            if not data["name"]:
                errors["name"] = "required_name"
            elif any(b["name"].casefold() == data["name"].casefold() and b["id"] != self._selected for b in self._batteries):
                errors["name"] = "duplicate_name"
            if not {"min_soc", "max_soc"}.intersection(errors) and data["min_soc"] >= data["max_soc"]:
                errors["min_soc"] = "invalid_soc_range"
            if data["soc_entity"] == data["power_entity"]:
                errors["power_entity"] = "distinct_sensors"
            for field in ("soc_entity", "power_entity", "command_entity"):
                if self.hass.states.get(data[field]) is None:
                    errors[field] = "entity_missing"
            if any(b["command_entity"] == data["command_entity"] and b["id"] != self._selected for b in self._batteries):
                errors["command_entity"] = "duplicate_command"
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.entry_id != self.config_entry.entry_id and any(b["command_entity"] == data["command_entity"] for b in entry.options.get(CONF_BATTERIES, [])):
                    errors["command_entity"] = "duplicate_command"
            if not errors:
                data["id"] = existing.get("id", uuid4().hex)
                data["command_service"] = data["command_entity"].split(".")[0] + ".set_value"
                if existing:
                    self._batteries[self._batteries.index(existing)] = data
                else:
                    self._batteries.append(data)
                return self._save()
        return self.async_show_form(step_id=step_id, data_schema=battery_schema(data if user_input is not None else existing), errors=errors)

    def _selection_form(self, step_id):
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema({
            vol.Required("battery"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[{"value": b["id"], "label": b["name"]} for b in self._batteries],
                mode=selector.SelectSelectorMode.DROPDOWN,
            ))
        }))

    async def async_step_edit_battery(self, user_input=None):
        if user_input is None:
            return self._selection_form("edit_battery")
        self._selected = user_input["battery"]
        return await self.async_step_battery()

    async def async_step_battery(self, user_input=None):
        return await self._battery_form("battery", user_input)

    async def async_step_remove_battery(self, user_input=None):
        if user_input is None:
            return self._selection_form("remove_battery")
        self._selected = user_input["battery"]
        return await self.async_step_confirm_remove()

    async def async_step_confirm_remove(self, user_input=None):
        battery = next(b for b in self._batteries if b["id"] == self._selected)
        if user_input is not None:
            self._batteries.remove(battery)
            return self._save()
        return self.async_show_form(step_id="confirm_remove", data_schema=vol.Schema({}), description_placeholders={"name": battery["name"]})

    async def async_step_control_settings(self, user_input=None):
        values = dict(self.config_entry.options)
        errors = {}
        if user_input is not None:
            user_input = dict(user_input)
            errors.update(validate_numbers(user_input, {"grid_target_w": (0, 10000)}))
            values.update(user_input)
            if values["control_mode"] == "autonomous":
                entity = self.hass.states.get(values.get("grid_power_entity", ""))
                if entity is None or entity.attributes.get("unit_of_measurement") != "W":
                    errors["grid_power_entity"] = "grid_required"
                if not values.get("grid_confirmed"):
                    errors["grid_confirmed"] = "grid_confirmation_required"
            if values["live_control"] and (not self._batteries or not all(b.get("adapter_confirmed", False) for b in self._batteries)):
                errors["live_control"] = "adapters_required"
            if not errors:
                self._settings = {**user_input, "grid_power_entity": user_input.get("grid_power_entity")}
                return self._save()
        schema = {
            vol.Required("control_mode", default=values.get("control_mode", "external")): selector.SelectSelector(selector.SelectSelectorConfig(options=["external", "autonomous"], translation_key="control_mode")),
            vol.Required("live_control", default=values.get("live_control", False)): selector.BooleanSelector(),
            vol.Required("grid_confirmed", default=values.get("grid_confirmed", False)): selector.BooleanSelector(),
            vol.Required("grid_target_w", default=values.get("grid_target_w", 20)): number_selector(0, 10000, "W"),
        }
        marker = vol.Optional("grid_power_entity", default=values["grid_power_entity"]) if values.get("grid_power_entity") else vol.Optional("grid_power_entity")
        schema[marker] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        return self.async_show_form(step_id="control_settings", data_schema=vol.Schema(schema), errors=errors)
