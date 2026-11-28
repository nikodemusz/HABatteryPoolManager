# Virtual Battery Pool

Home Assistant integration for controlling AC-coupled batteries as one pool. Requires Home Assistant 2026.11 or newer; tested with 2026.11.2.

Version **0.6** adds direct **Charge / Discharge / Standby** operation, continuously applied manual power settings, a visible simulation switch, and configurable power ramping. The pool starts in Standby after every restart or reload.

## Direct operation

After assigning the batteries through Configure, open the pool device:

1. Check **Simulation (no device commands)**. **ON** calculates only; **OFF** allows real device commands. Turning simulation off validates the configured adapters and leaves the pool stopped. A simulation change reloads the entry, so start the desired operation afterwards.
2. Set **Charging power** and **Discharging power** in positive watts. Both values are independently remembered. Changing a value while in Standby does not start the batteries.
3. Select **Battery operation → Charge** or **Discharge**. This also switches on Pool control. The selected power is continuously requested until Standby, a protective limit, a fault, or a restart/reload. **No 60-second refresh is needed for direct manual operation.**
4. Select **Standby** to stop. The integration requests zero power and each battery's configured idle mode.

Selecting Charge or Discharge automatically enables the separate power controls. Changing the inactive direction's power only prepares its value; it does not reverse the batteries. Editing the active direction applies the new value. Set the active power to zero or choose Standby to stop.

The **Limited power setpoint** sensor is a read-only diagnostic of the bounded pool request, not a scheduled charging task or proof of physical output. **Measured AC power** is the observed combined battery power, normalized as positive discharge and negative charge.

## Operating choices

| Battery operation | Behavior |
| --- | --- |
| Standby | Zero requests and idle modes; Pool control off |
| Charge | Continuously request the saved charging power |
| Discharge | Continuously request the saved discharging power |
| Automatic (self-consumption) | Balance grid import/export using the configured grid sensor |
| External request (60 s) | Accept explicitly refreshed requests from an external controller |

Manual power values and the separate-controls preference are saved. **Active operation and requests are never restored after restart.** Simulation/live selection is the same `live_control` setting exposed in Configure. Pool control remains available for automation compatibility; direct Charge/Discharge selection starts the pool without requiring a separate enable action.

## Power response and protection

The default software ramp is now **0 W/min = direct**: request the configured power immediately, bounded by battery and device limits. To retain a gradual increase, set **Power increase (W/min; 0 = direct)** in Configure; for example, 300 W/min reproduces the previous ramp. Stops and reductions are immediate.

| Setting | Default |
| --- | --- |
| Simulation | On; explicit live selection required |
| Initial/reloaded operation | Standby |
| Manual charge / discharge power | 300 W each |
| SOC reserve / ceiling | 20 % / 80 % per battery |
| Battery charge / discharge caps | 300 W each; respect actual device/BMS limits |
| Software ramp | 0 W/min, direct within caps |
| Direction reversal pause | 30 seconds at zero |
| Power deadband | 50 W |
| Maximum telemetry age | 60 seconds |
| Command heartbeat | 5 seconds |

SOC is weighted by usable capacity. Charging prioritizes the lowest eligible SOC; discharging prioritizes the highest. A change from charge to discharge still passes through zero and the 30-second reversal guard, including when Standby is selected between the two directions. All batteries require fresh valid SOC and AC power readings. BMS temperature, voltage, current and balancing protections remain with the device; software temperature control is not implemented.

## Battery adapters

Each battery needs a SOC sensor in **%**, an AC power sensor in **W**, and either:

- Two nonnegative current-power request entities (`number` or `input_number`), one for charge and one for discharge, where zero stops that direction; or
- One signed watt request entity for both directions, where zero stops.

For signed requests, command and measurement sign conventions must match the battery's selected convention. Separate device requests always use positive magnitudes. Batteries with different conventions and adapter types can coexist.

Optional **maximum charging power** and **maximum discharging power** entities are independent additional ceilings. The pool only reads these controls; it never writes them. Each direction uses the lower of its configured pool cap and linked device maximum. Zero disables that direction. Sensor-based dynamic limits require fresh reports; `number`/`input_number` maxima are persistent settings.

