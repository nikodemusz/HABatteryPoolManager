# Virtual Battery Pool

A Home Assistant integration for combining AC-coupled batteries into one controllable pool. Version 0.4.1 removes editor plausibility checks to allow staged migrations of existing batteries. Version 0.4 adds configurable charge/discharge/idle mode control alongside separate power requests, read-only device maxima, signed-watt adapters, simulation, and grid self-consumption control. Requires Home Assistant 2026.11 or newer; tested with 2026.11.2. Version 0.2.1 fixes setup forms failing to render by using native number selectors and validating finite values on submission.

## Supported battery adapters

Each battery must expose:

- A current SOC sensor in **%**, from 0 to 100.
- A measured AC power sensor in **W**.
- Either **two separate writable request entities** in W (one for current charging power and one for current discharging power, both nonnegative, with zero stopping that direction), or **one signed writable request entity** in W (positive/negative directions, zero to stop). Supported domains are `number` and `input_number`.
- If direction additionally depends on a device mode, a **select or input_select** with distinct charge, discharge and idle options. Map the actual option values explicitly in the editor.
- Optionally, **two independent maximum-power entities**: maximum charging power and maximum discharging power. These may be `number`, `input_number`, or `sensor`; the integration only reads them.

The editor normalizes the measured AC power convention: positive = discharge (default) or positive = charge. A combined signed request must use that same convention. Separate charge/discharge requests are always positive magnitudes and do not use a command sign. Different batteries may use different adapter types and measurement conventions. The pool entities always use **positive = discharge, negative = charge**.

The device must already have its general remote-control prerequisites enabled and have a **device-side command watchdog** that stops power when commands cease. The integration refreshes every 5 seconds; configure a watchdog appropriate to the device (typically 15–30 seconds, allowing for its communication interval). Verify the actual device behavior before confirming the adapter contract. A plain `input_number` needs a separate device bridge and watchdog; storing a helper value alone does not control hardware.

Unsigned maximum-power limiters alone cannot request charging or discharging. Direction mode selectors are supported through select.select_option or input_select.select_option. General operating-mode switches, remote-control unlocks, current limits in amperes and vendor APIs still need separate preparation or an adapter. No vendor protocol drivers are included. All pool batteries must be connected behind the same grid meter when using self-consumption mode. Do not let another controller write to the same battery simultaneously.

## Installation and first use

1. Install through HACS as a custom integration repository, or copy `custom_components/virtual_battery_pool` into Home Assistant's `custom_components` directory, then restart Home Assistant.
2. Add **Virtual Battery Pool** in Settings → Devices & services and give the pool a name.
3. Open **Configure → Add battery system**. Choose the individual battery's measurement entities, usable capacity, SOC limits, power limits, and measurement sign convention. Select **Separate charge/discharge requests** (default for new batteries) or **Combined signed request**. English and German help explains every field.
   - Separate mode: assign **Requested charging power** and **Requested discharging power**, e.g. the device's “Set charging power” and “Set discharging power” controls.
   - Optionally assign **Device maximum charging power** and **Device maximum discharging power**, e.g. “Maximum charging power” / “Maximum discharging power”. These are **not** current requests and are **never written**, including when stopping.
   - Keep the additional pool software limits at 300 W initially. Each direction uses the lower of its software limit and linked device maximum. A device maximum of zero disables only that direction.
   - If these controls are documented as watts but Home Assistant supplies no unit, explicitly confirm **Controls without a displayed unit use watts**. This accepts missing units only on the selected request/maximum entities; A, kW and % are rejected. SOC and AC measurement sensors still need % and W respectively.
   - If charging/discharging also requires a mode, select **Charge / discharge / idle mode entity**, e.g. the device's **Forced mode**. The next form suggests that entity's actual options and also accepts typed values while it is offline. Map charge → `charge`, discharge → `discharge`, idle → `standby` for an adapter exposing those values. There are no guessed mappings for other devices. A general **User mode** (`manual` / `anti_feed` / `trade_mode`) is a different prerequisite, not a substitute for direction control. Choose a real idle state, not an autonomous mode that can independently transfer power.
