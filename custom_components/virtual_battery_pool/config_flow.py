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


def battery_schema(values=None):
    values = values or {}
    fields = {}
    definitions = {
        "name": ("", str),
        "capacity_kwh": (5.0, vol.All(finite_number, vol.Range(min=0.001))),
        "soc_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "power_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "command_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number"]))),
        "min_soc": (0.0, vol.All(finite_number, vol.Range(min=0, max=100))),
        "max_soc": (100.0, vol.All(finite_number, vol.Range(min=0, max=100))),
        "max_charge_w": (800.0, vol.All(finite_number, vol.Range(min=0))),
        "max_discharge_w": (800.0, vol.All(finite_number, vol.Range(min=0))),
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

    async def async_step_init(self, user_input=None):
        if self._batteries is None:
            self._batteries = deepcopy(self.config_entry.options.get(CONF_BATTERIES, []))
        menu = ["add_battery"]
        if self._batteries:
            menu.extend(["edit_battery", "remove_battery"])
        return self.async_show_menu(step_id="init", menu_options=menu)

    def _save(self):
        return self.async_create_entry(
            title="", data={**self.config_entry.options, CONF_BATTERIES: self._batteries}
        )

    async def async_step_add_battery(self, user_input=None):
        return await self._battery_form("add_battery", user_input)

    async def _battery_form(self, step_id, user_input):
        existing = next((b for b in self._batteries if b["id"] == self._selected), {})
        errors = {}
        if user_input is not None:
            data = dict(user_input)
            data["name"] = data["name"].strip()
            if not data["name"]:
                errors["name"] = "required_name"
            elif any(b["name"].casefold() == data["name"].casefold() and b["id"] != self._selected for b in self._batteries):
                errors["name"] = "duplicate_name"
            if data["min_soc"] >= data["max_soc"]:
                errors["min_soc"] = "invalid_soc_range"
            if data["soc_entity"] == data["power_entity"]:
                errors["power_entity"] = "distinct_sensors"
            for field in ("soc_entity", "power_entity", "command_entity"):
                if self.hass.states.get(data[field]) is None:
                    errors[field] = "entity_missing"
            if not errors:
                data["id"] = existing.get("id", uuid4().hex)
                data["command_service"] = data["command_entity"].split(".")[0] + ".set_value"
                if existing:
                    self._batteries[self._batteries.index(existing)] = data
                else:
                    self._batteries.append(data)
                return self._save()
        return self.async_show_form(step_id=step_id, data_schema=battery_schema(user_input or existing), errors=errors)

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
