# cbpi4-boilpower

A boil kettle logic for CraftBeerPi 4. Full power until the wort boils, then a
fixed element duty the brewer sets from the dashboard.

> ## ⚠️ Read before you run this
>
> This software switches mains-voltage heating elements drawing several
> kilowatts. It can boil a vessel dry, scald, start a fire, or destroy an
> element.
>
> **It is offered with no warranty of any kind, and is not certified for any
> purpose.** You are responsible for your own rig.
>
> - **Hardware protection is mandatory, not optional.** This logic has *no*
>   over-temperature cut-out and no dry-fire detection, by design — see
>   *What it deliberately does not do*. A thermal cutout on the element, an
>   in-line thermostat and a GFCI/RCD are what keep a fault from becoming a
>   fire. Software is not a safety device.
> - **Do not run a boil unattended.**
> - **Test with water before you brew with it**, at the power levels you intend
>   to use.
> - Verify your own wiring, contactor or SSR ratings, and element ratings.
>
> If any of that is not true of your setup, do not use this.

## Why

A boil does not need a PID. Once wort is boiling its temperature stops being a
control variable — it sits at the boiling point whatever you do — and the only
thing left to choose is how hard it boils.

```
below Boil_Threshold   ->  Max_Output, get there
at or above it         ->  Boil_Power, hold that duty
```

## What it deliberately does not do

This is a dumb power controller. It has no temperature policy beyond the single
threshold comparison, and that is a decision rather than an omission.

It briefly had a maximum-temperature cut and a rate-of-rise dry-fire watch.
Both were removed, because **a dry kettle's probe sits in air**. Air couples
badly to a thermowell, so when a vessel boils dry the reading barely moves —
the temperature does not run away, it goes quiet. Rate-of-rise dry-fire
detection therefore has no true positives for the hazard it is named after, and
only false ones. A false cut mid-boil ruins a batch.

So the plugin will:

- hold the configured duty at any temperature above the threshold, however high
- hold full power indefinitely below it, even if the wort never climbs
- never stop itself, and never raise a temperature alarm

A kettle that will not climb means a failed element, a tripped breaker, or a
probe out of the wort. The plugin cannot tell those apart, and each wants a
different response from a person standing at the rig.

**Over-temperature protection belongs in hardware** — a thermal cutout on the
element, an in-line thermostat, or a GFCI. Do not rely on this plugin, or any
other software in the loop, to de-energize a runaway element.

## Boil latch

Once the wort reaches the threshold the boil is latched, and full power does
not return until it falls **2 °C** below it.

Without that, a vessel held at the boil sits within a fraction of a degree of
the threshold and crosses it repeatedly, so every control decision flips
between `Max_Output` and `Boil_Power`. Observed on a rig as 100/10/100/10 at
two-second intervals — on an SSR, the element slamming between full and idle
and a boil that visibly surges. PIDBoil documents the same defect and latches
for the same reason.

## What it needs outside this repository

The plugin runs on stock CraftBeerPi 4. The **dashboard slider does not** — that
lives in the interface repository, and the two are wired together by three
strings that nothing in either repository checks on its own.

| What | This plugin | `craftbeerpi4-ui` |
|---|---|---|
| Logic name | `cbpi.plugin.register("BoilPower", …)` | `isBoilPowerKettle()` matches `boilpower` |
| Action name | `async def set_boil_power` | `kettleapi.action(id, "set_boil_power", …)` |
| Parameter | `Property.Number(label="Power")` | `{ Power: power }` |

Rename any one of them on either side and nothing fails loudly: the plugin still
loads, the kettle still boils, and the slider silently stops working — or worse,
the dialog closes as though the value were accepted.

`test_boilpower_ui_contract.py` in the testbench asserts all three agree, and
skips rather than fails when the interface repository is not checked out.

**Without the interface changes** you still get the logic; the only way to change
boil power mid-boil is the HTTP action:

```
POST /kettle/{id}/action   {"name": "set_boil_power", "parameter": {"Power": 60}}
```

### Core version

Nothing here requires a patched core, but two fixes in this fork make it behave
correctly rather than merely work:

- `ActorController.set_power` pushes a websocket update when the duty changes,
  so the dashboard shows the duty actually in force rather than the previous one.
- `BasicController.call_action` reports *"its logic is not running"* instead of
  blaming the `@action` decorator, and notifies the brewer — without it, setting
  a power before starting the logic is silently dropped with HTTP 204.