4. Repeat for other batteries. Editor saves allow intermediate overlapping mappings, including across pools. Live operation still rejects conflicting power and direction-mode assignments.
5. Open **Control mode and live output**. Leave live output **off** for simulation. Choose external request or grid self-consumption.
6. Open the pool device and turn on **Pool control**. Inspect **Control status**, **Pool state of charge**, **Measured AC power**, and **Planned AC power**. Status attributes show the plan per battery, selected mode, simulation flag, any input validation error (`input_error`), and any latched fault. Simulation sends **no device commands**, including stop commands and mode selections.
7. After verifying each adapter's sign, zero behavior, remote mode, and watchdog on the actual device, confirm its adapter contract in the battery editor and enable **live output** in the pool settings. This reloads the integration and leaves the pool **off**. Turn on its switch to start. Activation rejects missing services, unavailable command entities, incompatible units/ranges, and live collisions with another pool.

**Pool control is off after every restart or options change.** A previous power request is never restored. Existing combined signed mappings remain in signed mode; new batteries default to separate requests. Existing adapters without a mapped mode continue to leave device modes untouched; edit them to add mode control where required. Existing battery mappings remain intact; existing SOC/power limits are preserved rather than silently overwritten. Existing adapters need to be edited and confirmed before live output is possible. Review older 0–100 % or 800 W settings if you want the new conservative defaults.

## Editing existing systems during a migration

The editor saves each battery independently. It no longer checks cross-battery or cross-pool assignments, power/limit role overlap, duplicate names, relative SOC limits, sensor separation, entity availability, or mode-option consistency when saving. Missing optional command mappings and configuration-only intermediate states can be saved. Mode options can be typed even when their entity is unavailable. Basic field types, required names/measurement fields, and finite numeric ranges remain enforced.

This allows two existing battery systems to be moved to the new model one at a time without a temporary overlap blocking the first save. IDs and the other battery's saved settings are preserved. Control settings can also be saved before all adapters and the grid input are complete.

Runtime protections are unchanged: conflicting controls, missing entities, unconfirmed adapters, invalid modes, or invalid inputs cannot produce a nonzero live plan. Fix intermediate mappings before activating live operation. Every options save reloads the pool and leaves its switch off.

## Operating modes

### External request

Set the pool's new **Power request (+ discharge / − charge)** number entity using `number.set_value`. For example, `500` requests up to 500 W of pool discharge; `-300` requests up to 300 W of charging. The planner may allocate less because of SOC, ramp, or power limits.

Refresh the request **more frequently than 60 seconds**, including if its value is unchanged. After 60 seconds without a fresh service call the request expires and the planner requests zero at its next 5-second tick (plus device communication time). A 15-second automation interval is suitable. A zero request stops immediately; switching **Pool control** off also clears the request. The number entity retains the last requested value after expiry; **Control status** reports that a fresh request is needed.

### Grid self-consumption

Select a whole-home grid connection power sensor in **W**, **positive for import and negative for export**, and confirm this convention. It must include all the batteries and the rest of the home. Do not choose PV generation or an individual battery's power.

The planner requests `grid power + measured battery discharge − desired residual grid import`. The default residual import is **20 W**, avoiding an exact zero target. Surplus export results in charging; excess import results in discharge, subject to all limits. The external request number is ignored in this mode. Missing or stale grid data requests zero power.

## Default protection settings

