"""Full power to the boil, then a fixed duty the brewer sets from the dashboard.

A boil does not need a PID. Once wort is boiling its temperature stops being a
control variable - it sits at the boiling point whatever you do - and the only
thing left to choose is how hard it boils. PIDBoil models this as a special
case at the end of a PID; this plugin is that case on its own:

    below the threshold   ->  Max_Output, get there
    at or above it        ->  Boil_Power, hold that duty

The duty is adjustable while the boil runs, through the Set Boil Power action,
because the alternative is unusable. Editing a kettle property calls
BasicController.update(), which calls stop() and - since KettleController sets
autostart False - never restarts. Verified on a live rig: changing the power
property mid-boil left state=False with the element de-energized, the new value
saved and doing nothing, and no warning. An action dispatches straight to the
running logic and never touches update(), so vigour changes without the boil
being interrupted.

Timing deliberately matches PIDBoil rather than inventing its own. The loop
runs on SampleTime and hands the percentage to the actor exactly as PIDBoil
does while approaching setpoint: actor_on(power) on the first demand,
actor_set_power() on a change, actor_off() at zero. Nothing here implements its
own duty cycle - whatever drives the element decides how a percentage becomes
on-time, so a PWM actor, an SSR and a contactor each behave as configured.
"""

import asyncio
import logging
import time

from cbpi.api import *
from cbpi.api.dataclasses import NotificationType

try:
    # Present on forks with a pluggable clock, which lets a whole brew day be
    # rehearsed faster than real time. Optional on purpose: this plugin has to
    # keep working on an unmodified CraftBeerPi, where an unconditional import
    # would be an ImportError during plugin discovery rather than a missing
    # feature.
    from cbpi.api import clock
except ImportError:  # pragma: no cover - depends on the host installation
    class _RealClock:
        @staticmethod
        def now():
            return time.time()

        @staticmethod
        def monotonic():
            return time.monotonic()

        @staticmethod
        async def sleep(seconds):
            await asyncio.sleep(max(0.0, seconds))

        @staticmethod
        async def sleep_until(deadline):
            await asyncio.sleep(max(0.0, deadline - time.time()))

    clock = _RealClock()

try:
    # Same reasoning as the clock import: only forks carry this, and a plugin
    # that cannot load is worse than one without dry-fire protection.
    from cbpi.api.dryfire import DryFireWatch
except ImportError:  # pragma: no cover - depends on the host installation
    DryFireWatch = None


