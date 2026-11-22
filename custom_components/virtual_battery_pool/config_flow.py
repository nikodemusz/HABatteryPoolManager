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



def configured_command_entities(config):
    keys = ("charge_command_entity", "discharge_command_entity") if config.get("command_mode", "signed") == "separate" else ("command_entity",)
    return {config[key] for key in (*keys, "mode_entity") if config.get(key)}


def configured_limit_entities(config):
    return {config[key] for key in ("max_charge_entity", "max_discharge_entity") if config.get(key)}


def battery_schema(values=None):
    values = values or {}
    fields = {}
    mode = values.get("command_mode", "signed" if values.get("command_entity") else "separate")
    definitions = {
        "name": ("", str),
        "capacity_kwh": (5.0, number_selector(0.001, unit="kWh")),
        "soc_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "power_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))),
        "command_mode": (mode, selector.SelectSelector(selector.SelectSelectorConfig(options=["separate", "signed"], translation_key="command_mode"))),
        "command_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number"]))),
        "charge_command_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number"]))),
        "discharge_command_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number"]))),
        "max_charge_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number", "sensor"]))),
        "max_discharge_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["number", "input_number", "sensor"]))),
        "mode_entity": (None, selector.EntitySelector(selector.EntitySelectorConfig(domain=["select", "input_select"]))),
        "power_unit_w_confirmed": (False, selector.BooleanSelector()),
        "positive_discharge": (True, selector.BooleanSelector()),
        "adapter_confirmed": (False, selector.BooleanSelector()),
        "min_soc": (20.0, number_selector(0, 100, "%")),
        "max_soc": (80.0, number_selector(0, 100, "%")),
        "max_charge_w": (300.0, number_selector(1, unit="W")),
        "max_discharge_w": (300.0, number_selector(1, unit="W")),
    }
    for key, (default, validator) in definitions.items():
        default = values.get(key, default)
        optional = key in {"command_entity", "charge_command_entity", "discharge_command_entity", "max_charge_entity", "max_discharge_entity", "mode_entity"}
        if optional:
            # A suggested selection can be cleared; a backend default would silently restore it.
            marker = vol.Optional(key, description={"suggested_value": default}) if default is not None else vol.Optional(key)
        else:
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
        self._pending_battery = None
        self._mode_options = []

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
            data.setdefault("command_mode", "signed")
            command_fields = ("charge_command_entity", "discharge_command_entity") if data["command_mode"] == "separate" else ("command_entity",)
            for field in ("soc_entity", "power_entity", *command_fields, "max_charge_entity", "max_discharge_entity", "mode_entity"):
                if not data.get(field):
                    if field in command_fields:
                        errors[field] = "command_required"
                elif self.hass.states.get(data[field]) is None:
                    errors[field] = "entity_missing"
            if data.get("mode_entity"):
                state = self.hass.states.get(data["mode_entity"])
                options = state.attributes.get("options", []) if state else []
                if (state is None or state.state in ("unknown", "unavailable")
                        or not isinstance(options, list) or not all(isinstance(o, str) for o in options)
                        or len(set(options)) < 3):
                    errors["mode_entity"] = "mode_unavailable"
            commands = configured_command_entities(data)
            limits = configured_limit_entities(data)
            if len({data.get(k) for k in command_fields}) != len(command_fields) and all(data.get(k) for k in command_fields):
                errors[command_fields[-1]] = "duplicate_command"
            conflicts = commands & limits
            others = [b for b in self._batteries if b["id"] != self._selected]
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if entry.entry_id != self.config_entry.entry_id:
                    others.extend(entry.options.get(CONF_BATTERIES, []))
            for other in others:
                other_commands = configured_command_entities(other)
                other_limits = configured_limit_entities(other)
                conflicts |= commands & (other_commands | other_limits)
                conflicts |= limits & other_commands
            for field in (*command_fields, "max_charge_entity", "max_discharge_entity", "mode_entity"):
                if data.get(field) in conflicts:
                    errors[field] = "duplicate_command"
            if not errors:
                data["id"] = existing.get("id", uuid4().hex)
                if data["command_mode"] == "signed":
                    data["command_service"] = data["command_entity"].split(".")[0] + ".set_value"
                    data.pop("charge_command_entity", None)
                    data.pop("discharge_command_entity", None)
                else:
                    data.pop("command_entity", None)
                    data.pop("command_service", None)
                if data.get("mode_entity"):
                    self._pending_battery = data
                    self._mode_options = list(self.hass.states.get(data["mode_entity"]).attributes["options"])
                    if existing.get("mode_entity") == data["mode_entity"]:
                        for key in ("charge_mode", "discharge_mode", "idle_mode"):
                            if existing.get(key):
                                self._pending_battery[key] = existing[key]
                    return await self.async_step_battery_modes()
                for key in ("mode_entity", "charge_mode", "discharge_mode", "idle_mode"):
                    data.pop(key, None)
                return self._save_battery(data)
        return self.async_show_form(step_id=step_id, data_schema=battery_schema(data if user_input is not None else existing), errors=errors)

    def _save_battery(self, data):
        existing = next((b for b in self._batteries if b["id"] == data["id"]), None)
        if existing:
            self._batteries[self._batteries.index(existing)] = data
        else:
            self._batteries.append(data)
        return self._save()

    async def async_step_battery_modes(self, user_input=None):
        if self._pending_battery is None:
            return self.async_abort(reason="configuration_expired")
        keys = ("charge_mode", "discharge_mode", "idle_mode")
        entity = self._pending_battery["mode_entity"]
        state = self.hass.states.get(entity)
        options = state.attributes.get("options", []) if state else []
        errors = {}
        if (state is None or state.state in ("unknown", "unavailable")
                or not isinstance(options, list) or not all(isinstance(o, str) for o in options) or len(set(options)) < 3):
            errors["base"] = "mode_unavailable"
        else:
            self._mode_options = list(options)
        if user_input is not None:
            for key in keys:
                if user_input.get(key) not in self._mode_options:
                    errors[key] = "mode_option_invalid"
            if len({user_input.get(key) for key in keys}) != 3:
                errors["base"] = "distinct_modes"
            if not errors:
                return self._save_battery({**self._pending_battery, **{key: user_input[key] for key in keys}})
        schema = {}
        for key in keys:
            value = (user_input or self._pending_battery).get(key)
            marker = vol.Required(key, default=value) if value in self._mode_options else vol.Required(key)
            schema[marker] = selector.SelectSelector(selector.SelectSelectorConfig(options=self._mode_options, mode=selector.SelectSelectorMode.DROPDOWN))
        return self.async_show_form(step_id="battery_modes", data_schema=vol.Schema(schema), errors=errors,
                                    description_placeholders={"entity": entity})

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
