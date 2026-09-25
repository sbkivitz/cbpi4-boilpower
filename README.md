# cbpi4-boilpower

A boil kettle logic for CraftBeerPi 4. Full power until the wort boils, then a
fixed element duty the brewer sets from the dashboard.

## Why

A boil does not need a PID. Once wort is boiling its temperature stops being a
control variable — it sits at the boiling point whatever you do — and the only
thing left to choose is how hard it boils.

```
below Boil_Threshold   ->  Max_Output, get there
at or above it         ->  Boil_Power, hold that duty
```

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
