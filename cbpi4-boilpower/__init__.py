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
        logging.info(
            "BoilPower: duty set to %s%% (was %s%%)",
            self._boil_power_override, previous,
        )
        try:
            self.cbpi.notify(
                getattr(getattr(self, "kettle", None), "name", "Boil"),
                "Boil power now {}% (was {}%). This boil only; Boil_Power is "
                "unchanged.".format(self._boil_power_override, previous),
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
            threshold = float(
                self.props.get("Boil_Threshold", None) or self._default_threshold()
            )
            max_output = self._clamp_percent(
                self.props.get("Max_Output", self.DEFAULT_MAX_OUTPUT),
                self.DEFAULT_MAX_OUTPUT,
            )
            # Preserve an override across a property edit, which constructs a
            # new instance; lose it on a genuine restart.
            self._boil_power_override = getattr(self, "_boil_power_override", None)

            self.kettle = self.get_kettle(self.id)
            self.heater = self.kettle.heater
            sensor_id = self.kettle.sensor

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

            while self.running:
                current_temp = self._read_temp(sensor_id)

                if current_temp is None:
                    # No trustworthy reading. Heating blind toward a boil is the
                    # one thing not to do, so the element comes off until a real
                    # measurement returns.
                    if heater_is_on:
                        await self.actor_off(self.heater)
                        heater_is_on = False
                        heat_percent_old = 0
                    await clock.sleep(sample_time)
                    continue

                if current_temp >= threshold:
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
                    if heater_is_on:
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
            heater = getattr(self, "heater", None)
            if heater is not None:
                await self.actor_off(heater)


def setup(cbpi):
    cbpi.plugin.register("BoilPower", BoilPower)
