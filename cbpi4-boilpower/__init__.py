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

    # Floor for SampleTime. GPIOActor derives its on-phase from this value, and
    # at zero neither branch of its duty cycle sleeps - which starves the event
    # loop rather than merely running fast.
    MIN_SAMPLE_TIME = 0.1

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

    def _sample_time(self):
        """Seconds between control decisions. Never raises.

        This was a bare int(), which raises on "2.5" or on anything a human
        might type into a free-text field - and an exception here escapes into
        run()'s handler and kills the control loop before it has switched the
        element off once. Every other property on this class is parsed
        defensively; this one was the exception, for no reason.

        Floored rather than clamped to the Select options, because a fork or a
        hand-edited config file may legitimately carry something else. The
        floor exists because GPIOActor derives its on-phase from this value:
        at zero, neither branch of its duty cycle sleeps and it starves the
        event loop.
        """
        try:
            seconds = float(self.props.get("SampleTime", self.DEFAULT_SAMPLE_TIME))
        except (TypeError, ValueError):
            return self.DEFAULT_SAMPLE_TIME
        if seconds != seconds:  # NaN
            return self.DEFAULT_SAMPLE_TIME
        return max(self.MIN_SAMPLE_TIME, seconds)

    @staticmethod
    def _clamp_percent(value, default):
        """A duty percentage, bounded to 0-100. Never raises.

        Anything deciding how long a five kilowatt element stays on has to cope
        with whatever arrives - a blank field, text, a NaN, an infinity -
        without throwing inside the control loop.

        `default` is returned unchanged when the value cannot be read, so a
        caller can pass None to mean "tell me it was unreadable" rather than
        being handed a number it did not ask for. int(None) used to raise here,
        which made that impossible.
        """
        def fallback():
            return default if default is None else int(default)

        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return fallback()
        # NaN and both infinities. NaN fails every comparison, so min/max would
        # propagate it straight into a duty cycle; infinity would clamp to a
        # number but says nothing about what the caller meant.
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return fallback()
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

        Never raises. The configured value is a free-text property, so it can
        be anything a brewer types, and a bare float() on it raised out of
        run() before the heater had even been resolved - which meant both
        cleanup paths skipped the de-energize and a failed start could leave an
        already-on element running with no logic behind it.
        """
        configured = self._default_threshold()
        raw = self.props.get("Boil_Threshold", None)
        if raw is not None and raw != "":
            try:
                parsed = float(raw)
                if parsed == parsed and parsed not in (float("inf"), float("-inf")):
                    configured = parsed
                else:
                    logging.warning(
                        "BoilPower: ignoring Boil_Threshold %r, using %.1f",
                        raw, configured,
                    )
            except (TypeError, ValueError):
                logging.warning(
                    "BoilPower: ignoring Boil_Threshold %r, using %.1f",
                    raw, configured,
                )
        try:
            published = float(self.get_kettle_target_temp(self.id) or 0)
            if published > 0 and published == published:
                return published
        except (TypeError, ValueError, AttributeError):
            pass
        return configured

    # A vessel this far above boiling is dry, whatever the setpoint says.
    #
    # The default used to be Boil_Threshold + 15C, which is wrong in a way that
    # would ruin a brew day: it trips whenever the kettle legitimately runs
    # hotter than its own threshold. Set a 77C/170F hop stand and then bring
    # the same kettle to a boil and the guard fires on a perfectly healthy
    # boil, killing the element and stopping the logic.
    #
    # Water cannot exceed its boiling point by any margin while there is water
    # in it - the energy goes into vapour, not temperature - so a reading well
    # above boiling means there is nothing left to boil. That is an absolute
    # fact about the vessel, independent of what the brewer asked for, which is
    # what a last-resort guard should be keyed on.
    def _heater_now(self):
        """What the heater actor is actually doing: (on, power, known).

        Read rather than remembered. A loop that tracks the element's state in
        a local variable is wrong the moment anything else touches the actor -
        and on a rig, the brewer is something else that touches the actor.

        The third value is the one that matters for safety. This used to return
        (False, None) both when the actor was genuinely off and when it could
        not be read at all, and the caller cannot tell those apart: "off" means
        command it on, so an unreadable actor made the loop re-issue actor_on()
        every single pass, for ever.

        That is not a cosmetic difference. GPIOActor implements its duty cycle
        by sleeping through an on-phase and an off-phase, so re-commanding it
        part-way through a window can restart that window and hold the element
        on past the duty it was given - delivering more power than asked for,
        which is the one failure this plugin must never have.

        So: known=False means "no idea", and the caller keeps using what it
        last commanded instead of treating silence as off.

        Never raises.
        """
        try:
            registry = getattr(self.cbpi, "actor", None)
            if registry is None:
                self._note_read_failure("no actor registry")
                return False, None, False
            actor = registry.find_by_id(self.heater)
            if actor is None:
                # A genuinely absent actor is knowledge, not an error: there is
                # nothing energized, so commanding it on is the right response.
                self._heater_read_ok = True
                return False, None, True

            # Prefer the driver's own state over the dataclass field.
            #
            # find_by_id returns the container, and its `state` is not what
            # commands update - the instance's is, and that is what to_dict
            # serialises. Reading the container could therefore report the
            # actor off while the driver had it on, which defeats the
            # reconciliation this function exists to perform and makes the loop
            # re-issue ON every decision.
            instance = getattr(actor, "instance", None)
            if instance is not None:
                state = getattr(instance, "state", None)
                power = getattr(instance, "power", getattr(actor, "power", None))
            else:
                state = getattr(actor, "state", None)
                power = getattr(actor, "power", None)

            if state is None:
                # Unknown is not off. Saying off would command it on.
                self._note_read_failure("actor reports no state")
                return False, None, False

            self._heater_read_ok = True
            return bool(state), power, True
        except Exception as e:  # noqa: BLE001
            self._note_read_failure(e)
            return False, None, False

    def _note_read_failure(self, reason):
        """Log an unreadable heater once per episode, not once per pass.

        The loop reads the actor every SampleTime, so logging a traceback on
        each failure buries a brew day's log in thousands of copies of the same
        stack - and the log is the only record of what the rig did. One entry
        when it breaks, one when it recovers.
        """
        if getattr(self, "_heater_read_ok", True):
            self._heater_read_ok = False
            logging.exception(
                "BoilPower: cannot read heater %s (%s). Falling back to the "
                "last commanded duty until it can be read again.",
                getattr(self, "heater", None), reason,
            )

    def _tell(self, title, message, level=NotificationType.INFO):
        """Say something to the brewer, and write it down.

        PIDBoil logs a message and then notifies it, so anything a brewer was
        told can be found afterwards. This plugin only notified, and the gap
        showed the first time it mattered: asked what warning had stopped a
        brew day, there was nothing in the log to answer with - the message had
        to be reconstructed from arithmetic on the current settings.

        One call does both, so the two cannot drift apart.
        """
        text = "{}: {}".format(title, message)
        if level == NotificationType.ERROR:
            logging.error(text)
        elif level == NotificationType.WARNING:
            logging.warning(text)
        else:
            logging.info(text)
        try:
            self.cbpi.notify(title, message, level)
        except Exception:  # noqa: BLE001 - never fail control on a toast
            pass

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

        # float() accepts NaN and both infinities, and neither is a temperature.
        #
        # A NaN fails every comparison, so `current_temp >= threshold` is False
        # and the loop concludes the wort is below the boil - demanding full
        # power on a reading that says nothing at all. After the latch it is
        # worse: the latch is never released either, so the kettle is stuck on
        # whichever side of the threshold it happened to be.
        #
        # An infinity compares cleanly and is just as meaningless.
        if value != value or value in (float("inf"), float("-inf")):
            logging.warning(
                "BoilPower: ignoring sensor %s, reading is %r",
                sensor_id, value,
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
            label="Boil_Power", configurable=True, default_value=85,
            min=0, max=100, step=1, unit="%",
            description="Element duty while boiling. Takes effect on the next "
                        "control decision without interrupting the boil.")],
    )
    async def set_boil_power(self, Boil_Power=None, Power=None, **kwargs):
        """Change boil vigour on a running boil, without cutting the element.

        The parameter is labelled `Boil_Power` to match the property it sets.
        That is a convention rather than a requirement, and it is what lets a
        generic interface open the field showing the duty currently in force
        instead of blank: the dialog seeds any action parameter whose label
        matches a configured property. A field that opens empty and is then
        submitted writes a power of nothing.

        `Power` is still accepted, because an earlier version of this plugin
        declared it under that name and something may still be calling it.
        """
        if Boil_Power is None:
            Boil_Power = Power
        previous = self._boil_power()

        # An unreadable value leaves the duty alone rather than falling back to
        # the class default.
        #
        # It used to clamp to DEFAULT_BOIL_POWER, so clearing the field while
        # boiling at 20% and pressing Set asked for 85% - a large, silent
        # increase in boil vigour from an incomplete edit, which is a boilover
        # rather than a typo. A default is the right answer for a property that
        # has never been configured, and the wrong one for a live command that
        # arrived malformed.
        requested = self._clamp_percent(Boil_Power, None)
        if requested is None:
            self._tell(
                getattr(getattr(self, "kettle", None), "name", "Boil"),
                "Ignored a boil power of {!r} - holding {}%.".format(
                    Boil_Power, previous
                ),
                NotificationType.WARNING,
            )
            return

        self._boil_power_override = requested

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

        self._tell(
            getattr(getattr(self, "kettle", None), "name", "Boil"),
            "Boil power now {}% (was {}%).".format(
                self._boil_power_override, previous
            ),
        )

    async def on_stop(self):
        # Guarded: run() may have raised before self.heater was assigned, and an
        # AttributeError here would mask whatever actually went wrong.
        heater = getattr(self, "heater", None)
        if heater is not None:
            await self.actor_off(heater)

    async def run(self):
        try:
            # Resolve the hardware and establish a known state first.
            #
            # This used to parse the sample time, threshold and max output
            # before touching the kettle, so anything that raised in there -
            # and Boil_Threshold is a free-text property a brewer can type
            # anything into - escaped before self.heater existed. Both cleanup
            # paths are guarded on that attribute, so they skipped the
            # de-energize, and a failed start left an element that was already
            # on running with no logic behind it. Nothing in the interface said
            # so; the kettle simply showed as stopped.
            #
            # Nothing below this point is needed to switch a heater off, so
            # nothing below this point runs before it.
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

            sample_time = self._sample_time()
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

            degree_ratio = 1.0 if self._unit() == "C" else 1.8

            # Nothing else here is concerned with temperature.
            #
            # This logic is a dumb power controller: full output until the wort
            # reaches the threshold, then the duty the brewer chose. The only
            # question it asks about temperature is which of those two it is
            # in.
            #
            # It previously carried two safety guards - an over-temperature
            # ceiling and a stall watch - and both were removed. Each ruined a
            # brew day by cutting the element on a healthy kettle, and neither
            # belongs here: a limit that stops heating is a policy about the
            # vessel, not about how hard to boil it, and burying it in a power
            # controller means every logic has to reimplement it and get it
            # wrong separately. Those limits belong in a layer that owns them.

            # Publish a setpoint if there is none, so the dashboard has
            # something to show. Without this the SV widget read 0 and a
            # running boil displayed "0 deg 85%", which tells the brewer
            # nothing and looks broken.
            try:
                if not (float(self.kettle.target_temp or 0) > 0):
                    self.kettle.target_temp = threshold
            except Exception:  # noqa: BLE001 - a cosmetic field, never fatal
                pass
            announced_boil = False
            # What this loop last commanded, used only when the actor cannot be
            # read. Starts as 0 because the line above just de-energized it.
            commanded = 0
            # Once boiling, stay boiling until the wort genuinely comes off it.
            boil_latched = False
            exit_drop = self.BOIL_EXIT_DROP_C * degree_ratio

            while self.running:
                current_temp = self._read_temp(sensor_id)
                # Fresh every pass, so moving the setpoint acts on a running
                # boil instead of waiting for a restart.
                threshold = self._threshold()

                if current_temp is None:
                    # No trustworthy reading. This is not a temperature policy,
                    # it is refusing to act on garbage: with no measurement
                    # there is no way to know whether to be at full power or at
                    # the holding duty, so the element comes off until a real
                    # one returns.
                    #
                    # Unconditional: commanding off something already off costs
                    # one redundant call, while not commanding off something
                    # that IS on is how an element stays live.
                    await self.actor_off(self.heater)
                    commanded = 0
                    await clock.sleep(sample_time)
                    continue

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
                        self._tell(
                            getattr(self.kettle, "name", "Boil"),
                            "Boiling at {:.1f}. Holding {}% - adjust from the "
                            "dashboard.".format(current_temp, heat_percent),
                        )
                else:
                    heat_percent = max_output
                    announced_boil = False

                # Drive the actor from what it IS doing, not from what this loop
                # last told it to do.
                #
                # These were tracked in locals - heater_is_on, heat_percent_old
                # - and commanded only on a believed transition. Switch the
                # element off from the dashboard and the loop still believed it
                # was on, so it never commanded again: the element stayed dark
                # while the logic went on thinking it was driving it, until the
                # brewer stopped and restarted the automation.
                #
                # That is the same desync ActorController.off had, where acting
                # only when the software believed the actor was already on made
                # switching off a no-op exactly when it was needed. Belief about
                # hardware goes stale the moment anything else touches it, and
                # on a rig the brewer is something else that touches it.
                #
                # Reading the actor costs one dictionary lookup per control
                # decision and cannot go stale.
                actual_on, actual_power, known = self._heater_now()

                if not known:
                    # Could not read the element. Fall back to what this loop
                    # last commanded, which is the best information left.
                    #
                    # Treating unreadable as off would re-command every pass and
                    # disturb the actor's duty cycle - see _heater_now.
                    actual_on = commanded is not None and commanded > 0
                    actual_power = commanded

                if heat_percent > 0:
                    if not actual_on:
                        await self.actor_on(self.heater, heat_percent)
                    elif actual_power != heat_percent:
                        await self.actor_set_power(self.heater, heat_percent)
                    commanded = heat_percent
                else:
                    # Unconditional, for the same reason as the guards above.
                    await self.actor_off(self.heater)
                    commanded = 0

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

