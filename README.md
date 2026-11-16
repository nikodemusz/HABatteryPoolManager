# Virtual Battery Pool

Manufacturer-independent Home Assistant HACS integration for coordinating AC-coupled batteries through explicitly configured Home Assistant entities and services. No vendor protocol drivers are included.

## Safety model

The controller only distributes an externally supplied pool power request; it does not derive its own grid target. An adapter must expose SOC, measured AC power, and a writable command entity with an explicitly selected Home Assistant service. All participating batteries must have fresh, valid SOC readings before a nonzero command is planned. Configurable SOC limits and power caps apply per battery; the default SOC interval is 0–100%. A deadband, aggregate rate limit, command hysteresis, and direction reversal guard constrain dispatch. In a fail-safe condition the plan is zero power.

The weighted virtual SOC is calculated from configured capacities. Dispatch prioritizes the lowest SOC for charging and the highest SOC for discharging, so batteries are not intentionally commanded in opposing directions. Temperature measurements are intended for later manual comparison with SOC and charging power over time; they do not currently alter control decisions.

## Current status

This repository contains an initial planner MVP and a minimal Home Assistant integration shell. It does not yet provide a UI battery-adapter editor, autonomous coordinator, entity platform, diagnostics panel, or live service-call executor. Install and review in simulation first; do not connect live write services until adapter semantics and safe release behavior are configured and tested on the actual battery.

## References

- [Omnibattery](https://github.com/ffunes/Omnibattery) describes multi-battery coordination and is licensed GPL-3.0.
- [marstek_venus_ha](https://github.com/diegoschlauri/marstek_venus_ha) describes entity-based battery adapters and is licensed GPL-3.0.

These repositories were consulted for feature-level ideas only. No source code was copied. This repository retains the GPL-3.0 license from its initial commit.

## Development

Copy `custom_components/virtual_battery_pool` to Home Assistant's `custom_components` directory, restart, and add the integration. The current config flow only creates a placeholder entry; controller configuration is currently a Python API. Python planner tests are in `tests/`.