@parameters([
    Property.Number(
        label="Boil_Threshold", configurable=True,
        description="Temperature at which the boil starts and the element drops "
                    "to Boil_Power. Default 99 C / 210.2 F."),
    Property.Number(
        label="Boil_Power", configurable=True, default_value=85,
        description="Element duty once boiling, 0-100%. Adjustable from the "
                    "dashboard while the boil runs."),
    Property.Number(
        label="Max_Output", configurable=True, default_value=100,
        description="Element duty while heating up to the threshold."),
    Property.Select(
        label="SampleTime", options=[2, 5],
        description="Seconds between control decisions. Matches PIDBoil."),
    Property.Number(
        label="Volume_Litres", configurable=True, default_value=0,
        description="Litres in the vessel. With Element_Watts, enables dry-fire "
                    "protection. 0 disables."),
    Property.Number(
        label="Element_Watts", configurable=True, default_value=0,
        description="Element rating in watts. With Volume_Litres, enables "
                    "dry-fire protection. 0 disables."),
    Property.Number(
        label="Max_Safe_Temp", configurable=True, default_value=0,
        description="Cut the element if the vessel exceeds this. A boil kettle "
                    "above boiling has already lost its liquid. 0 uses "
                    "Boil_Threshold + 15 degrees."),
])
class BoilPower(CBPiKettleLogic):
    """Ramp at full power, then hold a chosen duty."""

    # Defaults are authored in Celsius and converted, because a bare number in
    # a property default is a Celsius value on a Fahrenheit rig - which is how
    # a 20 C ambient became 20 F elsewhere in this project.
    DEFAULT_THRESHOLD_C = 99.0
    DEFAULT_BOIL_POWER = 85
    DEFAULT_MAX_OUTPUT = 100
    DEFAULT_SAMPLE_TIME = 5

    # Fallback only. The sensor's own cadence is preferred - see _read_temp.
    MAX_SENSOR_AGE = 30

    # How far the wort must fall below the threshold before full power returns.
    #
    # Without this the loop chatters. A vessel held at the boil sits within a
    # fraction of a degree of the threshold and crosses it repeatedly, so every
    # control decision flips between Max_Output and Boil_Power - observed on
    # the rig as 100/10/100/10 at two second intervals, which on an SSR is the
    # element slamming between full and idle and a boil that visibly surges.
    #
    # PIDBoil documents the same defect and latches its boil for the same
    # reason. Authored in Celsius and converted, like every other temperature
    # default here.
    BOIL_EXIT_DROP_C = 2.0

    def _unit(self):
        return str(self.get_config_value("TEMP_UNIT", "C")).upper()

    def _default_threshold(self):
        if self._unit() == "C":
            return self.DEFAULT_THRESHOLD_C
        return self.DEFAULT_THRESHOLD_C * 9.0 / 5.0 + 32.0

    @staticmethod
    def _clamp_percent(value, default):
        """A duty percentage, bounded to 0-100. Never raises.

        Anything deciding how long a five kilowatt element stays on has to cope
        with whatever arrives - a blank field, text, a NaN - without throwing
        inside the control loop.
        """
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return int(default)
        if numeric != numeric:  # NaN
            return int(default)
        return int(max(0, min(100, numeric)))

    def _boil_power(self):
        """The duty held once boiling, read fresh on every pass.

        Read here rather than captured before the loop, so a change takes
        effect on the next control decision instead of the next restart.

        An override set through the action wins over the stored property. It is
        deliberately not written to disk: adjusting vigour for one batch should
        not silently rewrite the recipe, and a restart should come back to the
        configured value.
        """
        override = getattr(self, "_boil_power_override", None)
        if override is not None:
            return override
        return self._clamp_percent(
            self.props.get("Boil_Power", self.DEFAULT_BOIL_POWER),
            self.DEFAULT_BOIL_POWER,
        )

    def _threshold(self):
        """Where full power ends and the fixed duty begins, read fresh.

        Read on every pass, not once before the loop. Captured up front, a
        change to the kettle's setpoint did nothing until the logic was
        restarted - so the dashboard's temperature slider, which is labelled as
        the boil threshold, silently had no effect on a running boil. A control
        that appears to work and does not is worse than one that is absent.

        The kettle's setpoint wins when there is one, leaving Boil_Threshold as
        the default it falls back to.
        """
        configured = float(
            self.props.get("Boil_Threshold", None) or self._default_threshold()
        )
        try:
            published = float(self.get_kettle_target_temp(self.id) or 0)
            if published > 0:
                return published
        except (TypeError, ValueError, AttributeError):
            pass
        return configured

    def _read_temp(self, sensor_id):
        """Current temperature, or None if it cannot be trusted.

        get_sensor_value() returns None for a missing or failing sensor, so a
        bare .get("value") raises AttributeError - which a broad handler turns
        into one log line and a dead control loop for the rest of the brew.

        Staleness is judged against the sensor's own cadence, not a fixed
        number of seconds: a OneWire probe on its default 60s interval is
        legitimately 59s old, and cutting heat on that would cycle the element
        every minute of a brew day with nothing wrong.
        """
        try:
            state = self.get_sensor_value(sensor_id)
            value = float(state.get("value"))
        except (AttributeError, TypeError, ValueError):
            logging.warning(
                "BoilPower: ignoring sensor %s, reading is missing or non-numeric",
                sensor_id,
            )
            return None

        age = state.get("age")
        limit = state.get("max_age") or self.MAX_SENSOR_AGE
        if age is not None and age > limit:
            logging.warning(
                "BoilPower: ignoring sensor %s, last updated %.0fs ago (limit %.0fs)",
                sensor_id, age, limit,
            )
            return None
        return value

    @action(
        "Set boil power",
        [Property.Number(
            label="Power", configurable=True,
            description="Element duty while boiling, 0-100%. Takes effect on the "
                        "next control decision without interrupting the boil. Not "
                        "saved - change Boil_Power to set it permanently.")],
    )
    async def set_boil_power(self, Power=None, **kwargs):
        """Change boil vigour on a running boil, without cutting the element."""
        previous = self._boil_power()
        self._boil_power_override = self._clamp_percent(
            Power, self.DEFAULT_BOIL_POWER
        )

        # Persist it, so the interface and the element agree.
        #
        # This deliberately did not save, on the reasoning that adjusting
        # vigour for one batch should not rewrite the recipe. That was wrong in
        # practice: the dashboard slider seeds from Boil_Power, so it kept
        # opening at the stored 85 while the element was actually running at
        # 10, and the brewer had no way to see the value in force. A control
        # surface that disagrees with the hardware is worse than one that
        # remembers a number you can change back.
        #
        # save() only writes the file and pushes an update - unlike update(),
        # it does not stop the logic - so this is safe during a boil.
        try:
            kettle = self.get_kettle(self.id)
            if kettle is not None:
                kettle.props["Boil_Power"] = self._boil_power_override
                await self.cbpi.kettle.save()
        except Exception as e:  # noqa: BLE001 - never fail a control action
            logging.warning("BoilPower: could not persist Boil_Power: %s", e)

        logging.info(
            "BoilPower: duty set to %s%% (was %s%%)",
            self._boil_power_override, previous,
        )
        try:
            self.cbpi.notify(
                getattr(getattr(self, "kettle", None), "name", "Boil"),
                "Boil power now {}% (was {}%).".format(
                    self._boil_power_override, previous
                ),
                NotificationType.INFO,
            )
        except Exception:  # noqa: BLE001 - never fail a control action on a toast
            pass

    async def on_stop(self):
        # Guarded: run() may have raised before self.heater was assigned, and an
        # AttributeError here would mask whatever actually went wrong.
        heater = getattr(self, "heater", None)
        if heater is not None:
            await self.actor_off(heater)

    async def run(self):
        try:
            sample_time = int(self.props.get("SampleTime", self.DEFAULT_SAMPLE_TIME))
            # The threshold itself is read per pass by _threshold(), so moving
            # the setpoint acts on a running boil. Only the starting value is
            # needed here, to publish a setpoint if the kettle has none.
            threshold = self._threshold()
            max_output = self._clamp_percent(
                self.props.get("Max_Output", self.DEFAULT_MAX_OUTPUT),
                self.DEFAULT_MAX_OUTPUT,
            )
            # Preserve an override across a property edit, which constructs a
            # new instance; lose it on a genuine restart.
            self._boil_power_override = getattr(self, "_boil_power_override", None)

            # Dry-fire protection. Needs two facts no kettle carries - how much
            # liquid is in it and how big the element is - so it does nothing
            # until both are configured.
            #
            # This plugin shipped without it, and a simulated 40 L kettle left
            # boiling at 85% ran itself dry and climbed to 404 F with the
            # element still commanded on and nothing said to the brewer. On a
            # real rig that is a destroyed element at best.
            dry_watch = DryFireWatch() if DryFireWatch else None
            dry_litres = max(0.0, float(self.props.get("Volume_Litres", 0) or 0))
            dry_watts = max(0.0, float(self.props.get("Element_Watts", 0) or 0))
            degree_ratio = 1.0 if self._unit() == "C" else 1.8

            # A second, cruder guard that needs nothing configured.
            #
            # Dry-fire detection compares the rate of rise against what the
            # element could physically achieve, which is precise but silent
            # until someone fills in two properties. A boil kettle sitting well
            # above boiling has already lost its liquid whatever the rate was,
            # so this catches the same accident with no setup at all.
            configured_max_safe = float(self.props.get("Max_Safe_Temp", 0) or 0)
            if configured_max_safe <= 0:
                configured_max_safe = 0.0

            self.kettle = self.get_kettle(self.id)
            self.heater = self.kettle.heater
            sensor_id = self.kettle.sensor

            # Publish a setpoint if there is none, so the dashboard has
            # something to show. Without this the SV widget read 0 and a
            # running boil displayed "0 deg 85%", which tells the brewer
            # nothing and looks broken.
            try:
                if not (float(self.kettle.target_temp or 0) > 0):
                    self.kettle.target_temp = threshold
            except Exception:  # noqa: BLE001 - a cosmetic field, never fatal
                pass

            # Establish a known state without a pulse. A plain GPIOActor's on()
            # drives the pin high and ignores the power argument, so starting
            # with "on at max_output" is really "on at 100%" before a single
            # temperature has been read. Actor state also survives a restart, so
            # an element left on by a previous run would otherwise stay on while
            # this loop believed it was off.
            await self.actor_off(self.heater)
            heater_is_on = False
            heat_percent_old = 0
            announced_boil = False
            # Once boiling, stay boiling until the wort genuinely comes off it.
            boil_latched = False
            exit_drop = self.BOIL_EXIT_DROP_C * degree_ratio

            while self.running:
                current_temp = self._read_temp(sensor_id)
                # Fresh every pass, so moving the setpoint acts on a running
                # boil instead of waiting for a restart.
                threshold = self._threshold()
                max_safe = configured_max_safe or (threshold + (15.0 * degree_ratio))

                if current_temp is None:
                    # No trustworthy reading. Heating blind toward a boil is the
                    # one thing not to do, so the element comes off until a real
                    # measurement returns.
                    #
                    # Unconditional: commanding off something already off costs
                    # one redundant call, while not commanding off something
                    # that IS on is how an element stays live.
                    await self.actor_off(self.heater)
                    heater_is_on = False
                    heat_percent_old = 0
                    await clock.sleep(sample_time)
                    continue

                # A boil kettle sitting well above boiling has already lost its
                # liquid. This needs nothing configured, unlike the rate-based
                # dry-fire check below, so it catches the accident on a rig
                # where nobody filled in the volume and wattage.
                if current_temp >= max_safe:
                    await self.actor_off(self.heater)
                    heater_is_on = False
                    heat_percent_old = 0
                    try:
                        self.cbpi.notify(
                            "Dry fire",
                            "{} reached {:.1f}, past the {:.1f} safe limit. The "
                            "element has been switched off - the vessel is "
                            "almost certainly dry.".format(
                                getattr(self.kettle, "name", "Kettle"),
                                current_temp, max_safe),
                            NotificationType.ERROR,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    self.running = False
                    break

                if dry_watch is not None and dry_watch.note(
                    current_temp, dry_watts, dry_litres, degree_ratio
                ):
                    await self.actor_off(self.heater)
                    heater_is_on = False
                    heat_percent_old = 0
                    try:
                        self.cbpi.notify(
                            "Dry fire",
                            dry_watch.describe(
                                getattr(self.kettle, "name", "Kettle"), dry_litres
                            ),
                            NotificationType.ERROR,
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    self.running = False
                    break

                if current_temp >= threshold:
                    boil_latched = True
                elif boil_latched and current_temp < threshold - exit_drop:
                    # Genuinely off the boil - a lid opened, a chiller started,
                    # or the element failed - so go back to getting it there.
                    boil_latched = False
                    announced_boil = False

                if boil_latched:
                    heat_percent = self._boil_power()
                    if not announced_boil:
                        announced_boil = True
                        try:
                            self.cbpi.notify(
                                getattr(self.kettle, "name", "Boil"),
                                "Boiling at {:.1f}. Holding {}% - adjust from the "
                                "dashboard.".format(current_temp, heat_percent),
                                NotificationType.INFO,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    heat_percent = max_output
                    announced_boil = False

                # Drive the actor's on/off state, not just its power level.
                # set_power() only forwards a number to the instance; it never
                # changes state, so a demand of 0% would leave a plain GPIOActor
                # nominally on at 0% duty rather than genuinely off.
                if heat_percent > 0:
                    if not heater_is_on:
                        await self.actor_on(self.heater, heat_percent)
                        heater_is_on = True
                        heat_percent_old = heat_percent
                    elif heat_percent != heat_percent_old:
                        await self.actor_set_power(self.heater, heat_percent)
                        heat_percent_old = heat_percent
                else:
                    # Unconditional, for the same reason as the guards above.
                    await self.actor_off(self.heater)
                    heater_is_on = False
                    heat_percent_old = 0

                await clock.sleep(sample_time)

        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            logging.exception("BoilPower error: %s", e)
        finally:
            self.running = False
            # Tell the interface the logic has gone.
            #
            # Kettle.to_dict() reports state from instance.state, and nothing
            # else clears it. A guard that stopped the loop therefore left the
            # dashboard showing a running kettle with no controller behind it -
            # observed as running=True for twenty seconds after the element had
            # been de-energized. A control surface that claims to be in charge
            # when it is not is worse than one that admits it stopped.
            try:
                self.state = False
            except Exception:  # noqa: BLE001
                pass
            heater = getattr(self, "heater", None)
            if heater is not None:
                await self.actor_off(heater)


def setup(cbpi):
    cbpi.plugin.register("BoilPower", BoilPower)