If a control is documented as watts but has no unit metadata, explicitly confirm **Controls without a displayed unit use watts**. A, kW and % are not automatically converted. An `input_number` or `input_select` requires a real device bridge; storing a helper value does not operate hardware.

### Direction modes

If the device additionally requires a direction mode, choose its `select`/`input_select` entity, then map its actual charge, discharge and idle options. For a device exposing `charge`, `discharge`, `standby`, assign those accordingly. A general user operating mode such as `manual`/`anti_feed`/`trade_mode` is a different prerequisite.

Before changing the direction mode, the integration zeros the requests. It waits up to 5 seconds for service completion and matching HA state before sending nonzero power. The inactive request receives zero before any active request. If step-size rounding yields zero, the device stays in idle. Stopping also attempts idle even if one zero request failed.

Remote-control prerequisites must already be enabled. A **device-side command watchdog** must stop power when commands cease (typically 15–30 seconds, allowing for device communication). The integration refreshes every 5 seconds; it does not configure vendor unlocks or the watchdog. Do not run another controller against the same endpoints. Service completion and HA state confirmation are not proof of physical device action; inspect measured power when commissioning.

## Automatic self-consumption

Configure a whole-home grid sensor in W, positive for import and negative for export, including all pool batteries behind that meter. Confirm the convention, then select **Automatic**. The pool requests:

`grid power + measured battery discharge − desired residual grid import`

The residual import default is 20 W. Import results in discharge, export in charging, subject to all limits. Invalid/stale grid data requests zero. Manual powers remain available for preparation but do not change active automatic operation. Select Charge/Discharge to override with manual operation or Standby to stop.

## Advanced external requests

For an external automation/controller, select **External request (60 s)**. With **Separate charge/discharge controls** off, write the existing signed pool number: positive discharges, negative charges. Refresh more frequently than 60 seconds, even if unchanged; expiry stops dispatch. The internal `set_direction_target` API retains the same expiry for compatibility.

The two normal UI power entities now store the continuous manual settings; editing them does not itself start a new direction. Use Battery operation to start. This avoids the former behavior where a UI setting silently expired after one minute.

Changing the interface clears an active external/manual request and stops it; an already running automatic grid controller continues. Inactive signed controls reject writes. Interface changes do not reload the pool.

## Installation and editing

Install via HACS as a custom integration repository, or copy `custom_components/virtual_battery_pool`, restart Home Assistant, and add Virtual Battery Pool. Configure batteries using their names, capacities, measurement entities, device request entities, limits and optional direction mode. German and English help explains the fields. Existing configured caps and SOC limits are preserved.

The editor permits temporary overlapping, incomplete or unavailable mappings so existing batteries can be migrated one at a time. Basic field formats and finite numeric ranges remain enforced. Runtime checks still reject unsafe controls and modes before real device commands. Every options save reloads the pool into Standby.

## Diagnostics and failures

Inspect Control status, `input_error`, `fault`, simulation, operation, and the per-battery plan. Valid SOC/power remains visible when a device limit is invalid; dispatch stays at zero and the status reports invalid limits.

A failed write or mode confirmation latches fault, attempts zero for every adapter and its idle mode, and blocks nonzero requests. Stops are retried while running, including after switching off. Fix the cause and explicitly start again. Simulation cannot be switched on to hide a failed live stop; the old live setting remains until safe release succeeds.

Normal stop, unload and shutdown request zero and idle. Abrupt loss of power/network/process can prevent software stop commands; the required device watchdog covers that case. A reboot never resumes a previous manual operation.

## Development

Use Python 3.14 and `python -m pip install -r requirements-dev.txt`, then:

```
python -m pytest -q -o asyncio_default_fixture_loop_scope=function
```

Tests exercise planner bounds, immediate/limited ramps, telemetry faults, migrations, HTTP form serialization, signed and direct power controls, persistent preferences, mode sequencing, live/simulation transitions, SOC limits, expiry of external requests, and actual entity services/reloads in an isolated HA instance. Device services in tests are simulated; they do not certify a real adapter.

## References and license

[Omnibattery](https://github.com/ffunes/Omnibattery) and [marstek_venus_ha](https://github.com/diegoschlauri/marstek_venus_ha) were consulted for feature-level ideas only; no source code was copied. GPL-3.0.
