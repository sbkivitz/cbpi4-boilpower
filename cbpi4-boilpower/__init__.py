"""Full power to the boil, then a fixed duty the brewer sets from the dashboard;
or, given a target below the boil, hold it there.

A boil does not need a PID. Once wort is boiling its temperature stops being a
control variable - it sits at the boiling point whatever you do - and the only
thing left to choose is how hard it boils. PIDBoil models this as a special
case at the end of a PID; this plugin is that case on its own:

    below the boil point (Boil_Threshold)   ->  Max_Output, get there
    at or above it                          ->  Boil_Power, hold that duty

The kettle's target says which job it has, as on any kettle (Scott, 10 Nov:
"Set the boil kettle's target to 170 F, like the HLT, and it holds there"):

    no target, or one at the boil   ->  boil, as above
    a target below the boil         ->  hold it: a whirlpool, a hop stand,
                                        warm water

"At the boil" is within BOIL_EXIT_DROP_C of Boil_Threshold or above, so a Boil
step's 210 F or 212 F means boil wherever the threshold sits. Boil_Threshold is
the one place the boil point is set - at altitude, set it to where your wort
boils - and the kettle's target no longer moves it. Until 0.0.3.post7 the
target WAS the boil point, and a hold was a separate Hold_Target setting; that
is gone, and a restart clears one left in a kettle's settings.

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
#: message, in the dashboard agent's words, after "HLT pump still running".
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
        description="Element duty while heating up to the boil, or to a hold "
                    "from well below it."),
    Property.Number(
        label="Hold_Power", configurable=True, default_value=30,
        description="Element duty while holding a target BELOW the boil - a "
                    "whirlpool or hop stand - once the wort reaches it. Set "
                    "the kettle's target below the boil and this logic holds "
                    "it instead of boiling. Modest on purpose: a hold replaces "
                    "standing losses, and full power near the target "
                    "overshoots. More than 4 C / 7.2 F below the hold it heats "
                    "at Max_Output, easing to this as it nears the hold. Raise "
                    "it for a poorly insulated vessel."),
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
    """Ramp at full power, then hold a chosen duty."""

    # Defaults are authored in Celsius and converted, because a bare number in
    # a property default is a Celsius value on a Fahrenheit rig - which is how
    # a 20 C ambient became 20 F elsewhere in this project.
    DEFAULT_THRESHOLD_C = 99.0
    DEFAULT_BOIL_POWER = 85
    # Duty while holding below boiling - a whirlpool or hop stand. Modest on
    # purpose: a hold replaces standing losses. Raise it for a poorly insulated
    # vessel. Far below the hold it climbs at Max_Output first (HOLD_CLIMB_C).
    DEFAULT_HOLD_POWER = 30
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

    # How far below a hold the wort must be for the hold to climb back at
    # Max_Output. Within this of the target it eases off in proportion, down
    # to Hold_Power at the target (_hold_climb_power).
    #
    # Scott, 4 Nov, on a whirlpool that had cooled too far: "I'd like a prompt
    # to ask if I want to raise temp back to whirlpool temp and hold". Hold_Power
    # replaces standing losses; it does not climb. On the simulator's boil
    # kettle - 5.5 kW, 40 litres, losing about 1.4 kW at 170 F - 30% would
    # take about an hour to climb back from 158 F, longer than the stand; full
    # power takes about five minutes. And then, of the flat 30% that took over
    # 7.2 F short: "I should not be forced to use the hold_power value".
    #
    # Never full power near the target: within this of it the power falls
    # away as the wort closes in, and at the hold it is the gentle Hold_Power
    # it always was, so the element's heat and the probe's lag carry the wort
    # no further past the hold than before - let alone back to a boil.
    HOLD_CLIMB_C = 4.0

    # How long the heat may stay paused before the brewer is warned, in
    # brewing seconds. Scott, 10 Nov: a forgotten pause gets a warning on every
    # page after 2 minutes. A boil-over is dealt with in seconds; two minutes
    # off is a boil going cold with nobody watching.
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

    def _keep_boiling(self):
        """Keep_Boiling, read fresh each pass: True unless it says No.

        Yes is today's boil: the latch lets go when the wort falls
        BOIL_EXIT_DROP_C below the boil, and full power brings it back. No
        keeps the latch once the wort has boiled, so Boil_Power alone drives
        it. Scott, 10 Nov, who had set 2% and watched the kettle cycle between
        full power and 2%: "let the boil power fully drive the temp (ok to fall
        if I set it too low %)".
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

    def _hold_power(self):
        """The duty a hold settles to at its target, read fresh each pass.

        Not Max_Output near the target. An earlier version of this branch used
        full output whenever the reading fell below the hold band, and a test
        caught it: on a full kettle with a multi-kilowatt element, 100% to
        close a six degree gap overshoots straight back towards a boil. That is
        the failure this whole branch exists to prevent, reintroduced one layer
        down. Full output is used only to climb from well below the hold
        (HOLD_CLIMB_C); from there the duty eases down to this one at the
        target (_hold_climb_power).

        Deliberately not Boil_Power either. That duty is chosen to sustain a
        rolling boil, which is far more than replacing standing losses at a hop
        stand.

        A separate knob rather than a derived fraction, because how hard to
        hold depends on the vessel, its insulation and the element - none of
        which this plugin can know.
        """
        return self._clamp_percent(
            self.props.get("Hold_Power", self.DEFAULT_HOLD_POWER),
            self.DEFAULT_HOLD_POWER,
        )

    def _hold_climb_power(self, below, climb_band, max_output):
        """The duty for wort `below` the hold, within climb_band of it.

        In proportion: Max_Output at climb_band below, easing to Hold_Power at
        the hold. Scott, 4 Nov, of a whirlpool cooled too far: "if I'm
        attempting to heat back up from where I over-cooled, I should not be
        forced to use the hold_power value". A flat Hold_Power from 7.2 F
        below only just beat the losses, and on the simulator's kettle the
        last 5 F took most of a 20 minute stand.

        Proportional, like the P of a PID, with no settings of its own to tune.
        Truncated, so it never reaches Max_Output inside the band. At the hold
        it is Hold_Power, as it always was, so it carries the wort no further
        past it than before.
        """
        hold = self._hold_power()
        share = min(1.0, max(0.0, below / climb_band)) if climb_band > 0 else 1.0
        return int(hold + (max_output - hold) * share)

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

        None is "no target", and BoilPower boils without one. Infinity and NaN
        are no target either: an infinite setpoint once became a boil point
        the wort could never reach, and the element held Max_Output for good
        (cbpi-testbench test_safety_invariants, I5).
        """
        try:
            value = float(self.get_kettle_target_temp(self.id) or 0)
        except Exception:  # noqa: BLE001
            # Broad on purpose: this calls into the core, and through it into
            # whatever is registered. Anything raised here used to escape the
            # control loop, which then died silently with the interface still
            # showing the logic running.
            logging.warning("BoilPower: could not read the kettle's target")
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        return value

    def _hold_target(self, threshold, exit_drop):
        """The temperature to hold, or None for a boil.

        The kettle's target, when it is more than exit_drop below the boil
        point `threshold`. Scott, 10 Nov: "Set the boil kettle's target to
        170 F, like the HLT, and it holds there". The HLT's PIDBoil works the
        same way: it holds any target, and switches to its boil power only once
        the wort reaches its own Boil_Threshold.

        A target within exit_drop of the boil, or above it, is a boil - a Boil
        step's 210 F or 212 F, or a 99 C one under a 99.5 C threshold - and so
        is no target at all. A hold that close to boiling could not be told
        apart from a boil, and the hold's own band would put it at the boil.

        This reverses a decision. Until 0.0.3.post7 the target WAS the boil
        point, so "my boil is at 205" - at altitude, or for a gentler boil -
        was said with the target, and a hold had to be asked for separately,
        through Hold_Target. Boil_Threshold says where the boil is now, and a
        gentler boil is Boil_Power.
        """
        target = self._target()
        if target is None or target >= threshold - exit_drop:
            return None
        return target

    def _threshold(self):
        """Where full power ends and the boil power begins: the boil point,
        Boil_Threshold, read fresh each pass. Never raises.

        It used to be the kettle's target when there was one, so the target
        was the boil point. Now the target says boil or hold (_hold_target),
        and only Boil_Threshold moves the boil point - set it with Boil at,
        running or not.
        """
        return self._configured_boiling()

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
        timer carry on (Scott, 10 Nov: the boil's timer keeps counting); only
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
            # The hold last announced, so a changed one is said again.
            announced_hold = None
            # What this loop last commanded, used only when the actor cannot be
            # read. Starts as 0 because the line above just de-energized it.
            commanded = 0
            # Once boiling, stay boiling until the wort genuinely comes off it.
            boil_latched = False
            exit_drop = self.BOIL_EXIT_DROP_C * degree_ratio
            climb_band = self.HOLD_CLIMB_C * degree_ratio

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
                    await clock.sleep(sample_time)
                    continue

                # A target BELOW the boil is a request to hold that
                # temperature, not to boil - a whirlpool or hop stand
                # (_hold_target).
                #
                # Without this the logic is not merely unhelpful, it actively
                # fights the brewer. Below `threshold - exit_drop` the boil
                # latch releases and heat_percent reverts to Max_Output, so a
                # kettle cooled to a 170F hop stand is driven at FULL POWER
                # back towards a boil. The comment on the latch release names
                # "a chiller started" as a trigger, which is exactly this case
                # described and then answered backwards.
                #
                # Bang-bang rather than PID: a hop stand wants "do not fall
                # below the target", not tight regulation, and the vessel's
                # thermal mass does most of the work. The same band that
                # decides the wort has come off the boil is reused as the
                # hysteresis, so there is one notion of "meaningfully below"
                # in this file rather than two.
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
                    if announced_hold != hold_target:
                        announced_hold = hold_target
                        self._tell(
                            getattr(self.kettle, "name", "Kettle"),
                            "Holding {:.1f}, its target, instead of boiling. "
                            "Below it, it heats at {}% while the wort is more "
                            "than {:.1f} below, easing to {}% as it reaches "
                            "the hold; then it rests until the wort is {:.1f} "
                            "below. To boil, set its target to {:.1f} or "
                            "above.".format(
                                hold_target, max_output, climb_band,
                                self._hold_power(), exit_drop,
                                threshold - exit_drop,
                            ),
                        )
                    # Heat only when below the band; off at or above target.
                    # Overshoot on a vessel this size is slow, and a hop stand
                    # a few degrees high is mild - sitting at full power is not.
                    # Far below, climb at full power to within climb_band
                    # first (HOLD_CLIMB_C); within it, ease off in proportion
                    # to how far below the wort still is, down to Hold_Power
                    # at the hold (_hold_climb_power). Hold_Power alone
                    # barely beats the losses of a kettle that has cooled well
                    # past the hold, and the last few degrees crawled.
                    below = hold_target - current_temp
                    if below >= climb_band:
                        heat_percent = max_output
                    elif below <= 0:
                        heat_percent = 0
                    elif below > exit_drop or (commanded or 0) > 0:
                        # Below the band, or inside it and still heating:
                        # carry on, rather than chattering the element every
                        # pass - eased, never at a climb's full power, even if
                        # the wort crossed both bands between two readings.
                        heat_percent = self._hold_climb_power(below, climb_band, max_output)
                    else:
                        heat_percent = 0
                else:
                    announced_hold = None

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
            # However the loop ended, a pause ends with it. After the element.
            await self._end_pause()


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
    point. And a Hold_Target setting left from before 0.0.3.post7, when a hold
    was kept apart from the target, is cleared too - in the kettle's settings
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

    Scott, 3 Nov: "Restarts should clear the hold." A hold is kept with the
    kettle - its target, below the boil - so that switching the kettle off and
    on keeps it. A restart is different: the server may have been down for
    minutes or a night, and a hold left in the kettle's target waits for the
    next time the kettle is switched on - perhaps the next brew. So a restart
    puts the target back to the boil, and the brewer is told which hold went,
    in time to set it again.

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

    Scott, 10 Nov: each recipe starts at the usual boil power - his choice, of
    "each boil starts at your usual power", moved to the recipe's start so a
    power set during the sparge or the "Ready to boil?" hold is kept for that
    boil. Said only when it changes something.
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