| Setting | Default | Behavior |
| --- | --- | --- |
| Live device output | Off | Simulation until explicitly configured |
| Pool switch | Off | No automatic restart after a reboot/reload |
| SOC reserve / ceiling | 20 % / 80 % | No discharge at/below reserve; no charge at/above ceiling |
| Charge / discharge limit | 300 W each per battery | Must also fit device capabilities and BMS limits |
| Aggregate power increase | 300 W/min | Ramp increases, including first start; stops/reductions are immediate |
| Direction reversal guard | 30 s at zero | Stops before changing charging direction |
| Power deadband | 50 W | Small requests result in zero |
| Maximum data age | 60 s | SOC, measured power and grid data must be valid and freshly reported |
| External request lifetime | 60 s | Requires continuing requests |
| Command heartbeat | 5 s | Device-side watchdog must stop on lost communication |

SOC is weighted by usable capacity. Charging favors the lowest eligible SOC; discharging favors the highest. Commands never intentionally charge one participating battery while discharging another. All participating batteries need valid data, including those at their limits. Linked maximum-power entities must hold valid, nonnegative values. Dynamic limits supplied by sensors must be reported within 60 seconds; number/input_number maxima are persistent settings and do not expire simply because they have not changed. Repeated identical sensor reports count as fresh reports; a sensor integration must not disguise stale device data as fresh readings.

These defaults reduce routine depth of discharge and power stress; they cannot suit every chemistry or replace the manufacturer's BMS. Temperature, cell voltage, current, and balancing protections remain with the battery/BMS. Temperature-based software control is not implemented.

## Failure and stop behavior

- Invalid/unknown/unavailable/stale sensor data or expired requests produce a zero plan. Normal operation can resume once fresh valid input returns.
- Before each live write, the integration checks service availability, command units, signed limits, and step alignment. Values are rounded toward zero so device step sizes do not exceed the planned magnitude.
- Reductions and reversals are sent before increases. In separate mode every inactive direction receives an explicit zero before any nonzero request is sent, even if its reported value is already zero. Stopping attempts zero on **both** request entities. Calls are serialized and individually limited to 5 seconds.
- When a mapped device mode must change, its power requests are zeroed first. The integration selects the requested direction/idle mode and waits up to 5 seconds for service completion and matching Home Assistant entity readback before sending any nonzero request. Mode selection uses the **rounded actual request**: if the step size rounds a request to zero, the device stays in idle. During the reversal guard the device stays in the configured idle mode.
- Stopping also attempts the configured idle mode after zeroing the requests, including if a zero write failed. An idle-mode failure remains latched and is retried. No prior automatic/user mode is restored; the configured idle option is the explicit release behavior.
- A write failure latches **fault**, attempts zero for **every** confirmed adapter even if one fails, and blocks further nonzero commands. Zero retries continue while running, including after switching off if an earlier stop failed. Fix the cause, switch off, and reactivate to reset the fault.
- Switching off, reloading/unloading, and a normal Home Assistant shutdown attempt zero on all confirmed adapters that were active. A failed stop remains visible in the fault attribute.
- Service success is not proof of physical power reaching zero. A power/network failure, a hung device, or abrupt process termination can prevent software stop commands. The required **device-side watchdog** handles this case. The integration does not configure the watchdog or general vendor operating/remote-control prerequisites for you. Mode readback confirms Home Assistant state, not physical battery behavior.

## Development and validation

Use Python 3.14 and `python -m pip install -r requirements-dev.txt`, then run:

```
python -m pytest -q -o asyncio_default_fixture_loop_scope=function
```

Tests cover planner bounds, weighted SOC, ramp growth, reversal timing, unsafe readings, persistent options, two-battery staged migration with overlapping mappings, HTTP form serialization, simulation, live signed service calls, stale/expired input, partial failures, stop behavior, separate endpoint interlocking, independent read-only device limits, mapped mode sequencing/readback/failure handling, missing-unit confirmation, cross-pool collisions, and the actual integration/entity setup and unload inside an isolated Home Assistant instance. They use fake device services; they do not certify any real battery adapter.

## References and license

[Omnibattery](https://github.com/ffunes/Omnibattery) and [marstek_venus_ha](https://github.com/diegoschlauri/marstek_venus_ha) were consulted for feature-level ideas only; no source code was copied. This project retains its GPL-3.0 license.
