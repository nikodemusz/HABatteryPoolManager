# Virtual Battery Pool

Manufacturer-independent Home Assistant HACS integration for coordinating AC-coupled batteries through explicitly configured Home Assistant entities and services. No vendor protocol drivers are included.

## Safety model

The controller is a planner, not a battery adapter. An adapter must expose SOC, measured AC power, and a writable command entity with an explicitly selected Home Assistant service. All participating batteries must have fresh, valid SOC readings before a nonzero command is planned. SOC limits, per-battery power caps, a deadband, rate limit, command hysteresis, and a direction reversal guard constrain dispatch. In a fail-safe condition the plan is zero power.

The weighted virtual SOC is calculated from configured usable capacities. Dispatch prioritizes the lowest SOC for charging and the highest SOC for discharging, so batteries are not intentionally commanded in opposing directions. External target power can be supplied to the planner; autonomous grid control interprets positive grid power as import and negative as export. A Shelly Pro 3EM is configured as a standard Home Assistant sensor entity.

## Current status

This repository contains an initial planner MVP and a minimal Home Assistant integration shell. It does not yet provide a UI battery-adapter editor, autonomous coordinator, entity platform, diagnostics panel, or live service-call executor. Install and review in simulation first; do not connect live write services until adapter semantics and safe release behavior are configured and tested on the actual battery.

## References

- [Omnibattery](https://github.com/ffunes/Omnibattery) describes multi-battery coordination and is licensed GPL-3.0.
- [marstek_venus_ha](https://github.com/diegoschlauri/marstek_venus_ha) describes entity-based battery adapters and is licensed GPL-3.0.

These repositories were consulted for feature-level ideas only. No source code was copied. This project uses an original implementation and MIT license.

## Development

Copy `custom_components/virtual_battery_pool` to Home Assistant's `custom_components` directory, restart, and add the integration. The current config flow only creates a placeholder entry; controller configuration is currently a Python API. Python planner tests are in `tests/`.
