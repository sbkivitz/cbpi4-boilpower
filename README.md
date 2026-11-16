# cbpi4-boilpower

A boil kettle logic for CraftBeerPi 4. Full power until the wort boils, then a
fixed element duty the brewer sets from the dashboard. Give the kettle a target
below the boil, such as 170 °F for a hop stand or 150 °F for a mash in a bag,
and a PID holds it there instead.

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

The kettle's target says which job it has, as on any kettle:

```
no target, or one at the boil   ->  boil, as above
a target below the boil         ->  hold it there with a PID
```

"At the boil" means within 2 °C / 3.6 °F of `Boil_Threshold`, or above it, so
a Boil step's 210 °F or 212 °F means boil wherever the threshold sits.
`Boil_Threshold` is the one place the boil point is set; at altitude, set it to
where your wort boils.

## What it deliberately does not do

This is a dumb power controller. Beyond the threshold comparison, and the PID
that holds a target below the boil, it has no temperature policy, and that is a
decision rather than an omission.

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

Use the actions, from the dashboard: **Set boil power**, **Keep it at the
boil**, **Pause the heat** and **Boil at**.

Do **not** change the power by editing the kettle's properties mid-boil.
Editing any kettle property calls `BasicController.update()`, which calls
`stop()` — and because `KettleController` sets `autostart = False`, it never
restarts. Verified on a live rig: changing the power property during a boil
left the kettle `state=False` with the element de-energized, the new value
saved and doing nothing, and no warning shown.

The action dispatches straight to the running logic and never touches
`update()`, so the duty changes on the next control decision without the boil
being interrupted.

Each action keeps its setting with the kettle, so a page opened later shows
the value in force, and stopping and starting the kettle keeps it.

Each declares one parameter, named for the setting it keeps, and does nothing
when given the value already in force. That matters because the dashboard's
**Set Target Temp** dialog sends every action the logic declares, each filled
in with its value in force, every time Set is pressed. A pause and a resume as
two actions without settings would pause and resume the heat at every Set.

### Keep it at the boil

`Keep_Boiling`, Yes by default. **Yes** is the boil latch below: if the wort
falls 2 °C / 3.6 °F below the boil, full power brings it back. **No**: full
power until the wort first boils, then only `Boil_Power`, and the wort may fall
if that is too low. With a low boil power and Yes, the kettle cycles between
full power and the boil power; with No it simply runs the boil power. Set back
to Yes while the wort is well below the boil, full power returns at once.

### Pause the heat

For a boil-over. `Paused` Yes switches the element off at once and keeps it
off, while AUTO, the boil latch, `Boil_Power` and the recipe's timer carry on.
`Paused` No heats again, going straight to what AUTO would be doing by then.
The pause is kept with the kettle as `Paused` and `Paused_At` (brewing-clock
seconds), so every page shows it and counts it from the same moment.

After 2 brewing minutes paused, a warning, "Boil Kettle heat still paused", is
kept on every page until OK. It is given once a pause, and taken down when the
heat carries on or AUTO stops. MANUAL, ALL OFF and a restart end a pause.
Pausing is refused while AUTO is off.

### Usual boil power

`Usual_Boil_Power`, blank by default. When a recipe starts from its first
step, `Boil_Power` goes back to it, and a message says so: "Boil power back to
your usual 45% (was 35%): the recipe started." A power set later that day, in
the sparge or the hold before the boil, is kept for that boil. Stopping and
starting the first step, or resuming it after a restart, carries the brew on
and changes nothing. Needs a CraftBeerPi with step listeners (this fork, from
cbpi4 4.7.7.post30); on one without, the setting is not used.

### Boil at

`Boil_Threshold`, 190–212 °F or 88–100 °C, changed without stopping the boil:
for a probe that never quite reaches the boil point on a low-pressure day. A
kettle aiming at the boil keeps aiming at it: its target moves with the boil
point, so raising it never turns a boil into a hold. Blank goes back to
99 °C / 210.2 °F.

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

## The kettle's target

The kettle's existing temperature slider writes `target_temp`, as it does for
any kettle. Set it at or above the boil point to boil; set it below to hold.
When the logic starts with no target, it puts the boil point in as the target,
so the setpoint widget never reads zero.

A target that isn't a number, such as NaN or infinity, is ignored, with a
message: the kettle carries on with the target it had before, so a hold stays
a hold and a boil stays a boil. With no target before it, the kettle boils.

Until 0.0.3.post7 this logic read the target as the boil threshold, so a low
setpoint meant "my boil is here", and a hold was a separate setting. Now
`Boil_Threshold` says where the boil is, and the target says boil or hold, as
the HLT's PIDBoil has always treated a target below its own threshold: set the
boil kettle's target to 170 °F, like the HLT's, and it holds there.

## Holding below the boil: a mash in a bag, a whirlpool, a hop stand

Set the kettle's target below the boil, for example 170 °F for a hop stand or
150 °F for a mash in a bag, and it holds the wort there instead of boiling it.
A PID sets the element's power, up to `Max_Output`. There is nothing to switch
on: any target below the boil is held this way.

The PID works as PIDBoil's does, with two changes for a probe that reads a
minute or so behind the wort:

- **P**: 14% of the element for each °F below the target (25.2 per °C). More
  than about 7 °F short, for example a whirlpool that cooled too far, it heats
  at full power.
- **I** makes up what the kettle loses to the room. It builds up only within
  5 °F / 2.8 °C of the target, and not while the heat is paused, so a climb
  from cold doesn't carry the wort past the target.
- **D** eases off as the wort rises, judged by how fast the readings rose over
  the last minute, so a probe that lags the wort doesn't let the climb run on
  past the target.

