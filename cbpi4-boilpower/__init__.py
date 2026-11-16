"""Full power to the boil, then a fixed duty the brewer sets from the dashboard;
or, given a target below the boil, hold it there with a PID.

A boil does not need a PID. Once wort is boiling its temperature stops being a
control variable - it sits at the boiling point whatever you do - and the only
thing left to choose is how hard it boils. PIDBoil models this as a special
case at the end of a PID; this plugin is that case on its own:

    below the boil point (Boil_Threshold)   ->  Max_Output, get there
    at or above it                          ->  Boil_Power, hold that duty

The kettle's target says which job it has, as on any kettle - set the boil
kettle's target to 170 F, like the HLT's, and it holds there:

    no target, or one at the boil   ->  boil, as above
    a target below the boil         ->  hold it with a PID: a mash in a bag,
                                        a whirlpool, a hop stand, warm water

"At the boil" is within BOIL_EXIT_DROP_C of Boil_Threshold or above, so a Boil
step's 210 F or 212 F means boil wherever the threshold sits. Boil_Threshold is
the one place the boil point is set - at altitude, set it to where your wort
boils - and the kettle's target does not move it. In versions before
0.0.3.post7 the target is the boil point, and a hold is a separate Hold_Target
setting; a restart clears a Hold_Target left in a kettle's settings.

The duty is adjustable while the boil runs, through the Set Boil Power action,
because the alternative is unusable. Editing a kettle property calls
BasicController.update(), which calls stop() and - since KettleController sets
autostart False - never restarts. Verified on a live rig: changing the power
property mid-boil left state=False with the element de-energized, the new value
saved and doing nothing, and no warning. An action dispatches straight to the
running logic and never touches update(), so vigour changes without the boil
being interrupted. The same goes for Keep it at the boil, Pause the heat and
Boil at: each is an action for the same reason.

Every action declares exactly one parameter, named for the setting it keeps,
and is a no-op given the value in force. The old dashboard's Set Target Temp
dialog sends every action a logic declares, each seeded with its value in
force, every time Set is pressed.

Timing deliberately matches PIDBoil rather than inventing its own. The loop
runs on SampleTime and hands the percentage to the actor exactly as PIDBoil
does while approaching setpoint: actor_on(power) on the first demand,
actor_set_power() on a change, actor_off() at zero. Nothing here implements its
own duty cycle - whatever drives the element decides how a percentage becomes
on-time, so a PWM actor, an SSR and a contactor each behave as configured.
"""

import asyncio
import inspect
import logging
import math
import time

from cbpi.api import *
from cbpi.api.dataclasses import NotificationAction, NotificationType

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


#: The forgotten-pause warning (BoilPower.PAUSE_WARN_AFTER_S): a title and its
#: message, worded like "HLT pump still running".
#: Kept on every page until OK, or until the heat carries on or AUTO stops. No
#: button heats: on the brew-day screen heat only comes on with a held press,
#: on the element button, which reads PAUSED while the heat is paused.
PAUSE_FORGOTTEN_TITLE = "{kettle} heat still paused"
PAUSE_FORGOTTEN_TEXT = (
    "The heat has been paused for {minutes} minutes. To heat again, press and "
    "hold PAUSED on the {kettle}."
)


def _notify_kept(cbpi, title, message, type, action):
    """notify(..., keep=True) where CraftBeerPi can keep a prompt for every
    page, plain notify where it cannot. -> the notification's id, if any."""
    try:
        keeps = "keep" in inspect.signature(cbpi.notify).parameters
    except (TypeError, ValueError):
        keeps = False
    if keeps:
        return cbpi.notify(title, message, type, action=action, keep=True)
    return cbpi.notify(title, message, type, action=action)