`cbpi.api.clock` is used when present and guarded by `try/ImportError`, so the
plugin loads on an unmodified CraftBeerPi.

## Adjusting the boil while it runs

Use the **Set boil power** action, from the dashboard.

Do **not** change the power by editing the kettle's properties mid-boil.
Editing any kettle property calls `BasicController.update()`, which calls
`stop()` — and because `KettleController` sets `autostart = False`, it never
restarts. Verified on a live rig: changing the power property during a boil
left the kettle `state=False` with the element de-energized, the new value
saved and doing nothing, and no warning shown.

The action dispatches straight to the running logic and never touches
`update()`, so the duty changes on the next control decision without the boil
being interrupted.

An adjustment made through the action applies to that boil only; it is not
written to disk. Change `Boil_Power` to move the default permanently.

## Timing

Deliberately identical to PIDBoil rather than invented. The loop runs on
`SampleTime` and hands the percentage to the actor the same way PIDBoil does
while approaching setpoint:

- `actor_on(power)` on the first demand
- `actor_set_power()` when the demand changes
- `actor_off()` at zero demand

Nothing here implements its own duty cycle. Whatever drives the element decides
how a percentage becomes on-time, so a PWM actor, an SSR and a contactor each
behave as they are configured to.

## Safety behaviour

- The element is switched **off** before the first temperature is read, rather
  than on at full power. A plain `GPIOActor.on()` drives the pin high and
  ignores the power argument, so "on at max output" is really "on at 100%".
- A reading that is missing, non-numeric or **stale** de-energizes the element
  until a trustworthy measurement returns. Staleness is judged against the
  sensor's own cadence, not a fixed 30 s, so a OneWire probe on a 60 s interval
  does not cycle the element every minute.
- `on_stop()` and the `finally` block both switch the heater off, so a
  cancelled task cannot leave the element energized.
- Duty is clamped to 0–100 and never raises on a malformed value.
- The element is driven from **what the actor reports**, not from what the loop
  last commanded. Switch the heater off from the dashboard mid-boil and the
  logic notices on the next pass and re-commands it, at the holding duty rather
  than full power. An earlier version tracked this in a local variable and
  wedged: the element stayed dark while the logic believed it was driving it,
  until the automation was stopped and restarted.
- If the actor cannot be read at all, the loop falls back to the duty it last
  commanded instead of treating silence as *off*. Treating it as off would
  re-issue `actor_on()` every pass — and `GPIOActor` implements its duty cycle
  by sleeping an on-phase then an off-phase, so re-commanding part-way through
  a window can restart it and hold the element on past the duty it was given.
  The failure is logged once when it starts, not once per pass.

## The dashboard setpoint

The kettle's existing temperature slider writes `target_temp`, and this logic
reads it as the boil threshold. That keeps the slider meaningful rather than
dead, and stops the setpoint widget reading zero.

Set it to your boiling point — around **210 °F / 99 °C**, adjusted for
altitude. A low setpoint means the kettle latches "boiling" almost immediately
and drops to the holding duty long before the wort is hot.

## Properties

| Property | Default | Meaning |
|---|---|---|
| `Boil_Threshold` | 99 °C / 210.2 °F | Where full power ends and fixed duty begins |
| `Boil_Power` | 85 | Element duty once boiling, 0–100 % |
| `Max_Output` | 100 | Element duty while heating up |
| `SampleTime` | 5 | Seconds between control decisions |

Temperature defaults are authored in Celsius and converted, so a bare number is
never interpreted as Fahrenheit on a Fahrenheit rig.

## Install

```bash
pip install .
cbpi plugins add cbpi4-boilpower
```

Then restart the server and set the kettle's **Logic** to `BoilPower`.

Plugins are discovered through `pkgutil.iter_modules()` plus
`importlib.metadata.version()`, so the package needs real distribution
metadata. Copying the folder into place looks like it works and then silently
fails to load — always `pip install`.

## Tests

`test_boilpower.py` in the testbench covers 44 checks, loading this file **by
path** so a run can never pass against a stale installed copy.

Two notes on what that suite is worth. It models the actor as real state that
the commands mutate, because an earlier harness stubbed the commands as pure
recorders and gave the logic a `cbpi` with no actor registry — so every read
raised, reported *off*, and the tests measured a world that does not exist. And
four of its checks were found to be incapable of failing: they asserted things
guaranteed by the opening and closing de-energize that every run performs. Both
were caught by running the plugin on a rig, not by the tests.

Nothing here substitutes for watching the first boil.