The defaults were chosen on cbpi4-simvessel's model of two kettles: the
simulators' (40 litres, 5500 W) and one built from measurements of the real rig
(RIG-NOTES 27.6). From 65 °F the rig's model passes 150 °F by 0.8 °F, then
holds it within 0.1 °F; with a probe twice as slow, as a bag of grain might
make it, it passes it by 1.4 °F. A whirlpool chilled to 158 °F is back within
2 °F of 170 °F in about 5 minutes on the rig's model and 7 on the simulators',
passing 170 °F by no more than 0.75 °F. The real rig hasn't run it yet. A kettle
that behaves very differently may want its own `P`, `I` and `D`; the PID
AutoTune plugin's results fit them.

- The hold is the kettle's target, so it is kept like any target: set it
  while the kettle is off, switch the kettle on, and the kettle holds rather
  than boils. Stop and Start keep it too.
- A **Boil step** sets its own target as it starts heating, so a hold left
  from one brew's hop stand never reaches the next brew's boil. A hold set
  while a Boil step runs is honoured: you asked for it.
- A **restart** of CraftBeerPi clears it, power cut or not, by putting the
  target back to the boil point. A message then says which hold was cleared,
  and stays until you press OK, so a page you open after the restart shows it
  too.

CraftBeerPi's **WhirlpoolStep** uses this hold. If the wort cools too far
during the stand, it asks whether to heat back and hold; it then sets the
kettle's target to the whirlpool temperature and switches the kettle on. When
the stand ends it switches the kettle off and puts back the target it found
(cbpi4 4.7.7.post36), so the kettle switched on later boils.

History. Until 0.0.3.post2 a hold was kept only by the running logic, so a hold
set while the kettle was off was lost when it was switched on, and the kettle
boiled at full power. From 0.0.3.post3 a restart clears a hold. Until
0.0.3.post4 a hold never climbed: far below its target it heated at
`Hold_Power` too. Until 0.0.3.post6 it dropped to a flat `Hold_Power` 7.2 °F
short of the target, which only just beats a kettle's losses, so the last few
degrees crawled. The brewer, 4 Nov: "I don't think if I'm attempting to heat
back up from where I over-cooled, I should be forced to use the hold_power
value".
Until 0.0.3.post7 a hold was the `Hold_Target` setting, set with a **Hold a
temperature** action; a restart clears one left in a kettle's settings. That
action had to go: the Set Target Temp dialog sends every action with its value
in force, so a hold kept apart from the target would undo the target its own
slider had just set. Until 0.0.3.post8 a hold eased from `Max_Output` down to
a `Hold_Power` setting (30%) as the wort neared its target, switched off at
the target, and rested until the wort fell 3.6 °F below, so it sat up to 4 °F
under its target. `Hold_Power` is no longer used; a kettle that still has it
ignores it.

## Properties

| Property | Default | Meaning |
|---|---|---|
| `Boil_Threshold` | 99 °C / 210.2 °F | Where the kettle boils: full power below, fixed duty from it. A target within 2 °C / 3.6 °F of it, or above, means boil |
| `Boil_Power` | 85 | Element duty once boiling, 0–100 % |
| `Max_Output` | 100 | Element duty while heating up to the boil, and the most a hold's PID uses |
| `P` | blank: 25.2 per °C / 14 per °F | A hold's PID: % of power per degree below the target |
| `I` | blank: 0.054 per °C / 0.03 per °F | % of power per degree per second below the target, built up only within 2.8 °C / 5 °F of it |
| `D` | blank: 1620 per °C / 900 per °F | % of power taken off per degree per second the wort rises. 0 turns it off |
| `SampleTime` | 5 | Seconds between control decisions, up to 60. More than 60, or anything that isn't a number, is 5, with a message |
| `Keep_Boiling` | Yes | Yes: full power brings the wort back to the boil. No: once boiled, only `Boil_Power` |
| `Usual_Boil_Power` | blank | The boil power each recipe starts at; blank leaves `Boil_Power` alone |

Each property's Default is what the logic uses when it is left empty, in the
server's temperature unit. The logic tells CraftBeerPi these values
(`blank_defaults`), so the Hardware page can show them and offer to fill them
in. `Usual_Boil_Power` has none: empty, it leaves `Boil_Power` alone, which no
number can say.

The logic also keeps `Paused` and `Paused_At` in a kettle's settings while the
heat is paused. They are not settings to edit: a start, a stop and a restart
clear them.

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

`test_boilpower.py` in the testbench covers 86 checks, loading this file **by
path** so a run can never pass against a stale installed copy.
`test_boilpower_brewday_controls.py` covers keep it at the boil, the pause and
its warning, the usual boil power and boil at (64 checks), and
`test_boilpower_hold_kept.py` and `test_boilpower_whirlpool.py` the hold.
`test_boilpower_pid_hold.py` runs the hold's PID on the two modelled kettles,
heated by the power it commands, and `test_e2e_whirlpool_hold.py` runs a
whirlpool's hold on a full server. `test_blank_defaults.py` checks that each
value `blank_defaults` gives is what the logic does with that property empty,
in °C and in °F.

Two notes on what that suite is worth. It models the actor as real state that
the commands mutate, because an earlier harness stubbed the commands as pure
recorders and gave the logic a `cbpi` with no actor registry — so every read
raised, reported *off*, and the tests measured a world that does not exist. And
four of its checks were found to be incapable of failing: they asserted things
guaranteed by the opening and closing de-energize that every run performs. Both
were caught by running the plugin on a rig, not by the tests.

Nothing here substitutes for watching the first boil.