@parameters([
    Property.Number(
        label="Boil_Threshold", configurable=True,
        description="Where this kettle boils: full power below it, Boil_Power "
                    "from it. Default 99 C / 210.2 F. At altitude, set it to "
                    "where your wort boils. A target within 2 C / 3.6 F of it, "
                    "or above, means boil; one further below means hold."),
    Property.Number(
        label="Boil_Power", configurable=True, default_value=85,
        description="Element duty once boiling, 0-100%. Adjustable from the "
                    "dashboard while the boil runs."),
    Property.Number(
        label="Max_Output", configurable=True, default_value=100,
        description="Element duty while heating up to the boil, and the most "
                    "the PID may use while it holds a target below the boil."),
    Property.Number(
        label="P", configurable=True,
        description="Holding a target below the boil - a mash in a bag, a "
                    "whirlpool, a hop stand - a PID sets the element's power. "
                    "P: % of power per degree below the target. Blank: 25.2 "
                    "per C / 14 per F. The PID AutoTune plugin's P, I and D "
                    "fit here."),
    Property.Number(
        label="I", configurable=True,
        description="The PID's I: % of power per degree per second the wort "
                    "stays below the target, building up within 2.8 C / 5 F "
                    "of it. Blank: 0.054 per C / 0.03 per F."),
    Property.Number(
        label="D", configurable=True,
        description="The PID's D: % of power taken off per degree per second "
                    "the wort is rising, so it slows before the target rather "
                    "than overshooting. Blank: 1620 per C / 900 per F. 0 "
                    "turns it off."),
    Property.Select(
        label="SampleTime", options=[2, 5],
        description="Seconds between control decisions. Matches PIDBoil."),
    Property.Select(
        label="Keep_Boiling", options=["Yes", "No"], default_value="Yes",
        description="Yes: if the wort falls 2 C / 3.6 F below the boil, full "
                    "power brings it back. No: once it has boiled, only "
                    "Boil_Power heats, and the wort may fall if that is too "
                    "low. Changeable while boiling from the brew-day screen."),
    Property.Number(
        label="Usual_Boil_Power", configurable=True,
        description="The boil power each recipe starts at, 0-100%. When a "
                    "recipe starts from its first step, Boil_Power goes back "
                    "to this. Blank: Boil_Power stays as you left it."),
])
class BoilPower(CBPiKettleLogic):
    """Ramp at full power, then hold a chosen duty; or hold a target below the
    boil with a PID."""

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

    # The most SampleTime may be. The element stays as one decision set it
    # until the next, so a failed sensor or a boil power turned down to 0%
    # waits up to this long to reach it; more than a minute is not control.
    MAX_SAMPLE_TIME = 60

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

    # The PID that holds a target below the boil (_HoldPID). Any hold is PID
    # control, with nothing to choose it: a mash in a bag held at 150 F needs
    # tight regulation. Switching off at the target and resting until the
    # wort is 3.6 F below would leave a hold up to 4 F under its target.
    #
    # Gains as PIDBoil's, which are PIDArduino's: % of the element per degree
    # below the target, per degree-second, and per degree-per-second of rise,
    # so the PID AutoTune plugin's results fit. Authored in Celsius and
    # converted, like every default here: 14, 0.03 and 900 in Fahrenheit.
    #
    # Chosen on cbpi4-simvessel's equations for both boil kettles this runs:
    # the simulators' (40 L, 5.5 kW, losing 25 W/K, probe 20 s behind) and the
    # rig's (RIG-NOTES 27.6: 205 kJ/K, 5.3 kW, about 5 W/K, probe about 65 s
    # behind), the rig's again with a probe twice as slow, as a bag of grain
    # might make it, and each at half the volume. From cold to 150 F the rig's
    # passes it by about 0.8 F (1.4 F with the slower probe) and then holds
    # within 0.1 F; the simulators' passes it by 0.3 F. A whirlpool chilled to
    # 158 F is back within 2 F of 170 F in about 5 minutes on the rig and 7 on
    # the simulators. What the rig really does is untested.
    DEFAULT_P_C = 25.2
    DEFAULT_I_C = 0.054
    DEFAULT_D_C = 1620.0

    # The integral builds up only this close to the target. Climbing from far
    # below it would otherwise build up all the way, and the rig's kettle,
    # which takes hours to lose a few degrees, would sit past its target:
    # about 2 F past, on the simulated rig, against 0.8 F like this.
    INTEGRAL_BAND_C = 2.8

    # The D term brakes on how fast the readings rise: a straight line through
    # the last this-many seconds of them. A rig probe reads in steps of
    # 0.11 F, and the difference of two readings would kick the element at
    # every step.
    DERIVATIVE_WINDOW_S = 60.0

    # How long the heat may stay paused before the brewer is warned, in
    # brewing seconds: a forgotten pause gets a warning on every page after 2
    # minutes. A boil-over is dealt with in seconds; two minutes off is a boil
    # going cold with nobody watching.
    PAUSE_WARN_AFTER_S = 120

    # Keep_Boiling values that mean No. Anything else - including a kettle set
    # up before the setting existed - is Yes, which is how this logic always
    # behaved.
    _NO = ("no", "false", "0", "off")

    # A pause (set_paused): when it began on the brewing clock, else None; and
    # its forgotten-pause warning once given - the notification's id, or True
    # where CraftBeerPi gives none. Class defaults, so a logic built without
    # __init__, as the tests and the transient action instance are, has them.
    _paused_since = None
    _pause_warned = None

    # The kettle's target as last read when it was a number (_target), None
    # for no target; and whether a target that is not a number has been
    # reported, until the kettle has a number or no target again. Class
    # defaults, as above.
    _last_target = None
    _target_refused = False

    def _unit(self):
        return str(self.get_config_value("TEMP_UNIT", "C")).upper()

    def _default_threshold(self):
        if self._unit() == "C":
            return self.DEFAULT_THRESHOLD_C
        return self.DEFAULT_THRESHOLD_C * 9.0 / 5.0 + 32.0

    def _sample_time(self):
        """Seconds between control decisions. Never raises.

        SampleTime offers 2 or 5, but a fork, a hand-edited config file or an
        API call can carry anything, so it is parsed rather than trusted. The
        loop sleeps this long after every decision, and the element stays as
        that decision set it: an infinite SampleTime would leave it for good.
        So a value that is not a finite number - text, NaN, an infinity, a
        number too big for a float - or that is more than MAX_SAMPLE_TIME is
        the default, 5, and the brewer is told. Blank or missing is the
        default, quietly.

        A number below MIN_SAMPLE_TIME is raised to it; any other is used as
        it is, not rounded to the choices offered.
        """
        raw = self.props.get("SampleTime", None)
        if raw is None or str(raw).strip() == "":
            return self.DEFAULT_SAMPLE_TIME
        try:
            seconds = float(raw)
        except (TypeError, ValueError, OverflowError):
            seconds = float("nan")
        if not math.isfinite(seconds) or seconds > self.MAX_SAMPLE_TIME:
            shown = repr(raw)
            if len(shown) > 24:
                shown = shown[:21] + "..."
            self._tell(self._kettle_name(),
                       "SampleTime is set to {}, which is not a number of seconds "
                       "up to {}, so {} is used instead.".format(
                           shown, self.MAX_SAMPLE_TIME, self.DEFAULT_SAMPLE_TIME),
                       NotificationType.WARNING)
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
        being handed a number it did not ask for: None is returned as it is,
        not passed to int(), which would raise.
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

    def _keep_boiling(self):
        """Keep_Boiling, read fresh each pass: True unless it says No.

        Yes is the usual boil: the latch lets go when the wort falls
        BOIL_EXIT_DROP_C below the boil, and full power brings it back. No
        keeps the latch once the wort has boiled, so Boil_Power alone drives
        it, and the wort may fall if the power is set low, rather than the
        kettle cycling between full power and, say, 2%.
        """
        raw = self.props.get("Keep_Boiling", "Yes")
        return str(raw).strip().lower() not in self._NO

    async def _keep_setting(self, name, value):
        """Keep a setting with the kettle, on disk, and tell every page.

        Written to this logic's settings and to the kettle's, which are the
        same dictionary for a running logic and may not be for the throwaway
        one an action builds while the kettle is off. save() writes the file
        and pushes an update without stopping the logic, unlike update().
        Never raises: the setting is in force here either way.
        """
        try:
            self.props[name] = value
        except Exception:  # noqa: BLE001
            pass
        try:
            kettle = self.get_kettle(self.id)
            props = getattr(kettle, "props", None)
            if props is not None and props is not self.props:
                props[name] = value
            await self.cbpi.kettle.save()
        except Exception as e:  # noqa: BLE001 - never fail an action on this
            logging.warning("BoilPower: could not keep %s with the kettle: %s", name, e)

    def _kettle_name(self):
        """This kettle's name, for the brewer. Never raises.

        self.kettle is only set once run() starts, and an action can reach a
        logic that never ran - a stopped kettle's - so it is looked up.
        """
        kettle = getattr(self, "kettle", None)
        if kettle is None:
            try:
                kettle = self.get_kettle(self.id)
            except Exception:  # noqa: BLE001
                kettle = None
        return getattr(kettle, "name", None) or "Boil kettle"

    def _pid_gains(self):
        """The hold's P, I and D in the server's unit: each the kettle's
        setting, else the default. Never raises.

        A setting is taken as entered, in the unit the server runs in, as
        PIDBoil takes its own; a default is Celsius and converted. A blank,
        unreadable, infinite or negative setting is the default - a negative
        gain would heat the wort harder the further it rose past its target.
        0 is a setting: no D, say.
        """
        ratio = self._degree_ratio()
        gains = []
        for label, celsius in (("P", self.DEFAULT_P_C),
                               ("I", self.DEFAULT_I_C),
                               ("D", self.DEFAULT_D_C)):
            try:
                value = float(self.props.get(label))
            except (TypeError, ValueError):
                value = None
            if value is None or not math.isfinite(value) or value < 0:
                value = celsius / ratio
            gains.append(value)
        return tuple(gains)

    def _configured_boiling(self):
        """Where this kettle boils: Boil_Threshold, else 99 C / 210.2 F.

        The one place the boil point is set. The kettle's target does not move
        it: a target says which job the kettle has (_hold_target), and asking
        for a 170 F hop stand must not redefine 170 F as boiling.

        Never raises: Boil_Threshold is free text and can be anything typed.
        """
        return _boiling_in(self.props, self._unit())

    def _target(self):
        """The kettle's target, a finite number above 0, else None. Read
        fresh. Never raises.

        None is "no target", and BoilPower boils without one: a target that
        is unset, blank, 0 or below.

        A target that is not a number - NaN, an infinity, a number too big for
        a float, text, or one that cannot be read at all - is not taken as no
        target. Taken that way, a hold below the boil would end, and the
        element would go to Max_Output for the boil. The last target that was
        a number stands instead, so the kettle carries on as it was, and the
        brewer is told once. With none before it, it is no target, and the
        kettle boils at Boil_Threshold.
        """
        try:
            raw = self.get_kettle_target_temp(self.id)
        except Exception:  # noqa: BLE001
            # Broad on purpose: this calls into the core, and through it into
            # whatever is registered. Anything raised here would escape the
            # control loop, which would die silently with the interface still
            # showing the logic running.
            return self._target_before("could not be read")
        try:
            value = float(raw or 0)
        except (TypeError, ValueError, OverflowError):
            value = float("nan")
        if not math.isfinite(value):
            shown = repr(raw)
            if len(shown) > 24:
                shown = shown[:21] + "..."
            return self._target_before("is {}, which is not a temperature".format(shown))
        self._target_refused = False
        self._last_target = value if value > 0 else None
        return self._last_target

    def _target_before(self, why):
        """_target's answer for a target that is not a number: the last one
        that was, or None. Tells the brewer once, `why` saying what is wrong
        with it, until the kettle has a number or no target again."""
        last = getattr(self, "_last_target", None)
        if not getattr(self, "_target_refused", False):
            self._target_refused = True
            if last is None:
                after = "With no target before it, the kettle boils."
            else:
                after = "The target before it, {:.1f}, still stands.".format(last)
            self._tell(self._kettle_name(),
                       "The target {}, so it is ignored. {}".format(why, after),
                       NotificationType.WARNING)
        return last

    def _hold_target(self, threshold, exit_drop):
        """The temperature to hold, or None for a boil.

        The kettle's target, when it is more than exit_drop below the boil
        point `threshold`: set the boil kettle's target to 170 F, like the
        HLT's, and it holds there. The HLT's PIDBoil works the
        same way: it holds any target, and switches to its boil power only once
        the wort reaches its own Boil_Threshold.

        A target within exit_drop of the boil, or above it, is a boil - a Boil
        step's 210 F or 212 F, or a 99 C one under a 99.5 C threshold - and so
        is no target at all. A hold that close to boiling could not be told
        apart from a boil, and the hold's own band would put it at the boil.

        Boil_Threshold says where the boil is, and a gentler boil is
        Boil_Power. (In versions before 0.0.3.post7 the target is the boil
        point, so "my boil is at 205" is said with the target, and a hold is
        asked for separately, through Hold_Target.)
        """
        target = self._target()
        if target is None or target >= threshold - exit_drop:
            return None
        return target

    def _threshold(self):
        """Where full power ends and the boil power begins: the boil point,
        Boil_Threshold, read fresh each pass. Never raises.

        The kettle's target says boil or hold (_hold_target); only
        Boil_Threshold moves the boil point - set it with Boil at, running or
        not.
        """
        return self._configured_boiling()

    # A vessel this far above boiling is dry, whatever the setpoint says.
    #
    # Not Boil_Threshold + 15C, which would ruin a brew day: it trips whenever
    # the kettle legitimately runs hotter than its own threshold. Set a
    # 77C/170F hop stand and then bring the same kettle to a boil, and that
    # guard fires on a perfectly healthy boil, killing the element and
    # stopping the logic.
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

        The third value is the one that matters for safety. Given (False,
        None) both when the actor is genuinely off and when it cannot be read
        at all, the caller could not tell those apart: "off" means command it
        on, so an unreadable actor would make the loop re-issue actor_on()
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
        told can be found afterwards. A notification alone leaves nothing in
        the log to say which warning stopped a brew day.

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
                        "control decision, and can be set before the boil "
                        "starts.")],
        # Boil power is a configured property, not a command. Refusing to set
        # it while the logic is stopped made it unreachable at the one moment a
        # brewer most naturally reaches for it - before starting the boil - and
        # answered "start the kettle first", which is backwards. Setting it
        # stopped simply persists it for the next run.
        allow_stopped=True,
    )
    async def set_boil_power(self, Boil_Power=None, Power=None, **kwargs):
        """Change boil vigour, running or not.

        The parameter is labelled `Boil_Power` to match the property it sets.
        That is a convention rather than a requirement, and it is what lets a
        generic interface open the field showing the duty currently in force
        instead of blank: the dialog seeds any action parameter whose label
        matches a configured property. A field that opens empty and is then
        submitted writes a power of nothing.

        `Power` is still accepted, because older versions of this plugin
        declare it under that name and something may still be calling it.
        """
        if Boil_Power is None:
            Boil_Power = Power
        previous = self._boil_power()

        # An unreadable value leaves the duty alone rather than falling back to
        # the class default.
        #
        # Clamped to DEFAULT_BOIL_POWER, clearing the field while boiling at
        # 20% and pressing Set would ask for 85% - a large, silent increase in
        # boil vigour from an incomplete edit, which is a boilover rather than
        # a typo. A default is the right answer for a property that has never
        # been configured, and the wrong one for a live command that arrived
        # malformed.
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
        # Not saving, so that adjusting vigour for one batch leaves the recipe
        # alone, is wrong in practice: the dashboard slider seeds from
        # Boil_Power, so it would keep opening at the stored 85 while the
        # element is actually running at 10, and the brewer would have no way
        # to see the value in force. A control surface that disagrees with the
        # hardware is worse than one that remembers a number you can change
        # back.
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

    @action(
        "Keep it at the boil",
        [Property.Select(
            label="Keep_Boiling", options=["Yes", "No"],
            description="Yes: if the wort falls 2 C / 3.6 F below the boil, "
                        "full power brings it back. No: once it has boiled, "
                        "only the boil power heats, and the wort may fall.")],
        # A setting, like Boil_Power: chosen before the boil as naturally as
        # during it.
        allow_stopped=True,
    )
    async def set_keep_boiling(self, Keep_Boiling=None, **kwargs):
        """Keep the wort at the boil, or let the boil power alone drive it.

        Kept with the kettle, so it holds from one brew day to the next until
        it is changed. Used at the next control decision: set back to Yes
        while the wort is well below the boil, full power comes back at once.

        Raises on anything but Yes or No, after saying so: a refusal returned
        instead reaches no caller, since BasicController.call_action reports a
        failure only when the action raises.
        """
        name = self._kettle_name()
        text = "" if Keep_Boiling is None else str(Keep_Boiling).strip().lower()
        if text in ("yes", "true", "1", "on"):
            keep = True
        elif text in self._NO:
            keep = False
        else:
            message = ("Keep it at the boil needs Yes or No, not {!r}. It was "
                       "left as it was.".format(Keep_Boiling))
            self._tell(name, message, NotificationType.WARNING)
            raise ValueError(message)
        before = self._keep_boiling()
        stored = self.props.get("Keep_Boiling", None)
        if keep == before and stored not in (None, ""):
            # The value in force: what the old dashboard's Set sends.
            return
        await self._keep_setting("Keep_Boiling", "Yes" if keep else "No")
        if keep == before:
            return
        if keep:
            self._tell(name, "Keeping it at the boil: if the wort falls {:.1f}{} "
                             "below {:.1f}{}, full power brings it back.".format(
                                 self.BOIL_EXIT_DROP_C * self._degree_ratio(), self._deg(),
                                 self._configured_boiling(), self._deg()))
        else:
            self._tell(name, "Boil power only: once it has boiled, only {}% heats, "
                             "and the wort may fall if that is too low.".format(
                                 self._boil_power()))

    @action(
        "Boil at",
        [Property.Number(
            label="Boil_Threshold", configurable=True,
            description="Where this kettle boils: full power below it, the "
                        "boil power from it. 190-212 F / 88-100 C. Blank: "
                        "99 C / 210.2 F.")],
        # A setting, like Boil_Power, and changed for the same reason mid-boil:
        # a probe that never reaches the boil point on a low-pressure day
        # holds full power until the brewer lowers it.
        allow_stopped=True,
    )
    async def set_boil_at(self, Boil_Threshold=None, **kwargs):
        """Move the boil point, running or not, without stopping the boil.

        Kept with the kettle, in its Boil_Threshold setting. A kettle aiming
        at the boil keeps aiming at it: its target moves with the boil point,
        so raising the boil point never turns a boil into a hold. Blank goes
        back to 99 C / 210.2 F.

        Raises on a value it cannot use, after saying so (see set_paused).
        """
        name = self._kettle_name()
        low, high = (88.0, 100.0) if self._unit() == "C" else (190.0, 212.0)
        before = self._configured_boiling()
        stored = self.props.get("Boil_Threshold", None)
        if Boil_Threshold is None or str(Boil_Threshold).strip() == "":
            if stored is None or str(stored).strip() == "":
                return
            new = ""
        else:
            try:
                value = float(Boil_Threshold)
            except (TypeError, ValueError):
                value = float("nan")
            if not math.isfinite(value) or not low <= value <= high:
                message = ("Boil at needs a temperature from {:g} to {:g}{}, not "
                           "{!r}. It still boils at {:.1f}{}.".format(
                               low, high, self._deg(), Boil_Threshold, before, self._deg()))
                self._tell(name, message, NotificationType.WARNING)
                raise ValueError(message)
            new = round(value, 1)
            if stored is not None and str(stored).strip() != "" and new == before:
                # The value in force: what the old dashboard's Set sends.
                return
        drop = self.BOIL_EXIT_DROP_C * self._degree_ratio()
        target = self._target()
        aiming_at_boil = target is not None and target >= before - drop
        await self._keep_setting("Boil_Threshold", new)
        after = self._configured_boiling()
        if aiming_at_boil and target != after:
            try:
                await self.cbpi.kettle.set_target_temp(self.id, after)
            except Exception as e:  # noqa: BLE001
                logging.warning("BoilPower: could not move the target with the boil point: %s", e)
        self._tell(name, "Boils at {:.1f}{} now (was {:.1f}{}).".format(
            after, self._deg(), before, self._deg()))

    @action(
        "Pause the heat",
        [Property.Select(
            label="Paused", options=["No", "Yes"], default_value="No",
            description="Yes: the element stays off while AUTO, the boil power "
                        "and the recipe carry on - for a boil-over. No: heat "
                        "again. MANUAL, ALL OFF or a restart end a pause.")],
        # One action with the state it sets, not a pause and a resume, and
        # allowed while stopped: the old dashboard's Set Target Temp dialog
        # sends every action the logic declares, each with the value in force
        # (seeded from the kettle's settings, or the default). So Set must
        # change nothing here - Paused is No unless paused, Yes while paused -
        # and a kettle that is off must not answer every Set with "start the
        # logic first". A parameterless pause and resume would pause and resume
        # the heat at every Set.
        allow_stopped=True,
    )
    async def set_paused(self, Paused=None, **kwargs):
        """Switch the element off and keep it off, without stopping AUTO; or
        carry on heating.

        For a boil-over. AUTO, the boil latch, Boil_Power and the recipe's
        timer carry on (the boil's timer keeps counting); only
        the element stays off, until Paused is No. Carrying on goes back to
        what AUTO does by then: the boil power once boiling, full power below,
        or the hold. MANUAL or ALL OFF, which stop this logic, end a pause too.

        Kept in the kettle's settings - Paused, Yes or No, and Paused_At, the
        brewing-clock seconds it began - so every page shows the pause and
        counts it from the same moment. After PAUSE_WARN_AFTER_S a warning is
        kept on every page until OK.

        Pausing is refused unless AUTO is running: a stopped kettle has no heat
        to pause, and a pause it remembered would greet the next start.
        Carrying on when not paused changes nothing, quietly.
        """
        name = self._kettle_name()
        text = "" if Paused is None else str(Paused).strip().lower()
        if text in ("yes", "true", "1", "on"):
            pause = True
        elif text in self._NO:
            pause = False
        else:
            message = ("Pause the heat needs Yes or No, not {!r}. Nothing was "
                       "changed.".format(Paused))
            self._tell(name, message, NotificationType.WARNING)
            raise ValueError(message)

        if not pause:
            if getattr(self, "_paused_since", None) is None:
                return
            await self._end_pause()
            self._tell(name, "Heating again.")
            return

        if not getattr(self, "running", False):
            message = "{}'s AUTO is off, so there is no heat to pause.".format(name)
            self._tell(name, message, NotificationType.WARNING)
            raise ValueError(message)
        if getattr(self, "_paused_since", None) is not None:
            return
        self._paused_since = clock.now()
        self._pause_warned = None
        heater = getattr(self, "heater", None)
        if heater is not None:
            # At once, not at the next control decision: a boil-over is now.
            await self.actor_off(heater)
        await self._keep_setting("Paused_At", round(self._paused_since, 1))
        await self._keep_setting("Paused", "Yes")
        self._tell(name, "Heat paused. The element stays off until you carry on; "
                         "AUTO, the boil power and the recipe carry on.")

    async def _end_pause(self):
        """Forget a pause, take its warning down from every page, and clear
        Paused_At. Never raises; nothing to do if there was none."""
        self._paused_since = None
        warned, self._pause_warned = getattr(self, "_pause_warned", None), None
        if warned is not None and warned is not True:
            retire = getattr(getattr(self.cbpi, "notification", None), "retire", None)
            if callable(retire):
                try:
                    retire(warned)
                except Exception as e:  # noqa: BLE001
                    logging.debug("BoilPower: could not take the pause warning down: %s", e)
        try:
            kept = self.props.get("Paused_At", None)
            paused = self.props.get("Paused", None)
        except Exception:  # noqa: BLE001
            kept = paused = None
        if kept not in (None, ""):
            await self._keep_setting("Paused_At", "")
        if paused not in (None, "", "No"):
            await self._keep_setting("Paused", "No")

    def _warn_if_pause_forgotten(self):
        """Once a pause, after PAUSE_WARN_AFTER_S: a warning kept on every
        page until OK, or until the pause ends. Never raises."""
        since = getattr(self, "_paused_since", None)
        if since is None or getattr(self, "_pause_warned", None) is not None:
            return
        try:
            if clock.now() - since < self.PAUSE_WARN_AFTER_S:
                return
        except Exception:  # noqa: BLE001
            return
        name = self._kettle_name()
        message = PAUSE_FORGOTTEN_TEXT.format(
            kettle=name, minutes=int(self.PAUSE_WARN_AFTER_S // 60))
        title = PAUSE_FORGOTTEN_TITLE.format(kettle=name)
        logging.warning("%s: %s", title, message)
        self._pause_warned = True
        try:
            warning = _notify_kept(self.cbpi, title, message, NotificationType.WARNING,
                                   [NotificationAction("OK")])
            if warning is not None:
                self._pause_warned = warning
        except Exception as e:  # noqa: BLE001
            logging.warning("BoilPower: could not warn about the pause: %s", e)

    def _degree_ratio(self):
        return 1.0 if self._unit() == "C" else 1.8

    def _deg(self):
        return " \u00b0C" if self._unit() == "C" else " \u00b0F"

    async def on_stop(self):
        # Guarded: run() may have raised before self.heater was assigned, and an
        # AttributeError here would mask whatever actually went wrong.
        heater = getattr(self, "heater", None)
        if heater is not None:
            await self.actor_off(heater)
        # MANUAL and ALL OFF end a pause: there is no AUTO left to carry on.
        # After the element, which is what stopping is for.
        await self._end_pause()

    async def run(self):
        try:
            # Resolve the hardware and establish a known state first.
            #
            # Parsing the sample time, threshold and max output first would
            # let anything that raised in there - and Boil_Threshold is a
            # free-text property a brewer can type anything into - escape
            # before self.heater existed. Both cleanup paths are guarded on
            # that attribute, so they would skip the de-energize, and a failed
            # start would leave an element that was already on running with no
            # logic behind it, the kettle simply showing as stopped.
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
            # The boil point is read per pass by _threshold(), so moving it
            # (Boil at) acts on a running boil. Only the starting value is
            # needed here, to publish a target if the kettle has none.
            threshold = self._threshold()
            max_output = self._clamp_percent(
                self.props.get("Max_Output", self.DEFAULT_MAX_OUTPUT),
                self.DEFAULT_MAX_OUTPUT,
            )
            # Preserve an override across a property edit, which constructs a
            # new instance; lose it on a genuine restart.
            self._boil_power_override = getattr(self, "_boil_power_override", None)
            # A pause belongs to the AUTO run it was made in. One still in the
            # settings is left from a run that ended without stopping - the
            # server went down - and must not greet this one.
            await self._end_pause()

            degree_ratio = 1.0 if self._unit() == "C" else 1.8

            # Nothing else here is concerned with temperature.
            #
            # This logic is a dumb power controller: full output until the wort
            # reaches the threshold, then the duty the brewer chose. The only
            # question it asks about temperature is which of those two it is
            # in.
            #
            # It carries no over-temperature ceiling or stall watch. Each cuts
            # the element on a healthy kettle, and neither belongs here: a
            # limit that stops heating is a policy about the vessel, not about
            # how hard to boil it, and burying it in a power controller means
            # every logic has to reimplement it and get it wrong separately.
            # Those limits belong in a layer that owns them.

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
            # The hold last announced, so a changed one is said again.
            announced_hold = None
            # What this loop last commanded, used only when the actor cannot be
            # read. Starts as 0 because the line above just de-energized it.
            commanded = 0
            # Once boiling, stay boiling until the wort genuinely comes off it.
            boil_latched = False
            exit_drop = self.BOIL_EXIT_DROP_C * degree_ratio
            # The hold's PID: its gains are read once, as Max_Output is - a
            # changed setting restarts the logic anyway - and the PID itself
            # is made as a hold begins and dropped when it ends, so a
            # whirlpool after the boil starts afresh rather than with what
            # the mash built up.
            gains = self._pid_gains()
            hold_pid = None

            while self.running:
                current_temp = self._read_temp(sensor_id)
                # Fresh every pass, so moving the boil point acts on a running
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
                    # The wort's rise can't be told across the gap.
                    if hold_pid is not None:
                        hold_pid.forget_readings()
                    await clock.sleep(sample_time)
                    continue

                # A target BELOW the boil is a request to hold that
                # temperature, not to boil - a mash in a bag, a whirlpool or a
                # hop stand (_hold_target).
                #
                # Without this the logic is not merely unhelpful, it actively
                # fights the brewer. Below `threshold - exit_drop` the boil
                # latch releases and heat_percent reverts to Max_Output, so a
                # kettle cooled to a 170F hop stand is driven at FULL POWER
                # back towards a boil. The comment on the latch release names
                # "a chiller started" as a trigger, which is exactly this case
                # described and then answered backwards.
                #
                # Held with a PID (_HoldPID), as the HLT's PIDBoil holds its
                # target: a hop stand only needs not to fall below, but a mash
                # wants tight regulation, and switching the element on and off
                # sits up to 4 F under the target.
                #
                # Read every pass, so a target changed mid-boil - by the
                # brewer, or a Whirlpool step's Heat and hold - takes over at
                # the next control decision. A Boil step sets its own target
                # as it starts heating, so no hold from an earlier stand can
                # reach a boil.
                hold_target = self._hold_target(threshold, exit_drop)
                if hold_target is not None:
                    boil_latched = False
                    announced_boil = False
                    if hold_pid is None:
                        hold_pid = _HoldPID(
                            *gains,
                            band=self.INTEGRAL_BAND_C * degree_ratio,
                            window=self.DERIVATIVE_WINDOW_S,
                            sample=sample_time,
                            max_output=max_output,
                        )
                    if announced_hold != hold_target:
                        announced_hold = hold_target
                        self._tell(
                            getattr(self.kettle, "name", "Kettle"),
                            "Holding {:.1f}, its target, instead of boiling: "
                            "a PID sets the element's power, up to {}%. To "
                            "boil, set its target to {:.1f} or above.".format(
                                hold_target, max_output, threshold - exit_drop,
                            ),
                        )
                    # Far below the target the PID asks for more than
                    # Max_Output and climbs at it; nearer, it eases off,
                    # braking on the wort's rise so the element's heat and the
                    # probe's lag don't carry the wort past - let alone back
                    # to a boil. Above the target it is off. A pause stops the
                    # integral building up while the element can't heat.
                    heat_percent = int(round(hold_pid.output(
                        current_temp, hold_target,
                        integrate=self._paused_since is None,
                    )))
                else:
                    announced_hold = None
                    hold_pid = None

                    if current_temp >= threshold:
                        boil_latched = True
                    elif (boil_latched and current_temp < threshold - exit_drop
                          and self._keep_boiling()):
                        # Genuinely off the boil - a lid opened, a chiller
                        # started, or the element failed - so go back to
                        # getting it there.
                        #
                        # Only when keeping it at the boil. With Keep_Boiling
                        # No the brewer has said the boil power alone drives
                        # the wort once it has boiled, and may fall: the latch
                        # stays until AUTO stops, or a hold takes over. Read
                        # each pass, so ticking it again while the wort is
                        # well below brings full power back at once.
                        boil_latched = False
                        announced_boil = False

                    if boil_latched:
                        heat_percent = self._boil_power()
                        if not announced_boil:
                            announced_boil = True
                            self._tell(
                                getattr(self.kettle, "name", "Boil"),
                                "Boiling at {:.1f}. Holding {}% - adjust from "
                                "the dashboard.".format(
                                    current_temp, heat_percent
                                ),
                            )
                    else:
                        heat_percent = max_output
                        announced_boil = False

                # Paused (set_paused): the element stays off whatever the above
                # decided. Everything else carried on - the latch, the
                # announcements - so carrying on goes straight back to what
                # AUTO would be doing by then.
                if self._paused_since is not None:
                    heat_percent = 0
                    self._warn_if_pause_forgotten()

                # Drive the actor from what it IS doing, not from what this loop
                # last told it to do.
                #
                # A loop that tracks heater_is_on and heat_percent_old in locals
                # and commands only on a believed transition goes wrong the
                # moment the element is switched off from the dashboard: it
                # still believes the element is on, so it never commands again,
                # and the element stays dark while the logic thinks it is
                # driving it, until the brewer stops and restarts the automation.
                #
                # Acting only when the software believes the actor is already on
                # makes switching off a no-op exactly when it is needed. Belief
                # about hardware goes stale the moment anything else touches it,
                # and on a rig the brewer is something else that touches it.
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
            # However the loop ended, a pause ends with it. After the element.
            await self._end_pause()


class _HoldPID:
    """The PID that holds a target below the boil (BoilPower.DEFAULT_P_C).

    PIDArduino's arithmetic, as PIDBoil's: the integral stops building up
    when it would push further into a rail the output is already against,
    and the D term acts on the readings rather than the error, so a new
    target doesn't kick the element. Two changes, for a kettle whose probe is
    a minute behind the wort: the integral builds up only within `band` of
    the target (BoilPower.INTEGRAL_BAND_C), and the rate the D term brakes on
    is a straight line through the last `window` seconds of readings
    (BoilPower.DERIVATIVE_WINDOW_S).

    Time is counted in control passes of `sample` brewing seconds, as
    PIDArduino counts it, so it needs no clock and gives the same result at
    any simulator speed.
    """

    def __init__(self, kp, ki, kd, band, window, sample, max_output):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.band = band
        self.window = window
        self.sample = sample
        self.max_output = max_output
        self.integral = 0.0
        self.last_output = 0.0
        self._t = 0.0
        self._readings = []

    def forget_readings(self):
        """A pass with no reading: the rise can't be told across the gap."""
        self._readings = []

    def rise(self):
        """Degrees per second the readings are rising, over the window: 0
        until they span half of it. A slope through a few seconds of
        readings is mostly the probe's steps of 0.11 F."""
        points = self._readings
        n = len(points)
        if n < 3 or points[-1][0] - points[0][0] < self.window / 2:
            return 0.0
        mean_t = sum(t for t, _ in points) / n
        mean_x = sum(x for _, x in points) / n
        spread = sum((t - mean_t) ** 2 for t, _ in points)
        if spread <= 0:
            return 0.0
        return sum((t - mean_t) * (x - mean_x) for t, x in points) / spread

    def output(self, reading, target, integrate=True):
        """The element's power for this pass, 0 to max_output."""
        self._t += self.sample
        self._readings.append((self._t, reading))
        oldest = self._t - self.window
        self._readings = [p for p in self._readings if p[0] >= oldest]
        error = target - reading
        into_rail = ((self.last_output >= self.max_output and error > 0)
                     or (self.last_output <= 0 and error < 0))
        if integrate and abs(error) <= self.band and not into_rail:
            self.integral += self.ki * self.sample * error
            self.integral = min(self.max_output, max(0.0, self.integral))
        out = self.kp * error + self.integral - self.kd * self.rise()
        self.last_output = min(self.max_output, max(0.0, out))
        return self.last_output


def _boiling_in(settings, unit):
    """Where a BoilPower kettle boils: Boil_Threshold in `settings`, else
    99 C / 210.2 F in `unit` ("C" or "F"). Never raises: it is free text."""
    if str(unit).strip().upper().startswith("C"):
        default = BoilPower.DEFAULT_THRESHOLD_C
    else:
        default = BoilPower.DEFAULT_THRESHOLD_C * 9.0 / 5.0 + 32.0
    try:
        raw = settings.get("Boil_Threshold", None)
    except Exception:  # noqa: BLE001
        return default
    if raw is None or str(raw).strip() == "":
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or not math.isfinite(parsed):
        logging.warning("BoilPower: ignoring Boil_Threshold %r, using %.1f", raw, default)
        return default
    return parsed


def _hold_in(settings):
    """The hold kept in `settings` - a kettle's props, or the BoilPower
    settings a kettle put aside for another logic - or None. Blank is none."""
    try:
        raw = settings.get("Hold_Target", None)
    except Exception:  # noqa: BLE001
        return None
    if raw is None or str(raw).strip() == "":
        return None
    return raw


def _clear_kettle_hold(kettle, unit="C"):
    """Clear the hold kept with `kettle`.

    A hold is the kettle's target, below where it boils: put back to the boil
    point. And a Hold_Target setting left by a version before 0.0.3.post7, which
    keeps a hold apart from the target, is cleared too - in the kettle's settings
    and in any BoilPower settings put aside while it runs another logic.

    (the hold that was in force or None, whether anything was changed)."""
    in_force, cleared = None, False
    if getattr(kettle, "type", None) == "BoilPower":
        props = getattr(kettle, "props", None)
        if props is not None and _hold_in(props) is not None:
            in_force = _hold_in(props)
            props["Hold_Target"] = ""
            cleared = True
        if props is not None:
            boiling = _boiling_in(props, unit)
            drop = BoilPower.BOIL_EXIT_DROP_C * (1.0 if str(unit).upper().startswith("C") else 1.8)
            try:
                target = float(getattr(kettle, "target_temp", None) or 0)
            except (TypeError, ValueError):
                target = 0.0
            if math.isfinite(target) and 0 < target < boiling - drop:
                in_force = target
                kettle.target_temp = boiling
                cleared = True
        # Kettles start switched off, so there is normally no logic yet.
        logic = getattr(kettle, "instance", None)
        if logic is not None:
            logic_props = getattr(logic, "props", None)
            if logic_props is not None and logic_props is not props and _hold_in(logic_props) is not None:
                logic_props["Hold_Target"] = ""
    # Put aside while the kettle runs another logic: switching back to
    # BoilPower would bring it back.
    aside = (getattr(kettle, "props_by_type", None) or {}).get("BoilPower")
    if isinstance(aside, dict) and _hold_in(aside) is not None:
        aside["Hold_Target"] = ""
        cleared = True
        logging.info("BoilPower: cleared the hold put aside on %s",
                     getattr(kettle, "name", "a kettle"))
    return in_force, cleared


def _tell_hold_cleared(cbpi, kettle_name, held):
    """Tell the brewer the restart cleared a hold - in a prompt kept until
    it is acknowledged, where CraftBeerPi can keep one, so a page opened
    after the restart shows it."""
    try:
        shown = "{:.1f}".format(float(held))
    except (TypeError, ValueError):
        shown = str(held)
    try:
        unit = " \u00b0F" if str(cbpi.config.get("TEMP_UNIT", "C")).upper() == "F" else " \u00b0C"
    except Exception:  # noqa: BLE001
        unit = ""
    message = (
        "CraftBeerPi restarted, and that cleared the hold at {}{}: the target "
        "is back to the boil, so switched on now, {} heats to a boil. To hold "
        "again, set its target below the boil."
        .format(shown, unit, kettle_name)
    )
    logging.warning("%s: %s", kettle_name, message)
    try:
        kept = "keep" in inspect.signature(cbpi.notify).parameters
    except (TypeError, ValueError):
        kept = False
    try:
        options = dict(keep=True) if kept else {}
        cbpi.notify(kettle_name, message, NotificationType.WARNING,
                    action=[NotificationAction("OK")], **options)
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: could not say the hold was cleared: %s", e)


async def _clear_holds_after_restart(cbpi):
    """A restart of CraftBeerPi clears every BoilPower hold, and says so.

    A hold is kept with the kettle - its target, below the boil - so that
    switching the kettle off and on keeps it. A restart is different: the
    server may have been down for minutes or a night, and a hold left in the
    kettle's target waits for the next time the kettle is switched on -
    perhaps the next brew. So a restart puts the target back to the boil, and
    the brewer is told which hold went, in time to set it again.

    A start-up hook: run once everything is loaded, before any page can
    connect. Never raises - a hook that raised would stop CraftBeerPi starting.
    """
    try:
        unit = str(cbpi.config.get("TEMP_UNIT", "C") or "C")
    except Exception:  # noqa: BLE001
        unit = "C"
    try:
        told, changed = [], False
        for kettle in list(getattr(cbpi.kettle, "data", None) or []):
            try:
                held, cleared = _clear_kettle_hold(kettle, unit)
            except Exception as e:  # noqa: BLE001
                logging.warning("BoilPower: could not clear the hold on %s: %s",
                                getattr(kettle, "name", "a kettle"), e)
                continue
            changed = changed or cleared
            if held is not None:
                told.append((getattr(kettle, "name", None) or "Boil kettle", held))
        if changed:
            await cbpi.kettle.save()
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: could not clear holds after the restart: %s", e)
        return
    for kettle_name, held in told:
        _tell_hold_cleared(cbpi, kettle_name, held)


async def _clear_pauses_after_restart(cbpi):
    """A restart ends every pause. Kettles start switched off, so a Paused_At
    still in a kettle's settings is from a run the server went down in. Quiet:
    the heat it paused went with the server. Never raises."""
    try:
        changed = False
        for kettle in list(getattr(cbpi.kettle, "data", None) or []):
            if getattr(kettle, "type", None) != "BoilPower":
                continue
            props = getattr(kettle, "props", None)
            if props is None or (props.get("Paused_At", None) in (None, "")
                                 and props.get("Paused", None) in (None, "", "No")):
                continue
            props["Paused_At"] = ""
            props["Paused"] = "No"
            changed = True
            logging.info("BoilPower: the restart ended the pause on %s",
                         getattr(kettle, "name", "a kettle"))
        if changed:
            await cbpi.kettle.save()
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: could not clear pauses after the restart: %s", e)


def _usual_power(settings):
    """Usual_Boil_Power in `settings`, a whole percent 0-100, or None: blank,
    or nothing a percentage can be read from. Never raises."""
    try:
        raw = settings.get("Usual_Boil_Power", None)
    except Exception:  # noqa: BLE001
        return None
    if raw is None or str(raw).strip() == "":
        return None
    return BoilPower._clamp_percent(raw, None)


async def _usual_power_for_the_recipe(cbpi):
    """Each BoilPower kettle with a Usual_Boil_Power: Boil_Power back to it,
    and said, as the recipe starts. Never raises.

    Each recipe starts at the usual boil power - at the recipe's start, not
    the boil's, so a power set during the sparge or the "Ready to boil?" hold
    is kept for that boil. Said only when it changes something.
    """
    told = []
    try:
        changed = False
        for kettle in list(getattr(cbpi.kettle, "data", None) or []):
            if getattr(kettle, "type", None) != "BoilPower":
                continue
            props = getattr(kettle, "props", None)
            usual = _usual_power(props) if props is not None else None
            if usual is None:
                continue
            logic = getattr(kettle, "instance", None)
            was = getattr(logic, "_boil_power_override", None)
            if was is None:
                was = BoilPower._clamp_percent(
                    props.get("Boil_Power", BoilPower.DEFAULT_BOIL_POWER),
                    BoilPower.DEFAULT_BOIL_POWER)
            if was == usual:
                continue
            props["Boil_Power"] = usual
            if logic is not None:
                # A running logic reads its override first (_boil_power).
                logic._boil_power_override = usual
                logic_props = getattr(logic, "props", None)
                if logic_props is not None and logic_props is not props:
                    logic_props["Boil_Power"] = usual
            changed = True
            told.append((getattr(kettle, "name", None) or "Boil kettle", usual, was))
        if changed:
            await cbpi.kettle.save()
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: could not put the usual boil power back: %s", e)
        return
    for name, usual, was in told:
        message = ("Boil power back to your usual {}% (was {}%): the recipe "
                   "started.".format(usual, was))
        logging.info("%s: %s", name, message)
        try:
            cbpi.notify(name, message, NotificationType.INFO)
        except Exception:  # noqa: BLE001
            pass


class _RecipeStart:
    """Told about the brew steps (StepController.add_listener); puts the usual
    boil power back when a recipe starts.

    A recipe starting is its first step starting while it was still waiting.
    Not that step stopped and started again, nor resumed after a restart: they
    carry on a brew, and a power the brewer has set since must stand. The
    listener's snapshot shows the step running either way, so each step's
    status is remembered from one event to the next, beginning with what was
    there as CraftBeerPi started. A step never seen before is new, so waiting.
    """

    def __init__(self, cbpi, snapshot=None):
        self.cbpi = cbpi
        self.status = self._statuses(snapshot)

    @staticmethod
    def _statuses(snapshot):
        found = {}
        for step in (snapshot or {}).get("steps") or []:
            if isinstance(step, dict) and step.get("id") is not None:
                found[step["id"]] = step.get("status")
        return found

    def __call__(self, event, snapshot):
        fresh = False
        try:
            steps = (snapshot or {}).get("steps") or []
            if event == "step_started" and steps and isinstance(steps[0], dict):
                first = steps[0].get("id")
                fresh = (snapshot.get("step_id") == first
                         and self.status.get(first, "I") == "I")
            self.status = self._statuses(snapshot)
        except Exception as e:  # noqa: BLE001 - a listener never stops a brew
            logging.warning("BoilPower: could not follow the brew steps: %s", e)
            return None
        # A coroutine: the step controller runs it as a task of its own.
        return _usual_power_for_the_recipe(self.cbpi) if fresh else None


def _listen_for_recipe_start(cbpi):
    """Follow the brew steps for the usual boil power. -> the listener, or
    None on a CraftBeerPi without step listeners, where Usual_Boil_Power is
    not used. Never raises."""
    step = getattr(cbpi, "step", None)
    add = getattr(step, "add_listener", None)
    if not callable(add):
        logging.info("BoilPower: this CraftBeerPi cannot say when a recipe "
                     "starts, so Usual_Boil_Power is not used")
        return None
    try:
        snapshot = step.snapshot() if callable(getattr(step, "snapshot", None)) else None
    except Exception:  # noqa: BLE001
        snapshot = None
    try:
        listener = _RecipeStart(cbpi, snapshot)
        add(listener)
        return listener
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: could not follow the brew steps: %s", e)
        return None


def setup(cbpi):
    cbpi.plugin.register("BoilPower", BoilPower)
    try:
        async def clear_holds(app):
            await _clear_holds_after_restart(cbpi)

        cbpi.app.on_startup.append(clear_holds)
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: a restart will not clear holds: %s", e)
    try:
        async def after_restart(app):
            await _clear_pauses_after_restart(cbpi)
            _listen_for_recipe_start(cbpi)

        cbpi.app.on_startup.append(after_restart)
    except Exception as e:  # noqa: BLE001
        logging.warning("BoilPower: a restart will not clear pauses: %s", e)

