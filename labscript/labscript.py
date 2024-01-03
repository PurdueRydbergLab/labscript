#####################################################################
#                                                                   #
# /labscript.py                                                     #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the program labscript, in the labscript      #
# suite (see http://labscriptsuite.org), and is licensed under the  #
# Simplified BSD License. See the license.txt file in the root of   #
# the project for the full license.                                 #
#                                                                   #
#####################################################################

import builtins
import contextlib
import os
import sys
import subprocess
import keyword
import threading
from functools import lru_cache
import numpy as np

# Notes for v3
#
# Anything commented with TO_DELETE:runmanager-batchompiler-agnostic 
# can be removed with a major version bump of labscript (aka v3+)
# We leave it in now to maintain backwards compatibility between new labscript
# and old runmanager.
# The code to be removed relates to the move of the globals loading code from
# labscript to runmanager batch compiler.

from .base import Device
from .compiler import compiler
from .core import (
    ClockLine,
    IntermediateDevice,
    Pseudoclock,
    PseudoclockDevice,
    TriggerableDevice,
)
from .utils import (
    LabscriptError,
    bitfield,
    fastflatten,
    max_or_zero,
    set_passed_properties
)
import labscript_utils.h5_lock, h5py
import labscript_utils.properties
from labscript_utils.filewatcher import FileWatcher

# This imports the default Qt library that other labscript suite code will
# import as well, since it all uses qtutils. By having a Qt library already
# imported, we ensure matplotlib (imported by pylab) will notice this and use
# the same Qt library and API version, and therefore not conflict with any
# other code is using:
import qtutils

from pylab import *

from labscript_utils import dedent
from labscript_utils.properties import set_attributes

import labscript.functions as functions
try:
    from labscript_utils.unitconversions import *
except ImportError:
    sys.stderr.write('Warning: Failed to import unit conversion classes\n')

ns = 1e-9
us = 1e-6
ms = 1e-3
s = 1
Hz = 1
kHz = 1e3
MHz = 1e6
GHz = 1e9

# Create a reference to the builtins dict 
# update this if accessing builtins ever changes
_builtins_dict = builtins.__dict__
    
# Startupinfo, for ensuring subprocesses don't launch with a visible command window:
if os.name=='nt':
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= 1 #subprocess.STARTF_USESHOWWINDOW # This variable isn't defined, but apparently it's equal to one.
else:
    startupinfo = None


class config(object):
    suppress_mild_warnings = True
    suppress_all_warnings = False
    compression = 'gzip'  # set to 'gzip' for compression 


@contextlib.contextmanager()
def suppress_mild_warnings(state=True):
    """A context manager which modifies config.suppress_mild_warnings

    Allows the user to suppress (or show) mild warnings for specific lines. Useful when
    you want to hide/show all warnings from specific lines.

    Arguments:
        state (bool): The new state for ``config.suppress_mild_warnings``. Defaults to
        ``True`` if not explicitly provided.
    """
    previous_warning_setting = config.suppress_mild_warnings
    config.suppress_mild_warnings = state
    yield
    config.suppress_mild_warnings = previous_warning_setting


@contextlib.contextmanager()
def suppress_all_warnings(state=True):
    """A context manager which modifies config.suppress_all_warnings

    Allows the user to suppress (or show) all warnings for specific lines. Useful when
    you want to hide/show all warnings from specific lines.
    
    Arguments:
        state (bool): The new state for ``config.suppress_all_warnings``. Defaults to
        ``True`` if not explicitly provided.
    """
    previous_warning_setting = config.suppress_all_warnings
    config.suppress_all_warnings = state
    yield
    config.suppress_all_warnings = previous_warning_setting


no_warnings = suppress_all_warnings # Historical alias


class Output(Device):
    """Base class for all output classes."""
    description = "generic output"
    allowed_states = None
    dtype = np.float64
    scale_factor = 1

    @set_passed_properties(property_names={})
    def __init__(
        self,
        name,
        parent_device,
        connection,
        limits=None,
        unit_conversion_class=None,
        unit_conversion_parameters=None,
        default_value=None,
        **kwargs
    ):
        """Instantiate an Output.

        Args:
            name (str): python variable name to assign the Output to.
            parent_device (:obj:`IntermediateDevice`): Parent device the output
                is connected to.
            connection (str): Channel of parent device output is connected to.
            limits (tuple, optional): `(min,max)` allowed for the output.
            unit_conversion_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional):
                Unit concersion class to use for the output.
            unit_conversion_parameters (dict, optional): Dictonary or kwargs to
                pass to the unit conversion class.
            default_value (float, optional): Default value of the output if no
                output is commanded.
            **kwargs: Passed to :meth:`Device.__init__`.

        Raises:
            LabscriptError: Limits tuple is invalid or unit conversion class
                units don't line up.
        """
        Device.__init__(self,name,parent_device,connection, **kwargs)

        self.instructions = {}
        self.ramp_limits = [] # For checking ramps don't overlap
        if default_value is not None:
            self.default_value = default_value
        if not unit_conversion_parameters:
            unit_conversion_parameters = {}
        self.unit_conversion_class = unit_conversion_class
        self.set_properties(
            unit_conversion_parameters,
            {"unit_conversion_parameters": list(unit_conversion_parameters.keys())}
        )

        # Instantiate the calibration
        if unit_conversion_class is not None:
            self.calibration = unit_conversion_class(unit_conversion_parameters)
            # Validate the calibration class
            for units in self.calibration.derived_units:
                # Does the conversion to base units function exist for each defined unit
                # type?
                if not hasattr(self.calibration, f"{units}_to_base"):
                    raise LabscriptError(
                        f'The function "{units}_to_base" does not exist within the '
                        f'calibration "{self.unit_conversion_class}" used in output '
                        f'"{self.name}"'
                    )
                # Does the conversion to base units function exist for each defined unit
                # type?
                if not hasattr(self.calibration, f"{units}_from_base"):
                    raise LabscriptError(
                        f'The function "{units}_from_base" does not exist within the '
                        f'calibration "{self.unit_conversion_class}" used in output '
                        f'"{self.name}"'
                    )

        # If limits exist, check they are valid
        # Here we specifically differentiate "None" from False as we will later have a
        # conditional which relies on self.limits being either a correct tuple, or
        # "None"
        if limits is not None:
            if not isinstance(limits, tuple) or len(limits) != 2:
                raise LabscriptError(
                    f'The limits for "{self.name}" must be tuple of length 2. '
                    'Eg. limits=(1, 2)'
                )
            if limits[0] > limits[1]:
                raise LabscriptError(
                    "The first element of the tuple must be lower than the second "
                    "element. Eg limits=(1, 2), NOT limits=(2, 1)"
                )
        # Save limits even if they are None        
        self.limits = limits

    @property
    def clock_limit(self):
        """float: Returns the parent clock line's clock limit."""
        parent = self.parent_clock_line
        return parent.clock_limit

    @property
    def trigger_delay(self):
        """float: The earliest time output can be commanded from this device after a trigger.
        This is nonzeo on secondary pseudoclocks due to triggering delays."""
        parent = self.pseudoclock_device
        if parent.is_master_pseudoclock:
            return 0
        else:
            return parent.trigger_delay

    @property
    def wait_delay(self):
        """float: The earliest time output can be commanded from this device after a wait.
        This is nonzeo on secondary pseudoclocks due to triggering delays and the fact
        that the master clock doesn't provide a resume trigger to secondary clocks until
        a minimum time has elapsed: compiler.wait_delay. This is so that if a wait is 
        extremely short, the child clock is actually ready for the trigger.
        """
        delay = compiler.wait_delay if self.pseudoclock_device.is_master_pseudoclock else 0
        return self.trigger_delay + delay

    def get_all_outputs(self):
        """Get all children devices that are outputs.

        For ``Output``, this is `self`.

        Returns:
            list: List of children :obj:`Output`.
        """
        return [self]
            
    def apply_calibration(self,value,units):
        """Apply the calibration defined by the unit conversion class, if present.

        Args:
            value (float): Value to apply calibration to.
            units (str): Units to convert to. Must be defined by the unit
                conversion class.

        Returns:
            float: Converted value.

        Raises:
            LabscriptError: If no unit conversion class is defined or `units` not
                in that class.
        """
        # Is a calibration in use?
        if self.unit_conversion_class is None:
            raise LabscriptError(
                'You can not specify the units in an instruction for output '
                f'"{self.name}" as it does not have a calibration associated with it'
            )

        # Does a calibration exist for the units specified?
        if units not in self.calibration.derived_units:
            raise LabscriptError(
                f'The units "{units}" does not exist within the calibration '
                f'"{self.unit_conversion_class}" used in output "{self.name}"'
            )

        # Return the calibrated value
        return getattr(self.calibration,units+"_to_base")(value)

    def instruction_to_string(self,instruction):
        """Gets a human readable description of an instruction.

        Args:
            instruction (dict or str): Instruction to get description of,
                or a fixed instruction defined in :attr:`allowed_states`.

        Returns:
            str: Instruction description.
        """
        if isinstance(instruction, dict):
            return instruction["description"]
        elif self.allowed_states:
            return str(self.allowed_states[instruction])
        else:
            return str(instruction)

    def add_instruction(self, time, instruction, units=None):
        """Adds a hardware instruction to the device instruction list.

        Args:
            time (float): Time, in seconds, that the instruction begins.
            instruction (dict or float): Instruction to add.
            units (str, optional): Units instruction is in, if `instruction`
                is a `float`.

        Raises:
            LabscriptError: If time requested is not allowed or samplerate
                is too fast.
        """
        if not compiler.start_called:
            raise LabscriptError("Cannot add instructions prior to calling start()")
        # round to the nearest 0.1 nanoseconds, to prevent floating point
        # rounding errors from breaking our equality checks later on.
        time = round(time, 10)
        # Also round end time of ramps to the nearest 0.1 ns:
        if isinstance(instruction,dict):
            instruction["end time"] = round(instruction["end time"], 10)
            instruction["initial time"] = round(instruction["initial time"], 10)
        # Check that time is not negative or too soon after t=0:
        if time < self.t0:
            raise LabscriptError(
                f"{self.description} {self.name} has an instruction at t={time}s. "
                "Due to the delay in triggering its pseudoclock device, the earliest "
                f"output possible is at t={self.t0}."
            )
        # Check that this doesn't collide with previous instructions:
        if time in self.instructions.keys():
            if not config.suppress_all_warnings:
                current_value = self.instruction_to_string(self.instructions[time])
                new_value = self.instruction_to_string(
                    self.apply_calibration(instruction, units)
                    if units and not isinstance(instruction, dict) 
                    else instruction
                )
                sys.stderr.write(
                    f"WARNING: State of {self.description} {self.name} at t={time}s "
                    f"has already been set to {current_value}. Overwriting to "
                    f"{new_value}. (note: all values in base units where relevant)"
                    "\n"
                )
        # Check that ramps don't collide
        if isinstance(instruction, dict):
            # No ramps allowed if this output is on a slow clock:
            if not self.parent_clock_line.ramping_allowed:
                raise LabscriptError(
                    f"{self.description} {self.name} is on clockline that does not "
                    "support ramping. It cannot have a function ramp as an instruction."
                )
            for start, end in self.ramp_limits:
                if start < time < end or start < instruction["end time"] < end:
                    start_value = self.instruction_to_string(self.instructions[start])
                    new_value = self.instruction_to_string(instruction)
                    raise LabscriptError(
                        f"State of {self.description} {self.name} from t = {start}s to "
                        f"{end}s has already been set to {start_value}. Cannot set to "
                        f"{new_value} from t = {time}s to {instruction['end time']}s."
                    )
            self.ramp_limits.append((time, instruction["end time"]))
            # Check that start time is before end time:
            if time > instruction["end time"]:
                raise LabscriptError(
                    f"{self.description} {self.name} has been passed a function ramp "
                    f"{self.instruction_to_string(instruction)} with a negative "
                    "duration."
                )
            if instruction["clock rate"] == 0:
                raise LabscriptError("A nonzero sample rate is required.")
            # Else we have a "constant", single valued instruction
        else:
            # If we have units specified, convert the value
            if units is not None:
                # Apply the unit calibration now
                instruction = self.apply_calibration(instruction, units)
            # if we have limits, check the value is valid
            if self.limits:
                if (instruction < self.limits[0]) or (instruction > self.limits[1]):
                    raise LabscriptError(
                        f"You cannot program the value {instruction} (base units) to "
                        f"{self.name} as it falls outside the limits "
                        f"({self.limits[0]} to {self.limits[1]})"
                    )
        self.instructions[time] = instruction
    
    def do_checks(self, trigger_times):
        """Basic error checking to ensure the user's instructions make sense.

        Args:
            trigger_times (iterable): Times to confirm don't conflict with
                instructions.

        Raises:
            LabscriptError: If a trigger time conflicts with an instruction.
        """
        # Check if there are no instructions. Generate a warning and insert an
        # instruction telling the output to remain at its default value.
        if not self.instructions:
            if not config.suppress_mild_warnings and not config.suppress_all_warnings:
                sys.stderr.write(
                    f"WARNING: {self.name} has no instructions. It will be set to "
                    f"{self.instruction_to_string(self.default_value)} for all time.\n"
                )
            self.add_instruction(self.t0, self.default_value)  
        # Check if there are no instructions at the initial time. Generate a warning and insert an
        # instruction telling the output to start at its default value.
        if self.t0 not in self.instructions.keys():
            if not config.suppress_mild_warnings and not config.suppress_all_warnings:
               sys.stderr.write(
                    f"WARNING: {self.name} has no initial instruction. It will "
                    "initially be set to "
                    f"{self.instruction_to_string(self.default_value)}.\n"
                )
            self.add_instruction(self.t0, self.default_value) 
        # Check that ramps have instructions following them.
        # If they don't, insert an instruction telling them to hold their final value.
        for instruction in list(self.instructions.values()):
            if (
                isinstance(instruction, dict)
                and instruction["end time"] not in self.instructions.keys()
            ):
                self.add_instruction(
                    instruction["end time"],
                    instruction["function"](
                        instruction["end time"] - instruction["initial time"]
                    ),
                    instruction["units"],
                )
        # Checks for trigger times:
        for trigger_time in trigger_times:
            for t, inst in self.instructions.items():
                # Check no ramps are happening at the trigger time:
                if (
                    isinstance(inst, dict)
                    and inst["initial time"] < trigger_time
                    and inst["end time"] > trigger_time
                ):
                    raise LabscriptError(
                        f"{self.description} {self.name} has a ramp "
                        f"{inst['description']} from t = {inst['initial time']} to "
                        f"{inst['end time']}. This overlaps with a trigger at "
                        f"t={trigger_time}, and so cannot be performed."
                    )
                # Check that nothing is happening during the delay time after the trigger:
                if (
                    round(trigger_time, 10)
                    < round(t, 10)
                    < round(trigger_time + self.trigger_delay, 10)
                ):
                    raise LabscriptError(
                        f"{self.description} {self.name} has an instruction at t={t}. "
                        f"This is too soon after a trigger at t={trigger_time}, "
                        "the earliest output possible after this trigger is at "
                        f"t={trigger_time + self.trigger_delay}"
                    )
                # Check that there are no instructions too soon before the trigger:
                if 0 < trigger_time - t < max(self.clock_limit, compiler.wait_delay):
                    raise LabscriptError(
                        f"{self.description} {self.name} has an instruction at t={t}. "
                        f"This is too soon before a trigger at t={trigger_time}, "
                        "the latest output possible before this trigger is at "
                        f"t={trigger_time - max(self.clock_limit, compiler.wait_delay)}"
                    )

    def offset_instructions_from_trigger(self, trigger_times):
        """Subtracts self.trigger_delay from all instructions at or after each trigger_time.

        Args:
            trigger_times (iterable): Times of all trigger events.
        """
        offset_instructions = {}
        for t, instruction in self.instructions.items():
            # How much of a delay is there for this instruction? That depends how many triggers there are prior to it:
            n_triggers_prior = len([time for time in trigger_times if time < t])
            # The cumulative offset at this point in time:
            offset = self.trigger_delay * n_triggers_prior + trigger_times[0]
            offset = round(offset, 10)
            if isinstance(instruction, dict):
                offset_instruction = instruction.copy()
                offset_instruction["end time"] = self.quantise_to_pseudoclock(
                    round(instruction["end time"] - offset, 10)
                )
                offset_instruction["initial time"] = self.quantise_to_pseudoclock(
                    round(instruction["initial time"] - offset, 10)
                )
            else:
                offset_instruction = instruction

            new_time = self.quantise_to_pseudoclock(round(t - offset, 10))
            offset_instructions[new_time] = offset_instruction
        self.instructions = offset_instructions

        # offset each of the ramp_limits for use in the calculation within
        # Pseudoclock/ClockLine so that the times in list are consistent with the ones
        # in self.instructions
        for i, times in enumerate(self.ramp_limits):
            n_triggers_prior = len([time for time in trigger_times if time < times[0]])
            # The cumulative offset at this point in time:
            offset = self.trigger_delay * n_triggers_prior + trigger_times[0]
            offset = round(offset, 10)

            # offset start and end time of ramps
            # NOTE: This assumes ramps cannot proceed across a trigger command
            #       (for instance you cannot ramp an output across a WAIT)
            self.ramp_limits[i] = (
                self.quantise_to_pseudoclock(round(times[0] - offset, 10)),
                self.quantise_to_pseudoclock(round(times[1] - offset, 10)),
            )

    def get_change_times(self):
        """If this function is being called, it means that the parent
        Pseudoclock has requested a list of times that this output changes
        state.

        Returns:
            list: List of times output changes values.
        """        
        times = list(self.instructions.keys())
        times.sort()

        current_dict_time = None
        for time in times:
            if isinstance(self.instructions[time], dict) and current_dict_time is None:
                current_dict_time = self.instructions[time]
            elif (
                current_dict_time is not None
                and current_dict_time['initial time'] < time < current_dict_time['end time']
            ):
                raise LabscriptError(
                    f"{self.description} {self.name} has an instruction at "
                    f"t={time:.10f}s. This instruction collides with a ramp on this "
                    "output at that time. The collision "
                    f"{current_dict_time['description']} is happening from "
                    f"{current_dict_time['initial time']:.10f}s untill "
                    f"{current_dict_time['end time']:.10f}s"
                )

        self.times = times
        return times

    def get_ramp_times(self):
        """If this is being called, then it means the parent Pseuedoclock
        has asked for a list of the output ramp start and stop times.

        Returns:
            list: List of (start, stop) times of ramps for this Output.
        """
        return self.ramp_limits

    def make_timeseries(self, change_times):
        """If this is being called, then it means the parent Pseudoclock
        has asked for a list of this output's states at each time in
        change_times. (Which are the times that one or more connected
        outputs in the same pseudoclock change state). By state, I don't
        mean the value of the output at that moment, rather I mean what
        instruction it has. This might be a single value, or it might
        be a reference to a function for a ramp etc. This list of states
        is stored in self.timeseries rather than being returned."""
        self.timeseries = []
        i = 0
        time_len = len(self.times)
        for change_time in change_times:
            while i < time_len and change_time >= self.times[i]:
                i += 1
            self.timeseries.append(self.instructions[self.times[i-1]])     

    def expand_timeseries(self,all_times,flat_all_times_len):
        """This function evaluates the ramp functions in self.timeseries
        at the time points in all_times, and creates an array of output
        values at those times.  These are the values that this output
        should update to on each clock tick, and are the raw values that
        should be used to program the output device.  They are stored
        in self.raw_output."""
        # If this output is not ramping, then its timeseries should
        # not be expanded. It's already as expanded as it'll get.
        if not self.parent_clock_line.ramping_allowed:
            self.raw_output = np.array(self.timeseries, dtype=np.dtype(self.dtype))
            return
        outputarray = np.empty((flat_all_times_len,), dtype=np.dtype(self.dtype))
        j = 0
        for i, time in enumerate(all_times):
            if np.iterable(time):
                time_len = len(time)
                if isinstance(self.timeseries[i], dict):
                    # We evaluate the functions at the midpoints of the
                    # timesteps in order to remove the zero-order hold
                    # error introduced by sampling an analog signal:
                    try:
                        midpoints = time + 0.5*(time[1] - time[0])
                    except IndexError:
                        # Time array might be only one element long, so we
                        # can't calculate the step size this way. That's
                        # ok, the final midpoint is determined differently
                        # anyway:
                        midpoints = zeros(1)
                    # We need to know when the first clock tick is after
                    # this ramp ends. It's either an array element or a
                    # single number depending on if this ramp is followed
                    # by another ramp or not:
                    next_time = all_times[i+1][0] if iterable(all_times[i+1]) else all_times[i+1]
                    midpoints[-1] = time[-1] + 0.5*(next_time - time[-1])
                    outarray = self.timeseries[i]["function"](
                        midpoints - self.timeseries[i]["initial time"]
                    )
                    # Now that we have the list of output points, pass them through the unit calibration
                    if self.timeseries[i]["units"] is not None:
                        outarray = self.apply_calibration(
                            outarray, self.timeseries[i]["units"]
                        )
                    # if we have limits, check the value is valid
                    if self.limits:
                        if ((outarray<self.limits[0])|(outarray>self.limits[1])).any():
                            raise LabscriptError(
                                f"The function {self.timeseries[i]['function']} called "
                                f'on "{self.name}" at t={midpoints[0]} generated a '
                                "value which falls outside the base unit limits "
                                f"({self.limits[0]} to {self.limits[1]})"
                            )
                else:
                    outarray = empty(time_len, dtype=self.dtype)
                    outarray.fill(self.timeseries[i])
                outputarray[j:j+time_len] = outarray
                j += time_len
            else:
                outputarray[j] = self.timeseries[i]
                j += 1
        del self.timeseries # don't need this any more.
        self.raw_output = outputarray


class AnalogQuantity(Output):
    """Base class for :obj:`AnalogOut`.

    It is also used internally by :obj:`DDS`. You should never instantiate this
    class directly.
    """
    description = "analog quantity"
    default_value = 0

    def _check_truncation(self, truncation, min=0, max=1):
        if not (min <= truncation <= max):
            raise LabscriptError(
                f"Truncation argument must be between {min} and {max} (inclusive), but "
                f"is {truncation}."
            )

    def ramp(self, t, duration, initial, final, samplerate, units=None, truncation=1.):
        """Command the output to perform a linear ramp.

        Defined by
        `f(t) = ((final - initial)/duration)*t + initial`

        Args:
            t (float): Time, in seconds, to begin the ramp.
            duration (float): Length, in seconds, of the ramp.
            initial (float): Initial output value, at time `t`.
            final (float): Final output value, at time `t+duration`.
            samplerate (float): Rate, in Hz, to update the output.
            units: Units the output values are given in, as specified by the
                unit conversion class.
            truncation (float, optional): Fraction of ramp to perform. Must be between 0 and 1.

        Returns:
            float: Length of time ramp will take to complete. 
        """
        self._check_truncation(truncation)
        if truncation > 0:
            # if start and end value are the same, we don't need to ramp and can save
            # the sample ticks etc
            if initial == final:
                self.constant(t, initial, units)
                if not config.suppress_mild_warnings and not config.suppress_all_warnings:
                    sys.stderr.write(
                        f"WARNING: {self.__class__.__name__} '{self.name}' has the "
                        f"same initial and final value at time t={t:.10f}s with "
                        f"duration {duration:.10f}s. In order to save samples and "
                        "clock ticks this instruction is replaced with a constant "
                        "output.\n"
                    )
            else:
                self.add_instruction(
                    t,
                    {
                        "function": functions.ramp(
                            round(t + duration, 10) - round(t, 10), initial, final
                        ),
                        "description": "linear ramp",
                        "initial time": t,
                        "end time": t + truncation * duration,
                        "clock rate": samplerate,
                        "units": units,
                    }
                )
        return truncation * duration

    def sine(
        self,
        t,
        duration,
        amplitude,
        angfreq,
        phase,
        dc_offset,
        samplerate,
        units=None,
        truncation=1.
    ):
        """Command the output to perform a sinusoidal modulation.

        Defined by
        `f(t) = amplitude*sin(angfreq*t + phase) + dc_offset`

        Args:
            t (float): Time, in seconds, to begin the ramp.
            duration (float): Length, in seconds, of the ramp.
            amplitude (float): Amplitude of the modulation.
            angfreq (float): Angular frequency, in radians per second.
            phase (float): Phase offset of the sine wave, in radians.
            dc_offset (float): DC offset of output away from 0.
            samplerate (float): Rate, in Hz, to update the output.
            units: Units the output values are given in, as specified by the
                unit conversion class.
            truncation (float, optional): Fraction of duration to perform. Must be between 0 and 1.

        Returns:
            float: Length of time modulation will take to complete. Equivalent to `truncation*duration`.
        """
        self._check_truncation(truncation)
        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.sine(
                        round(t + duration, 10) - round(t, 10),
                        amplitude,
                        angfreq,
                        phase,
                        dc_offset,
                    ),
                    "description": "sine wave",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def sine_ramp(
        self, t, duration, initial, final, samplerate, units=None, truncation=1.
    ):
        """Command the output to perform a ramp defined by one half period of a squared sine wave.

        Defined by
        `f(t) = (final-initial)*(sin(pi*t/(2*duration)))^2 + initial`

        Args:
            t (float): Time, in seconds, to begin the ramp.
            duration (float): Length, in seconds, of the ramp.
            initial (float): Initial output value, at time `t`.
            final (float): Final output value, at time `t+duration`.
            samplerate (float): Rate, in Hz, to update the output.
            units: Units the output values are given in, as specified by the
                unit conversion class.
            truncation (float, optional): Fraction of ramp to perform. Must be between 0 and 1.

        Returns:
            float: Length of time ramp will take to complete. 
        """
        self._check_truncation(truncation)
        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.sine_ramp(
                        round(t + duration, 10) - round(t, 10), initial, final
                    ),
                    "description": "sinusoidal ramp",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def sine4_ramp(
        self, t, duration, initial, final, samplerate, units=None, truncation=1.
    ):
        """Command the output to perform an increasing ramp defined by one half period of a quartic sine wave.

        Defined by
        `f(t) = (final-initial)*(sin(pi*t/(2*duration)))^4 + initial`

        Args:
            t (float): Time, in seconds, to begin the ramp.
            duration (float): Length, in seconds, of the ramp.
            initial (float): Initial output value, at time `t`.
            final (float): Final output value, at time `t+duration`.
            samplerate (float): Rate, in Hz, to update the output.
            units: Units the output values are given in, as specified by the
                unit conversion class.
            truncation (float, optional): Fraction of ramp to perform. Must be between 0 and 1.

        Returns:
            float: Length of time ramp will take to complete. 
        """
        self._check_truncation(truncation)
        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.sine4_ramp(
                        round(t + duration, 10) - round(t, 10), initial, final
                    ),
                    "description": "sinusoidal ramp",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def sine4_reverse_ramp(
        self, t, duration, initial, final, samplerate, units=None, truncation=1.
    ):
        """Command the output to perform a decreasing ramp defined by one half period of a quartic sine wave.

        Defined by
        `f(t) = (final-initial)*(sin(pi*t/(2*duration)))^4 + initial`

        Args:
            t (float): Time, in seconds, to begin the ramp.
            duration (float): Length, in seconds, of the ramp.
            initial (float): Initial output value, at time `t`.
            final (float): Final output value, at time `t+duration`.
            samplerate (float): Rate, in Hz, to update the output.
            units: Units the output values are given in, as specified by the
                unit conversion class.
            truncation (float, optional): Fraction of ramp to perform. Must be between 0 and 1.

        Returns:
            float: Length of time ramp will take to complete. 
        """
        self._check_truncation(truncation)
        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.sine4_reverse_ramp(
                        round(t + duration, 10) - round(t, 10), initial, final
                    ),
                    "description": "sinusoidal ramp",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def exp_ramp(
        self,
        t,
        duration,
        initial,
        final,
        samplerate,
        zero=0,
        units=None,
        truncation=None,
        truncation_type="linear",
        **kwargs,
    ):
        """Exponential ramp whose rate of change is set by an asymptotic value (zero argument).
        
        Args:
            t (float): time to start the ramp
            duration (float): duration of the ramp
            initial (float): initial value of the ramp (sans truncation)
            final (float): final value of the ramp (sans truncation)
            zero (float): asymptotic value of the exponential decay/rise, i.e. limit as t --> inf
            samplerate (float): rate to sample the function
            units: unit conversion to apply to specified values before generating raw output
            truncation_type (str): 

                * `'linear'` truncation stops the ramp when it reaches the value given by the 
                  truncation parameter, which must be between initial and final
                * `'exponential'` truncation stops the ramp after a period of truncation*duration
                  In this instance, the truncation parameter should be between 0 (full truncation)
                  and 1 (no truncation). 

        """
        # Backwards compatibility for old kwarg names
        if "trunc" in kwargs:
            truncation = kwargs.pop("trunc")
        if "trunc_type" in kwargs:
            truncation_type = kwargs.pop("trunc_type")
        if truncation is not None:
            # Computed the truncated duration based on the truncation_type
            if truncation_type == "linear":
                self._check_truncation(
                    truncation, min(initial, final), max(initial, final)
                )
                # Truncate the ramp when it reaches the value truncation
                trunc_duration = duration * \
                    np.log((initial-zero)/(truncation-zero)) / \
                    np.log((initial-zero)/(final-zero))
            elif truncation_type == "exponential":
                # Truncate the ramps duration by a fraction truncation
                self._check_truncation(truncation)
                trunc_duration = truncation * duration
            else:
                raise LabscriptError(
                    "Truncation type for exp_ramp not supported. Must be either linear "
                    "or exponential."
                )
        else:
            trunc_duration = duration
        if trunc_duration > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.exp_ramp(
                        round(t + duration, 10) - round(t, 10), initial, final, zero
                    ),
                    "description": 'exponential ramp',
                    "initial time": t,
                    "end time": t + trunc_duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return trunc_duration

    def exp_ramp_t(
        self,
        t,
        duration,
        initial,
        final,
        time_constant,
        samplerate,
        units=None,
        truncation=None,
        truncation_type="linear",
        **kwargs
    ):
        """Exponential ramp whose rate of change is set by the time_constant.

        Args:
            t (float): time to start the ramp
            duration (float): duration of the ramp
            initial (float): initial value of the ramp (sans truncation)
            final (float): final value of the ramp (sans truncation)
            time_constant (float): 1/e time of the exponential decay/rise
            samplerate (float): rate to sample the function
            units: unit conversion to apply to specified values before generating raw output
            truncation_type (str): 

                * `'linear'` truncation stops the ramp when it reaches the value given by the 
                  truncation parameter, which must be between initial and final
                * `'exponential'` truncation stops the ramp after a period of truncation*duration
                  In this instance, the truncation parameter should be between 0 (full truncation)
                  and 1 (no truncation).

        """
        # Backwards compatibility for old kwarg names
        if "trunc" in kwargs:
            truncation = kwargs.pop("trunc")
        if "trunc_type" in kwargs:
            truncation_type = kwargs.pop("trunc_type")
        if truncation is not None:
            zero = (final-initial*np.exp(-duration/time_constant)) / \
                (1-np.exp(-duration/time_constant))
            if truncation_type == "linear":
                self._check_truncation(truncation, min(initial, final), max(initial, final))
                trunc_duration = time_constant * \
                    np.log((initial-zero)/(truncation-zero))
            elif truncation_type == 'exponential':
                self._check_truncation(truncation)
                trunc_duration = truncation * duration
            else:
                raise LabscriptError(
                    "Truncation type for exp_ramp_t not supported. Must be either "
                    "linear or exponential."
                )
        else:
            trunc_duration = duration
        if trunc_duration > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.exp_ramp_t(
                        round(t + duration, 10) - round(t, 10),
                        initial,
                        final,
                        time_constant,
                    ),
                    "description": "exponential ramp with time consntant",
                    "initial time": t,
                    "end time": t + trunc_duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return trunc_duration

    def piecewise_accel_ramp(
        self, t, duration, initial, final, samplerate, units=None, truncation=1.
    ):
        """Changes the output so that the second derivative follows one period of a triangle wave.

        Args:
            t (float): Time, in seconds, at which to begin the ramp.
            duration (float): Duration of the ramp, in seconds.
            initial (float): Initial output value at time `t`.
            final (float): Final output value at time `t+duration`.
            samplerate (float): Update rate of the output, in Hz.
            units: Units, defined by the unit conversion class, the value is in.
            truncation (float, optional): Fraction of ramp to perform. Default 1.0.

        Returns:
            float: Time the ramp will take to complete.
        """
        self._check_truncation(truncation)
        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": functions.piecewise_accel(
                        round(t + duration, 10) - round(t, 10), initial, final
                    ),
                    "description": "piecewise linear accelleration ramp",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def square_wave(
        self,
        t,
        duration,
        amplitude,
        frequency,
        phase,
        offset,
        duty_cycle,
        samplerate,
        units=None,
        truncation=1.
    ):
        """A standard square wave.

        This method generates a square wave which starts HIGH (when its phase is
        zero) then transitions to/from LOW at the specified `frequency` in Hz.
        The `amplitude` parameter specifies the peak-to-peak amplitude of the
        square wave which is centered around `offset`. For example, setting
        `amplitude=1` and `offset=0` would give a square wave which transitions
        between `0.5` and `-0.5`. Similarly, setting `amplitude=2` and
        `offset=3` would give a square wave which transitions between `4` and
        `2`. To instead specify the HIGH/LOW levels directly, use
        `square_wave_levels()`.

        Note that because the transitions of a square wave are sudden and
        discontinuous, small changes in timings (e.g. due to numerical rounding
        errors) can affect the output value. This is particularly relevant at
        the end of the waveform, as the final output value may be different than
        expected if the end of the waveform is close to an edge of the square
        wave. Care is taken in the implementation of this method to avoid such
        effects, but it still may be desirable to call `constant()` after
        `square_wave()` to ensure a particular final value. The output value may
        also be different than expected at certain moments in the middle of the
        waveform due to the finite samplerate (which may be different than the
        requested `samplerate`), particularly if the actual samplerate is not a
        multiple of `frequency`.

        Args:
            t (float): The time at which to start the square wave.
            duration (float): The duration for which to output a square wave
                when `truncation` is set to `1`. When `truncation` is set to a
                value less than `1`, the actual duration will be shorter than
                `duration` by that factor.
            amplitude (float): The peak-to-peak amplitude of the square wave.
                See above for an example of how to calculate the HIGH/LOW output
                values given the `amplitude` and `offset` values.
            frequency (float): The frequency of the square wave, in Hz.
            phase (float): The initial phase of the square wave. Note that the
                square wave is defined such that the phase goes from 0 to 1 (NOT
                2 pi) over one cycle, so setting `phase=0.5` will start the
                square wave advanced by 1/2 of a cycle. Setting `phase` equal to
                `duty_cycle` will cause the waveform to start LOW rather than
                HIGH.
            offset (float): The offset of the square wave, which is the value
                halfway between the LOW and HIGH output values. Note that this
                is NOT the LOW output value; setting `offset` to `0` will cause
                the HIGH/LOW values to be symmetrically split around `0`. See
                above for an example of how to calculate the HIGH/LOW output
                values given the `amplitude` and `offset` values.
            duty_cycle (float): The fraction of the cycle for which the output
                should be HIGH. This should be a number between zero and one
                inclusively. For example, setting `duty_cycle=0.1` will
                create a square wave which outputs HIGH over 10% of the
                cycle and outputs LOW over 90% of the cycle.
            samplerate (float): The requested rate at which to update the output
                value. Note that the actual samplerate used may be different if,
                for example, another output of the same device has a
                simultaneous ramp with a different requested `samplerate`, or if
                `1 / samplerate` isn't an integer multiple of the pseudoclock's
                timing resolution.
            units (str, optional): The units of the output values. If set to
                `None` then the output's base units will be used. Defaults to
                `None`.
            truncation (float, optional): The actual duration of the square wave
                will be `duration * truncation` and `truncation` must be set to
                a value in the range [0, 1] (inclusively). Set to `1` to output
                the full duration of the square wave. Setting it to `0` will
                skip the square wave entirely. Defaults to `1.`.

        Returns:
            duration (float): The actual duration of the square wave, accounting
                for `truncation`.
        """
        # Convert to values used by square_wave_levels, then call that method.
        level_0 = offset + 0.5 * amplitude
        level_1 = offset - 0.5 * amplitude
        return self.square_wave_levels(
            t,
            duration,
            level_0,
            level_1,
            frequency,
            phase,
            duty_cycle,
            samplerate,
            units,
            truncation,
        )

    def square_wave_levels(
        self,
        t,
        duration,
        level_0,
        level_1,
        frequency,
        phase,
        duty_cycle,
        samplerate,
        units=None,
        truncation=1.
    ):
        """A standard square wave.

        This method generates a square wave which starts at `level_0` (when its
        phase is zero) then transitions to/from `level_1` at the specified
        `frequency`. This is the same waveform output by `square_wave()`, but
        parameterized differently. See that method's docstring for more
        information.

        Args:
            t (float): The time at which to start the square wave.
            duration (float): The duration for which to output a square wave
                when `truncation` is set to `1`. When `truncation` is set to a
                value less than `1`, the actual duration will be shorter than
                `duration` by that factor.
            level_0 (float): The initial level of the square wave, when the
                phase is zero.
            level_1 (float): The other level of the square wave.
            frequency (float): The frequency of the square wave, in Hz.
            phase (float): The initial phase of the square wave. Note that the
                square wave is defined such that the phase goes from 0 to 1 (NOT
                2 pi) over one cycle, so setting `phase=0.5` will start the
                square wave advanced by 1/2 of a cycle. Setting `phase` equal to
                `duty_cycle` will cause the waveform to start at `level_1`
                rather than `level_0`.
            duty_cycle (float): The fraction of the cycle for which the output
                should be set to `level_0`. This should be a number between zero
                and one inclusively. For example, setting `duty_cycle=0.1` will
                create a square wave which outputs `level_0` over 10% of the
                cycle and outputs `level_1` over 90% of the cycle.
            samplerate (float): The requested rate at which to update the output
                value. Note that the actual samplerate used may be different if,
                for example, another output of the same device has a
                simultaneous ramp with a different requested `samplerate`, or if
                `1 / samplerate` isn't an integer multiple of the pseudoclock's
                timing resolution.
            units (str, optional): The units of the output values. If set to
                `None` then the output's base units will be used. Defaults to
                `None`.
            truncation (float, optional): The actual duration of the square wave
                will be `duration * truncation` and `truncation` must be set to
                a value in the range [0, 1] (inclusively). Set to `1` to output
                the full duration of the square wave. Setting it to `0` will
                skip the square wave entirely. Defaults to `1.`.

        Returns:
            duration (float): The actual duration of the square wave, accounting
                for `truncation`.
        """
        # Check the argument values.
        self._check_truncation(truncation)
        if duty_cycle < 0 or duty_cycle > 1:
            raise LabscriptError(
                "Square wave duty cycle must be in the range [0, 1] (inclusively) but "
                f"was set to {duty_cycle}."
            )

        if truncation > 0:
            # Add the instruction.
            func = functions.square_wave(
                round(t + duration, 10) - round(t, 10),
                level_0,
                level_1,
                frequency,
                phase,
                duty_cycle,
            )
            self.add_instruction(
                t,
                {
                    "function": func,
                    "description": "square wave",
                    "initial time": t,
                    "end time": t + truncation * duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation * duration

    def customramp(self, t, duration, function, *args, **kwargs):
        """Define a custom function for the output.

        Args:
            t (float): Time, in seconds, to start the function.
            duration (float): Length in time, in seconds, to perform the function.
            function (func): Function handle that defines the output waveform.
                First argument is the relative time from function start, in seconds.
            *args: Arguments passed to `function`.
            **kwargs: Keyword arguments pass to `function`.
                Standard kwargs common to other output functions are: `units`, 
                `samplerate`, and `truncation`. These kwargs are optional, but will
                not be passed to `function` if present.

        Returns:
            float: Duration the function is to be evaluate for. Equivalent to 
            `truncation*duration`.
        """
        units = kwargs.pop("units", None)
        samplerate = kwargs.pop("samplerate")
        truncation = kwargs.pop("truncation", 1.)
        self._check_truncation(truncation)

        def custom_ramp_func(t_rel):
            """The function that will return the result of the user's function,
            evaluated at relative times t_rel from 0 to duration"""
            return function(
                t_rel, round(t + duration, 10) - round(t, 10), *args, **kwargs
            )

        if truncation > 0:
            self.add_instruction(
                t,
                {
                    "function": custom_ramp_func,
                    "description": f"custom ramp: {function.__name__}",
                    "initial time": t,
                    "end time": t + truncation*duration,
                    "clock rate": samplerate,
                    "units": units,
                }
            )
        return truncation*duration

    def constant(self, t, value, units=None):
        """Sets the output to a constant value at time `t`.

        Args:
            t (float): Time, in seconds, to set the constant output.
            value (float): Value to set.
            units: Units, defined by the unit conversion class, the value is in.
        """
        # verify that value can be converted to float
        try:
            val = float(value)
        except:
            raise LabscriptError(
                f"Cannot set {self.name} to value={value} at t={t} as the value cannot "
                "be converted to float"
            )
        self.add_instruction(t, value, units)


class AnalogOut(AnalogQuantity):
    """Analog Output class for use with all devices that support timed analog outputs."""
    description = "analog output"
    
    
class StaticAnalogQuantity(Output):
    """Base class for :obj:`StaticAnalogOut`.

    It can also be used internally by other more complex output types.
    """
    description = "static analog quantity"
    default_value = 0.0
    """float: Value of output if no constant value is commanded."""
    
    @set_passed_properties(property_names = {})
    def __init__(self, *args, **kwargs):
        """Instatiantes the static analog quantity.

        Defines an internal tracking variable of the static output value and
        calls :func:`Output.__init__`.

        Args:
            *args: Passed to :func:`Output.__init__`.
            **kwargs: Passed to :func:`Output.__init__`.
        """
        Output.__init__(self, *args, **kwargs)
        self._static_value = None

    def constant(self, value, units=None):
        """Set the static output value of the output.

        Args:
            value (float): Value to set the output to.
            units: Units, defined by the unit conversion class, the value is in.

        Raises:
            LabscriptError: If static output has already been set to another value
                or the value lies outside the output limits.
        """
        if self._static_value is None:
            # If we have units specified, convert the value
            if units is not None:
                # Apply the unit calibration now
                value = self.apply_calibration(value, units)
            # if we have limits, check the value is valid
            if self.limits:
                minval, maxval = self.limits
                if not minval <= value <= maxval:
                    raise LabscriptError(
                        f"You cannot program the value {value} (base units) to "
                        f"{self.name} as it falls outside the limits "
                        f"({self.limits[0]} to {self.limits[1]})"
                    )
            self._static_value = value
        else:
            raise LabscriptError(
                f"{self.description} {self.name} has already been set to "
                f"{self._static_value} (base units). It cannot also be set to "
                f"{value} ({units if units is not None else 'base units'})."
            )

    def get_change_times(self):
        """Enforces no change times.

        Returns:
            list: An empty list, as expected by the parent pseudoclock.
        """
        # Return an empty list as the calling function at the pseudoclock level expects
        # a list
        return []

    def make_timeseries(self,change_times):
        """Since output is static, does nothing."""
        pass

    def expand_timeseries(self,*args,**kwargs):
        """Defines the `raw_output` attribute.
        """
        self.raw_output = array([self.static_value], dtype=self.dtype)

    @property
    def static_value(self):
        """float: The value of the static output."""
        if self._static_value is None:
            if not config.suppress_mild_warnings and not config.suppress_all_warnings:
                sys.stderr.write(
                    f"WARNING: {self.name} has no value set. It will be set to "
                    f"{self.instruction_to_string(self.default_value)}.\n"
                )
            self._static_value = self.default_value
        return self._static_value


class StaticAnalogOut(StaticAnalogQuantity):
    """Static Analog Output class for use with all devices that have constant outputs."""
    description = "static analog output"
        
class DigitalQuantity(Output):
    """Base class for :obj:`DigitalOut`.

    It is also used internally by other, more complex, output types.
    """
    description = "digital quantity"
    allowed_states = {1: "high", 0: "low"}
    default_value = 0
    dtype = np.uint32
    
    # Redefine __init__ so that you cannot define a limit or calibration for DO
    @set_passed_properties(property_names={"connection_table_properties": ["inverted"]})
    def __init__(self, name, parent_device, connection, inverted=False, **kwargs):
        """Instantiate a digital quantity.

        Args:
            name (str): python variable name to assign the quantity to.
            parent_device (:obj:`IntermediateDevice`): Device this quantity is attached to.
            connection (str): Connection on parent device we are connected to.
            inverted (bool, optional): If `True`, output is logic inverted.
            **kwargs: Passed to :func:`Output.__init__`.
        """
        Output.__init__(self,name,parent_device,connection, **kwargs)
        self.inverted = bool(inverted)

    def go_high(self, t):
        """Commands the output to go high.

        Args:
            t (float): Time, in seconds, when the output goes high.
        """
        self.add_instruction(t, 1)

    def go_low(self, t):
        """Commands the output to go low.

        Args:
            t (float): Time, in seconds, when the output goes low.
        """
        self.add_instruction(t, 0)

    def enable(self, t):
        """Commands the output to enable.

        If `inverted=True`, this will set the output low.

        Args:
            t (float): Time, in seconds, when the output enables.
        """
        if self.inverted:
            self.go_low(t)
        else:
            self.go_high(t)

    def disable(self, t):
        """Commands the output to disable.

        If `inverted=True`, this will set the output high.

        Args:
            t (float): Time, in seconds, when the output disables.
        """
        if self.inverted:
            self.go_high(t)
        else:
            self.go_low(t)

    def repeat_pulse_sequence(self, t, duration, pulse_sequence, period, samplerate):
        """This function only works if the DigitalQuantity is on a fast clock
        
        The pulse sequence specified will be repeated from time t until t+duration.
        
        Note 1: The samplerate should be significantly faster than the smallest time difference between 
        two states in the pulse sequence, or else points in your pulse sequence may never be evaluated.
        
        Note 2: The time points your pulse sequence is evaluated at may be different than you expect,
        if another output changes state between t and t+duration. As such, you should set the samplerate
        high enough that even if this rounding of tie points occurs (to fit in the update required to change the other output)
        your pulse sequence will not be significantly altered)

        Args:
            t (float): Time, in seconds, to start the pulse sequence.
            duration (float): How long, in seconds, to repeat the sequence.
            pulse_sequence (list): List of tuples, with each tuple of the form
                `(time, state)`.
            period (float): Defines how long the final tuple will be held for before
                repeating the pulse sequence. In general, should be longer than the 
                entire pulse sequence.
            samplerate (float): How often to update the output, in Hz.
        """
        self.add_instruction(
            t,
            {
                "function": functions.pulse_sequence(pulse_sequence, period),
                "description": "pulse sequence",
                "initial time":t,
                "end time": t + duration,
                "clock rate": samplerate,
                "units": None,
            }
        )

        return duration


class DigitalOut(DigitalQuantity):
    """Digital output class for use with all devices."""
    description = "digital output"


class StaticDigitalQuantity(DigitalQuantity):
    """Base class for :obj:`StaticDigitalOut`.

    It can also be used internally by other, more complex, output types.
    """
    description = "static digital quantity"
    default_value = 0
    """float: Value of output if no constant value is commanded."""

    @set_passed_properties(property_names = {})
    def __init__(self, *args, **kwargs):
        """Instatiantes the static digital quantity.

        Defines an internal tracking variable of the static output value and
        calls :func:`Output.__init__`.

        Args:
            *args: Passed to :func:`Output.__init__`.
            **kwargs: Passed to :func:`Output.__init__`.
        """
        DigitalQuantity.__init__(self, *args, **kwargs)
        self._static_value = None

    def go_high(self):
        """Command a static high output.

        Raises:
            LabscriptError: If output has already been set low.
        """
        if self._static_value is None:
            self.add_instruction(0,1)
            self._static_value = 1
        else:
            raise LabscriptError(
                f"{self.description} {self.name} has already been set to "
                f"{self.instruction_to_string(self._static_value)}. It cannot "
                "also be set to 1."
            )

    def go_low(self):
        """Command a static low output.

        Raises:
            LabscriptError: If output has already been set high.
        """
        if self._static_value is None:
            self.add_instruction(0,0) 
            self._static_value = 0
        else:
            raise LabscriptError(
                f"{self.description} {self.name} has already been set to "
                f"{self.instruction_to_string(self._static_value)}. It cannot "
                "also be set to 0."
            )

    def get_change_times(self):
        """Enforces no change times.

        Returns:
            list: An empty list, as expected by the parent pseudoclock.
        """
        # Return an empty list as the calling function at the pseudoclock level expects
        # a list
        return []

    def make_timeseries(self, change_times):
        """Since output is static, does nothing."""
        pass
    
    def expand_timeseries(self, *args, **kwargs):
        """Defines the `raw_output` attribute.
        """
        self.raw_output = array([self.static_value], dtype=self.dtype)

    @property
    def static_value(self):
        """float: The value of the static output."""
        if self._static_value is None:
            if not config.suppress_mild_warnings and not config.suppress_all_warnings:
                sys.stderr.write(
                    f"WARNING: {self.name} has no value set. It will be set to "
                    f"{self.instruction_to_string(self.default_value)}.\n"
                )
            self._static_value = self.default_value
        return self._static_value


class StaticDigitalOut(StaticDigitalQuantity):
    """Static Digital Output class for use with all devices that have constant outputs."""
    description = "static digital output"


class AnalogIn(Device):
    """Analog Input for use with all devices that have an analog input."""
    description = "Analog Input"

    @set_passed_properties(property_names={})
    def __init__(
        self, name, parent_device, connection, scale_factor=1.0, units="Volts", **kwargs
    ):
        """Instantiates an Analog Input.

        Args:
            name (str): python variable to assign this input to.
            parent_device (:obj:`IntermediateDevice`): Device input is connected to.
            scale_factor (float, optional): Factor to scale the recorded values by.
            units (str, optional): Units of the input.
            **kwargs: Keyword arguments passed to :func:`Device.__init__`.
        """
        self.acquisitions = []
        self.scale_factor = scale_factor
        self.units=units
        Device.__init__(self, name, parent_device, connection, **kwargs)

    def acquire(
        self, label, start_time, end_time, wait_label="", scale_factor=None, units=None
    ):
        """Command an acquisition for this input.

        Args:
            label (str): Unique label for the acquisition. Used to identify the saved trace.
            start_time (float): Time, in seconds, when the acquisition should start.
            end_time (float): Time, in seconds, when the acquisition should end.
            wait_label (str, optional): 
            scale_factor (float): Factor to scale the saved values by.
            units: Units of the input, consistent with the unit conversion class.

        Returns:
            float: Duration of the acquistion, equivalent to `end_time - start_time`.
        """
        if scale_factor is None:
            scale_factor = self.scale_factor
        if units is None:
            units = self.units
        self.acquisitions.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "label": label,
                "wait_label": wait_label,
                "scale_factor": scale_factor,
                "units": units,
            }
        )
        return end_time - start_time


class Shutter(DigitalOut):
    """Customized version of :obj:`DigitalOut` that accounts for the open/close
    delay of a shutter automatically.

    When using the methods :meth:`open` and :meth:`close`, the shutter open
    and close times are precise without haveing to track the delays. Note:
    delays can be set using runmanager globals and periodically updated
    via a calibration.

    .. Warning:: 

        If the shutter is asked to do something at `t=0`, it cannot start
        moving earlier than that. This means the initial shutter states
        will have imprecise timing.
    """
    description = "shutter"

    @set_passed_properties(
        property_names={"connection_table_properties": ["open_state"]}
    )
    def __init__(
        self, name, parent_device, connection, delay=(0, 0), open_state=1, **kwargs
    ):
        """Instantiates a Shutter.

        Args:
            name (str): python variable to assign the object to.
            parent_device (:obj:`IntermediateDevice`): Parent device the
                digital output is connected to.
            connection (str): Physical output port of the device the digital
                output is connected to.
            delay (tuple, optional): Tuple of the (open, close) delays, specified
                in seconds.
            open_state (int, optional): Allowed values are `0` or `1`. Defines which
                state of the digital output opens the shutter.

        Raises:
            LabscriptError: If the `open_state` is not `0` or `1`.
        """
        inverted = not bool(open_state)
        DigitalOut.__init__(
            self, name, parent_device, connection, inverted=inverted, **kwargs
        )
        self.open_delay, self.close_delay = delay
        self.open_state = open_state
        if self.open_state == 1:
            self.allowed_states = {0: "closed", 1: "open"}
        elif self.open_state == 0:
            self.allowed_states = {1: "closed", 0: "open"}
        else:
            raise LabscriptError(
                f"Shutter {self.name} wasn't instantiated with open_state = 0 or 1."
            )
        self.actual_times = {}
    def open(self, t):
        """Command the shutter to open at time `t`.

        Takes the open delay time into account.

        Note that the delay time will not be take into account the open delay if the
        command is made at t=0 (or other times less than the open delay). No warning
        will be issued for this loss of precision during compilation.

        Args:
            t (float): Time, in seconds, when shutter should be open.
        """
        # If a shutter is asked to do something at t=0, it cannot start moving
        # earlier than that.  So initial shutter states will have imprecise
        # timing. Not throwing a warning here because if I did, every run
        # would throw a warning for every shutter. The documentation will
        # have to make a point of this.
        t_calc = t-self.open_delay if t >= self.open_delay else 0
        self.actual_times[t] = {"time": t_calc, "instruction": 1}
        self.enable(t_calc)

    def close(self, t):
        """Command the shutter to close at time `t`.

        Takes the close delay time into account.

        Note that the delay time will not be take into account the close delay if the
        command is made at t=0 (or other times less than the close delay). No warning
        will be issued for this loss of precision during compilation.

        Args:
            t (float): Time, in seconds, when shutter should be closed.
        """
        t_calc = t-self.close_delay if t >= self.close_delay else 0
        self.actual_times[t] = {"time": t_calc, "instruction": 0}
        self.disable(t_calc)

    def generate_code(self, hdf5_file):
        classname = self.__class__.__name__
        calibration_table_dtypes = [
            ("name", "a256"), ("open_delay", float), ("close_delay", float)
        ]
        if classname not in hdf5_file["calibrations"]:
            hdf5_file["calibrations"].create_dataset(
                classname, (0,), dtype=calibration_table_dtypes, maxshape=(None,)
            )
        metadata = (self.name, self.open_delay, self.close_delay)
        dataset = hdf5_file["calibrations"][classname]
        dataset.resize((len(dataset) + 1,))
        dataset[len(dataset) - 1] = metadata

    def get_change_times(self, *args, **kwargs):
        retval = DigitalOut.get_change_times(self, *args, **kwargs)

        if len(self.actual_times) > 1:
            sorted_times = list(self.actual_times.keys())
            sorted_times.sort()
            for i in range(len(sorted_times) - 1):
                time = sorted_times[i]
                next_time = sorted_times[i + 1]
                instruction = self.actual_times[time]["instruction"]
                next_instruction = self.actual_times[next_time]["instruction"]
                state = "opened" if instruction == 1 else "closed"
                next_state = "open" if next_instruction == 1 else "close"
                # only look at instructions that contain a state change
                if instruction != next_instruction:
                    if self.actual_times[next_time]["time"] < self.actual_times[time]["time"]:
                        sys.stderr.write(
                            f"WARNING: The shutter '{self.name}' is requested to "
                            f"{next_state} too early (taking delay into account) at "
                            f"t={next_time:.10f}s when it is still not {state} from "
                            f"an earlier instruction at t={time:.10f}s\n"
                        )
                elif not config.suppress_mild_warnings and not config.suppress_all_warnings:
                    sys.stderr.write(
                        f"WARNING: The shutter '{self.name}' is requested to "
                        f"{next_state} at t={next_time:.10f}s but was never {state} "
                        f"after an earlier instruction at t={time:.10f}s\n"
                    )
        return retval


class Trigger(DigitalOut):
    """Customized version of :obj:`DigitalOut` that tracks edge type.
    """
    description = "trigger device"
    allowed_children = [TriggerableDevice]

    @set_passed_properties(property_names={})
    def __init__(
        self, name, parent_device, connection, trigger_edge_type="rising", **kwargs
    ):
        """Instantiates a DigitalOut object that tracks the trigger edge type.

        Args:
            name (str): python variable name to assign the quantity to.
            parent_device (:obj:`IntermediateDevice`): Device this quantity is attached to.
            trigger_edge_type (str, optional): Allowed values are `'rising'` and `'falling'`.
            **kwargs: Passed to :func:`Output.__init__`.

        """
        DigitalOut.__init__(self, name, parent_device, connection, **kwargs)
        self.trigger_edge_type = trigger_edge_type
        if self.trigger_edge_type == "rising":
            self.enable = self.go_high
            self.disable = self.go_low
            self.allowed_states = {1: "enabled", 0: "disabled"}
        elif self.trigger_edge_type == "falling":
            self.enable = self.go_low
            self.disable = self.go_high
            self.allowed_states = {1: "disabled", 0: "enabled"}
        else:
            raise ValueError(
                "trigger_edge_type must be 'rising' or 'falling', not "
                f"'{trigger_edge_type}'."
            )
        # A list of the times this trigger has been asked to trigger:
        self.triggerings = []

    def trigger(self, t, duration):
        """Command a trigger pulse.

        Args:
            t (float): Time, in seconds, for the trigger edge to occur.
            duration (float): Duration of the trigger, in seconds.
        """
        assert duration > 0, "Negative or zero trigger duration given"
        if t != self.t0 and self.t0 not in self.instructions:
            self.disable(self.t0)

        start = t
        end = t + duration
        for other_start, other_duration in self.triggerings:
            other_end = other_start + other_duration
            # Check for overlapping exposures:
            if not (end < other_start or start > other_end):
                raise LabscriptError(
                    f"{self.description} {self.name} has two overlapping triggerings: "
                    f"one at t = {start}s for {duration}s, and another at "
                    f"t = {other_start}s for {other_duration}s."
                )
        self.enable(t)
        self.disable(round(t + duration, 10))
        self.triggerings.append((t, duration))

    def add_device(self, device):
        if device.connection != "trigger":
            raise LabscriptError(
                f"The 'connection' string of device {device.name} "
                f"to {self.name} must be 'trigger', not '{device.connection}'"
            )
        DigitalOut.add_device(self, device)


class WaitMonitor(Trigger):

    @set_passed_properties(property_names={})
    def __init__(
        self,
        name,
        parent_device,
        connection,
        acquisition_device,
        acquisition_connection,
        timeout_device=None,
        timeout_connection=None,
        timeout_trigger_type='rising',
        **kwargs
    ):
        """Create a wait monitor. 

        This is a device or devices, one of which:

        a) outputs pulses every time the master pseudoclock begins running (either at
           the start of the shot or after a wait

        b) measures the time in between those pulses in order to determine how long the
           experiment was paused for during waits

        c) optionally, produces pulses in software time that can be used to retrigger
           the master pseudoclock if a wait lasts longer than its specified timeout

        Args:

            parent_device (Device)
                The device with buffered digital outputs that should be used to produce
                the wait monitor pulses. This device must be one which is clocked by
                the master pseudoclock.

            connection (str)
                The name of the output connection of parent_device that should be used
                to produce the pulses.

            acquisition_device (Device)
                The device which is to receive those pulses as input, and that will
                measure how long between them. This does not need to be the same device
                as the wait monitor output device (corresponding to `parent_device` and
                `connection`). At time of writing, the only devices in labscript that
                can be a wait monitor acquisition device are NI DAQmx devices that have
                counter inputs.

            acquisition_connection (str)
                The name of the input connection on `acquisition_device` that is to read
                the wait monitor pulses. The user must manually connect the output
                device (`parent_device`/`connection`) to this input with a cable, in
                order that the pulses can be read by the device. On NI DAQmx devices,
                the acquisition_connection should be the name of the counter to be used
                such as 'Ctr0'. The physical connection should be made to the input
                terminal corresponding to the gate of that counter.

            timeout_device (Device, optional)
                The device that should be used to produce pulses in software time if a
                wait lasts longer than its prescribed timeout. These pulses can
                connected to the trigger input of the master pseudoclock, via a digital
                logic to 'AND' it with other trigger sources, in order to resume the
                master pseudoclock upon a wait timing out. To produce these pulses
                during a shot requires cooperation between the acquisition device and
                the timeout device code, and at present this means only NI DAQmx devices
                can be used as the timeout device (though it need not be the same device
                as the acquisition device). If not set, timeout pulses will not be
                produced and the user must manually resume the master pseudoclock via
                other means, or abort a shot if the indended resumption mechanism fails.

            timeout_connection (str, optional)
                Which connection on the timeout device should be used to produce timeout
                pulses. Since only NI DAQmx devices are supported at the moment, this
                must be a digital output on a port on the NI DAQmx device that is not
                being used. Most NI DAQmx devices have both buffered and unbuffered
                ports, so typically one would use one line of one of the unbuffered
                ports for the timeout output.

            timeout_trigger_type (str), default `'rising'`
                The edge type to be used for the timeout signal, either `'rising'` or
                `'falling'`
        """
        if compiler.wait_monitor is not None:
            raise LabscriptError("Cannot instantiate a second WaitMonitor: there can be only be one in the experiment")
        compiler.wait_monitor = self
        Trigger.__init__(self, name, parent_device, connection, **kwargs)
        if not parent_device.pseudoclock_device.is_master_pseudoclock:
            raise LabscriptError('The output device for monitoring wait durations must be clocked by the master pseudoclock device')
        
        if (timeout_device is not None) != (timeout_connection is not None):
            raise LabscriptError('Must specify both timeout_device and timeout_connection, or neither')
        self.acquisition_device = acquisition_device
        self.acquisition_connection = acquisition_connection 
        self.timeout_device = timeout_device
        self.timeout_connection = timeout_connection
        self.timeout_trigger_type = timeout_trigger_type
        
        
class DDSQuantity(Device):
    """Used to define a DDS output. 

    It is a container class, with properties that allow access to a frequency,
    amplitude, and phase of the output as :obj:`AnalogQuantity`. 
    It can also have a gate, which provides enable/disable control of the output 
    as :obj:`DigitalOut`.

    This class instantiates channels for frequency/amplitude/phase (and optionally the
    gate) itself. 
    """
    description = 'DDS'
    allowed_children = [AnalogQuantity, DigitalOut, DigitalQuantity]

    @set_passed_properties(property_names={})
    def __init__(
        self,
        name,
        parent_device,
        connection,
        digital_gate=None,
        freq_limits=None,
        freq_conv_class=None,
        freq_conv_params=None,
        amp_limits=None,
        amp_conv_class=None,
        amp_conv_params=None,
        phase_limits=None,
        phase_conv_class=None,
        phase_conv_params=None,
        call_parents_add_device=True,
        **kwargs
    ):
        """Instantiates a DDS quantity.

        Args:
            name (str): python variable for the object created.
            parent_device (:obj:`IntermediateDevice`): Device this output is
                connected to.
            connection (str): Output of parent device this DDS is connected to.
            digital_gate (dict, optional): Configures a digital output to use as an enable/disable
                gate for the output. Should contain keys `'device'` and `'connection'`
                with arguments for the `parent_device` and `connection` for instantiating
                the :obj:`DigitalOut`. All other (optional) keys are passed as kwargs.
            freq_limits (tuple, optional): `(lower, upper)` limits for the 
                frequency of the output
            freq_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the frequency of the output.
            freq_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the frequency of the output.
            amp_limits (tuple, optional): `(lower, upper)` limits for the 
                amplitude of the output
            amp_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the amplitude of the output.
            amp_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the amplitude of the output.
            phase_limits (tuple, optional): `(lower, upper)` limits for the 
                phase of the output
            phase_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the phase of the output.
            phase_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the phase of the output.
            call_parents_add_device (bool, optional): Have the parent device run
                its `add_device` method.
            **kwargs: Keyword arguments passed to :func:`Device.__init__`.
        """        
        # Here we set call_parents_add_device=False so that we
        # can do additional initialisation before manually calling
        # self.parent_device.add_device(self). This allows the parent's
        # add_device method to perform checks based on the code below,
        # whilst still providing us with the checks and attributes that
        # Device.__init__ gives us in the meantime.
        Device.__init__(
            self, name, parent_device, connection, call_parents_add_device=False, **kwargs
        )

        # Ask the parent device if it has default unit conversion classes it would like
        # us to use:
        if hasattr(parent_device, 'get_default_unit_conversion_classes'):
            classes = self.parent_device.get_default_unit_conversion_classes(self)
            default_freq_conv, default_amp_conv, default_phase_conv = classes
            # If the user has not overridden, use these defaults. If
            # the parent does not have a default for one or more of amp,
            # freq or phase, it should return None for them.
            if freq_conv_class is None:
                freq_conv_class = default_freq_conv
            if amp_conv_class is None:
                amp_conv_class = default_amp_conv
            if phase_conv_class is None:
                phase_conv_class = default_phase_conv

        self.frequency = AnalogQuantity(
            f"{self.name}_freq",
            self,
            "freq",
            freq_limits,
            freq_conv_class,
            freq_conv_params,
        )
        self.amplitude = AnalogQuantity(
            f"{self.name}_amp",
            self,
            "amp",
            amp_limits,
            amp_conv_class,
            amp_conv_params,
        )
        self.phase = AnalogQuantity(
            f"{self.name}_phase",
            self,
            "phase",
            phase_limits,
            phase_conv_class,
            phase_conv_params,
        )

        self.gate = None
        digital_gate = digital_gate or {}
        if "device" in digital_gate and "connection" in digital_gate:
            dev = digital_gate.pop("device")
            conn = digital_gate.pop("connection")
            self.gate = DigitalOut(f"{name}_gate", dev, conn, **digital_gate)
        # Did they only put one key in the dictionary, or use the wrong keywords?
        elif len(digital_gate) > 0:
            raise LabscriptError(
                'You must specify the "device" and "connection" for the digital gate '
                f"of {self.name}."
            )

        # If the user has not specified a gate, and the parent device
        # supports gating of DDS output, it should add a gate to this
        # instance in its add_device method, which is called below. If
        # they *have* specified a gate device, but the parent device
        # has its own gating (such as the PulseBlaster), it should
        # check this and throw an error in its add_device method. See
        # labscript_devices.PulseBlaster.PulseBlaster.add_device for an
        # example of this.
        # In some subclasses we need to hold off on calling the parent
        # device's add_device function until further code has run,
        # e.g., see PulseBlasterDDS in PulseBlaster.py
        if call_parents_add_device:
            self.parent_device.add_device(self)

    def setamp(self, t, value, units=None):
        """Set the amplitude of the output.

        Args:
            t (float): Time, in seconds, when the amplitude is set.
            value (float): Amplitude to set to.
            units: Units that the value is defined in.
        """
        self.amplitude.constant(t, value, units)

    def setfreq(self, t, value, units=None):
        """Set the frequency of the output.

        Args:
            t (float): Time, in seconds, when the frequency is set.
            value (float): Frequency to set to.
            units: Units that the value is defined in.
        """
        self.frequency.constant(t, value, units)

    def setphase(self, t, value, units=None):
        """Set the phase of the output.

        Args:
            t (float): Time, in seconds, when the phase is set.
            value (float): Phase to set to.
            units: Units that the value is defined in.
        """
        self.phase.constant(t, value, units)

    def enable(self, t):
        """Enable the Output.

        Args:
            t (float): Time, in seconds, to enable the output at.

        Raises:
            LabscriptError: If the DDS is not instantiated with a digital gate.
        """
        if self.gate is None:
            raise LabscriptError(
                f"DDS {self.name} does not have a digital gate, so you cannot use the "
                "enable(t) method."
            )
        self.gate.go_high(t)

    def disable(self, t):
        """Disable the Output.

        Args:
            t (float): Time, in seconds, to disable the output at.

        Raises:
            LabscriptError: If the DDS is not instantiated with a digital gate.
        """
        if self.gate is None:
            raise LabscriptError(
                f"DDS {self.name} does not have a digital gate, so you cannot use the "
                "disable(t) method."
            )
        self.gate.go_low(t)

    def pulse(
        self,
        t,
        duration,
        amplitude,
        frequency,
        phase=None,
        amplitude_units=None,
        frequency_units=None,
        phase_units=None,
        print_summary=False,
    ):
        """Pulse the output.

        Args:
            t (float): Time, in seconds, to start the pulse at.
            duration (float): Length of the pulse, in seconds.
            amplitude (float): Amplitude to set the output to during the pulse.
            frequency (float): Frequency to set the output to during the pulse.
            phase (float, optional): Phase to set the output to during the pulse.
            amplitude_units: Units of `amplitude`.
            frequency_units: Units of `frequency`.
            phase_units: Units of `phase`.
            print_summary (bool, optional): Print a summary of the pulse during
                compilation time.

        Returns:
            float: Duration of the pulse, in seconds.
        """
        if print_summary:
            functions.print_time(
                t,
                f"{self.name} pulse at {frequency/MHz:.4f} MHz for {duration/ms:.3f} ms",
            )
        self.setamp(t, amplitude, amplitude_units)
        if frequency is not None:
            self.setfreq(t, frequency, frequency_units)
        if phase is not None:
            self.setphase(t, phase, phase_units)
        if amplitude != 0 and self.gate is not None:
            self.enable(t)
            self.disable(t + duration)
            self.setamp(t + duration, 0)
        return duration


class DDS(DDSQuantity):
    """DDS class for use with all devices that have DDS-like outputs."""


class StaticDDS(Device):
    """Static DDS class for use with all devices that have static DDS-like outputs."""
    description = "Static RF"
    allowed_children = [StaticAnalogQuantity,DigitalOut,StaticDigitalOut]

    @set_passed_properties(property_names = {})
    def __init__(
        self,
        name,
        parent_device,
        connection,
        digital_gate=None,
        freq_limits=None,
        freq_conv_class=None,
        freq_conv_params=None,
        amp_limits=None,
        amp_conv_class=None,
        amp_conv_params=None,
        phase_limits=None,
        phase_conv_class=None,
        phase_conv_params=None,
        **kwargs,
    ):
        """Instantiates a Static DDS quantity.

        Args:
            name (str): python variable for the object created.
            parent_device (:obj:`IntermediateDevice`): Device this output is
                connected to.
            connection (str): Output of parent device this DDS is connected to.
            digital_gate (dict, optional): Configures a digital output to use as an enable/disable
                gate for the output. Should contain keys `'device'` and `'connection'`
                with arguments for the `parent_device` and `connection` for instantiating
                the :obj:`DigitalOut`. All other (optional) keys are passed as kwargs.
            freq_limits (tuple, optional): `(lower, upper)` limits for the 
                frequency of the output
            freq_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the frequency of the output.
            freq_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the frequency of the output.
            amp_limits (tuple, optional): `(lower, upper)` limits for the 
                amplitude of the output
            amp_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the amplitude of the output.
            amp_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the amplitude of the output.
            phase_limits (tuple, optional): `(lower, upper)` limits for the 
                phase of the output
            phase_conv_class (:obj:`labscript_utils:labscript_utils.unitconversions`, optional): 
                Unit conversion class for the phase of the output.
            phase_conv_params (dict, optional): Keyword arguments passed to the 
                unit conversion class for the phase of the output.
            call_parents_add_device (bool, optional): Have the parent device run
                its `add_device` method.
            **kwargs: Keyword arguments passed to :func:`Device.__init__`.
        """
        # We tell Device.__init__ to not call
        # self.parent.add_device(self), we'll do that ourselves later
        # after further intitialisation, so that the parent can see the
        # freq/amp/phase objects and manipulate or check them from within
        # its add_device method.
        Device.__init__(
            self, name, parent_device, connection, call_parents_add_device=False, **kwargs
        )

        # Ask the parent device if it has default unit conversion classes it would like us to use:
        if hasattr(parent_device, 'get_default_unit_conversion_classes'):
            classes = parent_device.get_default_unit_conversion_classes(self)
            default_freq_conv, default_amp_conv, default_phase_conv = classes
            # If the user has not overridden, use these defaults. If
            # the parent does not have a default for one or more of amp,
            # freq or phase, it should return None for them.
            if freq_conv_class is None:
                freq_conv_class = default_freq_conv
            if amp_conv_class is None:
                amp_conv_class = default_amp_conv
            if phase_conv_class is None:
                phase_conv_class = default_phase_conv

        self.frequency = StaticAnalogQuantity(
            f"{self.name}_freq",
            self,
            "freq",
            freq_limits,
            freq_conv_class,
            freq_conv_params
        )
        self.amplitude = StaticAnalogQuantity(
            f"{self.name}_amp",
            self,
            "amp",
            amp_limits,
            amp_conv_class,
            amp_conv_params,
        )
        self.phase = StaticAnalogQuantity(
            f"{self.name}_phase",
            self,
            "phase",
            phase_limits,
            phase_conv_class,
            phase_conv_params,
        )        

        digital_gate = digital_gate or {}
        if "device" in digital_gate and "connection" in digital_gate:
            dev = digital_gate.pop("device")
            conn = digital_gate.pop("connection")
            self.gate = DigitalOut(f"{name}_gate", dev, conn, **digital_gate)
        # Did they only put one key in the dictionary, or use the wrong keywords?
        elif len(digital_gate) > 0:
            raise LabscriptError(
                'You must specify the "device" and "connection" for the digital gate '
                f"of {self.name}"
            )
        # Now we call the parent's add_device method. This is a must, since we didn't do so earlier from Device.__init__.
        self.parent_device.add_device(self)

    def setamp(self, value, units=None):
        """Set the static amplitude of the output.

        Args:
            value (float): Amplitude to set to.
            units: Units that the value is defined in.
        """
        self.amplitude.constant(value,units)

    def setfreq(self, value, units=None):
        """Set the static frequency of the output.

        Args:
            value (float): Frequency to set to.
            units: Units that the value is defined in.
        """
        self.frequency.constant(value,units)

    def setphase(self, value, units=None):
        """Set the static phase of the output.

        Args:
            value (float): Phase to set to.
            units: Units that the value is defined in.
        """
        self.phase.constant(value,units) 

    def enable(self, t=None):
        """Enable the Output.

        Args:
            t (float, optional): Time, in seconds, to enable the output at.

        Raises:
            LabscriptError: If the DDS is not instantiated with a digital gate.
        """        
        if self.gate:
            self.gate.go_high(t)
        else:
            raise LabscriptError(
                f"DDS {self.name} does not have a digital gate, so you cannot use the "
                "enable(t) method."
            )

    def disable(self, t=None):
        """Disable the Output.

        Args:
            t (float, optional): Time, in seconds, to disable the output at.

        Raises:
            LabscriptError: If the DDS is not instantiated with a digital gate.
        """
        if self.gate:
            self.gate.go_low(t)
        else:
            raise LabscriptError(
                f"DDS {self.name} does not have a digital gate, so you cannot use the "
                "disable(t) method."
            )

def save_time_markers(hdf5_file):
    """Save shot time markers to the shot file.

    Args:
        hdf5_file (:obj:`h5py:h5py.File`): Handle to file to save to.
    """
    time_markers = compiler.time_markers
    dtypes = [('label','a256'), ('time', float), ('color', '(1,3)int')]
    data_array = zeros(len(time_markers), dtype=dtypes)
    for i, t in enumerate(time_markers):
        data_array[i] = time_markers[t]["label"], t, time_markers[t]["color"]
    time_markers_dataset = hdf5_file.create_dataset('time_markers', data = data_array)

def generate_connection_table(hdf5_file):
    """Generates the connection table for the compiled shot.

    Args:
        hdf5_file (:obj:`h5py:h5py.File`): Handle to file to save to.
    """
    connection_table = []
    devicedict = {}
    
    # Only use a string dtype as long as is needed:
    max_BLACS_conn_length = -1

    for device in compiler.inventory:
        devicedict[device.name] = device

        unit_conversion_parameters = device._properties['unit_conversion_parameters']
        serialised_unit_conversion_parameters = labscript_utils.properties.serialise(unit_conversion_parameters)

        properties = device._properties["connection_table_properties"]
        serialised_properties = labscript_utils.properties.serialise(properties)
        
        # If the device has a BLACS_connection atribute, then check to see if it is longer than the size of the hdf5 column
        if hasattr(device,"BLACS_connection"):
            # Make sure it is a string!
            BLACS_connection = str(device.BLACS_connection)
            if len(BLACS_connection) > max_BLACS_conn_length:
                max_BLACS_conn_length = len(BLACS_connection)
        else:
            BLACS_connection = ""
            
            #if there is no BLACS connection, make sure there is no "gui" or "worker" entry in the connection table properties
            if 'worker' in properties or 'gui' in properties:
                raise LabscriptError('You cannot specify a remote GUI or worker for a device (%s) that does not have a tab in BLACS'%(device.name))
          
        if getattr(device, 'unit_conversion_class', None) is not None:
            c = device.unit_conversion_class
            unit_conversion_class_repr = f"{c.__module__}.{c.__name__}"
        else:
            unit_conversion_class_repr = repr(None)

        connection_table.append((device.name, device.__class__.__name__,
                                 device.parent_device.name if device.parent_device else str(None),
                                 str(device.connection if device.parent_device else str(None)),
                                 unit_conversion_class_repr,
                                 serialised_unit_conversion_parameters,
                                 BLACS_connection,
                                 serialised_properties))
    
    connection_table.sort()
    vlenstring = h5py.special_dtype(vlen=str)
    connection_table_dtypes = [('name','a256'), ('class','a256'), ('parent','a256'), ('parent port','a256'),
                               ('unit conversion class','a256'), ('unit conversion params', vlenstring),
                               ('BLACS_connection','a'+str(max_BLACS_conn_length)),
                               ('properties', vlenstring)]
    connection_table_array = empty(len(connection_table),dtype=connection_table_dtypes)
    for i, row in enumerate(connection_table):
        connection_table_array[i] = row
    dataset = hdf5_file.create_dataset('connection table', compression=config.compression, data=connection_table_array, maxshape=(None,))
    
    if compiler.master_pseudoclock is None:
        master_pseudoclock_name = 'None'
    else:
        master_pseudoclock_name = compiler.master_pseudoclock.name
    dataset.attrs['master_pseudoclock'] = master_pseudoclock_name

# Create a dictionary for caching results from vcs commands. The keys will be
# the paths to files that are saved during save_labscripts(). The values will be
# a list of tuples of the form (command, info, err); see the "Returns" section
# of the _run_vcs_commands() docstring for more info. Also create a FileWatcher
# instance for tracking when vcs results need updating. The callback will
# replace the outdated cache entry with a new list of updated vcs commands and
# outputs.
_vcs_cache = {}
_vcs_cache_rlock = threading.RLock()
def _file_watcher_callback(name, info, event):
    with _vcs_cache_rlock:
        _vcs_cache[name] = _run_vcs_commands(name)

_file_watcher = FileWatcher(_file_watcher_callback)

@lru_cache(None)
def _have_vcs(vcs):
    try:
        subprocess.check_output([vcs, '--version'])
        return True
    except FileNotFoundError:
        msg = f"""Warning: Cannot run {vcs} commands, {vcs} info for labscriptlib files
        will not be saved. You can disable this warning by setting
        [labscript]/save_{vcs}_info = False in labconfig."""
        sys.stderr.write(dedent(msg) + '\n')
        return False

def _run_vcs_commands(path):
    """Run some VCS commands on a file and return their output.
    
    The function is used to gather up version control system information so that
    it can be stored in the hdf5 files of shots. This is for convenience and
    compliments the full copy of the file already included in the shot file.
    
    Whether hg and git commands are run is controlled by the `save_hg_info`
    and `save_git_info` options in the `[labscript]` section of the labconfig.

    Args:
        path (str): The path with file name and extension of the file on which
            the commands will be run. The working directory will be set to the
            directory containing the specified file.

    Returns:
        results (list of (tuple, str, str)): A list of tuples, each
            containing information related to one vcs command of the form
            (command, info, err). The first entry in that tuple is itself a
            tuple of strings which was passed to subprocess.Popen() in order to
            run the command. Then info is a string that contains the text
            printed to stdout by that command, and err contains the text printed
            to stderr by the command.
    """
    # Gather together a list of commands to run.
    module_directory, module_filename = os.path.split(path)
    vcs_commands = []
    if compiler.save_hg_info and _have_vcs('hg'):
        hg_commands = [
            ['log', '--limit', '1'],
            ['status'],
            ['diff'],
        ]
        for command in hg_commands:
            command = tuple(['hg'] + command + [module_filename])
            vcs_commands.append((command, module_directory))
    if compiler.save_git_info and _have_vcs('git'):
        git_commands = [
            ['branch', '--show-current'],
            ['describe', '--tags', '--always', 'HEAD'],
            ['rev-parse', 'HEAD'],
            ['diff', 'HEAD', module_filename],
        ]
        for command in git_commands:
            command = tuple(['git'] + command)
            vcs_commands.append((command, module_directory))

    # Now go through and start running the commands.
    process_list = []
    for command, module_directory in vcs_commands:
        process = subprocess.Popen(
            command,
            cwd=module_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=startupinfo,
        )
        process_list.append((command, process))

    # Gather up results from the commands issued.
    results = []
    for command, process in process_list:
        info, err = process.communicate()
        info = info.decode('utf-8')
        err = err.decode('utf-8')
        results.append((command, info, err))
    return results
 
def save_labscripts(hdf5_file):
    """Writes the script files for the compiled shot to the shot file.

    If `save_hg_info` labconfig parameter is `True`, will attempt to save
    hg version info as an attribute.

    Args:
        hdf5_file (:obj:`h5py:h5py.File`): Handle to file to save to.
    """
    if compiler.labscript_file is not None:
        script_text = open(compiler.labscript_file).read()
    else:
        script_text = ''
    script = hdf5_file.create_dataset('script',data=script_text)
    script.attrs['name'] = os.path.basename(compiler.labscript_file).encode() if compiler.labscript_file is not None else ''
    script.attrs['path'] = os.path.dirname(compiler.labscript_file).encode() if compiler.labscript_file is not None else sys.path[0]
    try:
        import labscriptlib
    except ImportError:
        return
    prefix = os.path.dirname(labscriptlib.__file__)
    for module in sys.modules.values():
        if getattr(module, '__file__', None) is not None:
            path = os.path.abspath(module.__file__)
            if path.startswith(prefix) and (path.endswith('.pyc') or path.endswith('.py')):
                path = path.replace('.pyc', '.py')
                save_path = 'labscriptlib/' + path.replace(prefix, '').replace('\\', '/').replace('//', '/')
                if save_path in hdf5_file:
                    # Don't try to save the same module script twice! (seems to at least
                    # double count __init__.py when you import an entire module as in
                    # from labscriptlib.stages import * where stages is a folder with an
                    # __init__.py file. Doesn't seem to want to double count files if
                    # you just import the contents of a file within a module
                    continue
                hdf5_file.create_dataset(save_path, data=open(path).read())
                with _vcs_cache_rlock:
                    if not path in _vcs_cache:
                        # Add file to watch list and create its entry in the cache.
                        _file_watcher.add_file(path)
                        _file_watcher_callback(path, None, None)
                    # Save the cached vcs output to the file.
                    for command, info, err in _vcs_cache[path]:
                        attribute_str = command[0] + ' ' + command[1]
                        hdf5_file[save_path].attrs[attribute_str] = (info + '\n' + err)



def write_device_properties(hdf5_file):
    """Writes device_properties for each device in compiled shot to shto file.

    Args:
        hdf5_file (:obj:`h5py:h5py.File`): Handle to file to save to.
    """
    for device in compiler.inventory:
        device_properties = device._properties["device_properties"]

        # turn start_order and stop_order into device_properties,
        # but only if the device has a BLACS_connection attribute. start_order
        # or stop_order being None means the default order, zero. An order
        # being specified on a device without a BLACS_connection is an error.
        for attr_name in ['start_order', 'stop_order']:
            attr = getattr(device, attr_name)
            if hasattr(device, 'BLACS_connection'):
                if attr is None:
                    # Default:
                    attr = 0
                device_properties[attr_name] = attr
            elif attr is not None:
                msg = ('cannot set %s on device %s, which does ' % (attr_name, device.name) +
                       'not have a BLACS_connection attribute and thus is not started and stopped directly by BLACS.')
                raise TypeError(msg)

        # Special case: We don't create the group if the only property is an
        # empty dict called 'added properties'. This is because this property
        # is present in all devices, and represents a place to pass in
        # arbitrary data from labscript experiment scripts. We don't want a
        # group for every device if nothing is actually being passed in, so we
        # ignore this case.
        if device_properties and device_properties != {'added_properties':{}}:
            # Create group if doesn't exist:
            if not device.name in hdf5_file['devices']:
                hdf5_file['/devices'].create_group(device.name)
            labscript_utils.properties.set_device_properties(hdf5_file, device.name, device_properties)


def generate_wait_table(hdf5_file):
    """Generates the wait table for the shot and saves it to the shot file.

    Args:
        hdf5_file (:obj:`h5py:h5py.File`): Handle to file to save to.
    """
    dtypes = [('label','a256'), ('time', float), ('timeout', float)]
    data_array = zeros(len(compiler.wait_table), dtype=dtypes)
    for i, t in enumerate(sorted(compiler.wait_table)):
        label, timeout = compiler.wait_table[t]
        data_array[i] = label, t, timeout
    dataset = hdf5_file.create_dataset('waits', data = data_array)
    if compiler.wait_monitor is not None:
        acquisition_device = compiler.wait_monitor.acquisition_device.name 
        acquisition_connection = compiler.wait_monitor.acquisition_connection
        if compiler.wait_monitor.timeout_device is None:
            timeout_device = ''
        else:
            timeout_device = compiler.wait_monitor.timeout_device.name
        if compiler.wait_monitor.timeout_connection is None:
            timeout_connection = ''
        else:
            timeout_connection = compiler.wait_monitor.timeout_connection
        timeout_trigger_type = compiler.wait_monitor.timeout_trigger_type
    else:
        acquisition_device, acquisition_connection, timeout_device, timeout_connection, timeout_trigger_type = '', '', '', '', ''
    dataset.attrs['wait_monitor_acquisition_device'] = acquisition_device
    dataset.attrs['wait_monitor_acquisition_connection'] = acquisition_connection
    dataset.attrs['wait_monitor_timeout_device'] = timeout_device
    dataset.attrs['wait_monitor_timeout_connection'] = timeout_connection
    dataset.attrs['wait_monitor_timeout_trigger_type'] = timeout_trigger_type

    
def generate_code():
    """Compiles a shot and saves it to the shot file.
    """
    if compiler.hdf5_filename is None:
        raise LabscriptError('hdf5 file for compilation not set. Please call labscript_init')
    elif not os.path.exists(compiler.hdf5_filename):
        with h5py.File(compiler.hdf5_filename ,'w') as hdf5_file:
            hdf5_file.create_group('globals')
    with h5py.File(compiler.hdf5_filename, 'a') as hdf5_file:
        try:
            hdf5_file.create_group('devices')
            hdf5_file.create_group('calibrations')
        except ValueError:
            # Group(s) already exist - this is not a fresh h5 file, we cannot compile with it:
            raise ValueError('The HDF5 file %s already contains compilation data '%compiler.hdf5_filename +
                             '(possibly partial if due to failed compilation). ' +
                             'Please use a fresh shot file. ' +
                             'If this one was autogenerated by a previous compilation, ' +
                             'and you wish to have a new one autogenerated, '+
                             'simply delete it and run again, or add the \'-f\' command line ' +
                             'argument to automatically overwrite.')
        for device in compiler.inventory:
            if device.parent_device is None:
                device.generate_code(hdf5_file)
                
        save_time_markers(hdf5_file)
        generate_connection_table(hdf5_file)
        write_device_properties(hdf5_file)
        generate_wait_table(hdf5_file)
        save_labscripts(hdf5_file)

        # Save shot properties:
        group = hdf5_file.create_group('shot_properties')
        set_attributes(group, compiler.shot_properties)


def trigger_all_pseudoclocks(t='initial'):
    # Must wait this long before providing a trigger, in case child clocks aren't ready yet:
    wait_delay = compiler.wait_delay
    if type(t) in [str, bytes] and t == 'initial':
        # But not at the start of the experiment:
        wait_delay = 0
    # Trigger them all:
    for pseudoclock in compiler.all_pseudoclocks:
        pseudoclock.trigger(t, compiler.trigger_duration)
    # How long until all devices can take instructions again? The user
    # can command output from devices on the master clock immediately,
    # but unless things are time critical, they can wait this long and
    # know for sure all devices can receive instructions:
    max_delay_time = max_or_zero([pseudoclock.trigger_delay for pseudoclock in compiler.all_pseudoclocks if not pseudoclock.is_master_pseudoclock])
    # On the other hand, perhaps the trigger duration and clock limit of the master clock is
    # limiting when we can next give devices instructions:
    # So find the max of 1.0/clock_limit of every clockline on every pseudoclock of the master pseudoclock
    master_pseudoclock_delay = max(1.0/compiler.master_pseudoclock.clock_limit, max_or_zero([1.0/clockline.clock_limit for pseudoclock in compiler.master_pseudoclock.child_devices for clockline in pseudoclock.child_devices]))
    max_delay = max(compiler.trigger_duration + master_pseudoclock_delay, max_delay_time)    
    return max_delay + wait_delay
    
def wait(label, t, timeout=5):
    """Commands pseudoclocks to pause until resumed by an external trigger, or a timeout is reached.

    Args:
        label (str): Unique name for wait.
        t (float): Time, in seconds, at which experiment should begin the wait.
        timeout (float, optional): Maximum length of the wait, in seconds. After
            this time, the pseudoclocks are automatically re-triggered.

    Returns:
        float: Time required for all pseudoclocks to resume execution once
        wait has completed.            
    """
    if not str(label):
        raise LabscriptError('Wait must have a name')
    max_delay = trigger_all_pseudoclocks(t)
    if t in compiler.wait_table:
        raise LabscriptError('There is already a wait at t=%s'%str(t))
    if any([label==existing_label for existing_label, _ in compiler.wait_table.values()]):
        raise LabscriptError('There is already a wait named %s'%str(label))
    compiler.wait_table[t] = str(label), float(timeout)
    return max_delay

def add_time_marker(t, label, color=None, verbose=False):
    """Add a marker for the specified time. These markers are saved in the HDF5 file.
    This allows one to label that time with a string label, and a color that
    applications may use to represent this part of the experiment. The color may be
    specified as an RGB tuple, or a string of the color name such as 'red', or a string
    of its hex value such as '#ff88g0'. If verbose=True, this funtion also prints the
    label and time, which can be useful to view during shot compilation.

    Runviewer displays these markers and allows one to manipulate the time axis based on
    them, and BLACS' progress bar plugin displays the name and colour of the most recent
    marker as the shot is running"""
    if isinstance(color, (str, bytes)):
        import PIL.ImageColor
        color = PIL.ImageColor.getrgb(color)
    if color is None:
        color = (-1, -1, -1)
    elif not all(0 <= n <= 255 for n in color):
        raise ValueError("Invalid RGB tuple %s" % str(color))
    if verbose:
        functions.print_time(t, label)
    compiler.time_markers[t] = {"label": label, "color": color}

def start():
    """Indicates the end of the connection table and the start of the 
    experiment logic.

    Returns:
        float: Time required for all pseudoclocks to start execution.
    """
    compiler.start_called = True
    # Get and save some timing info about the pseudoclocks:
    # TODO: What if you need to trigger individual Pseudolocks on the one device, rather than the PseudoclockDevice as a whole?
    pseudoclocks = [device for device in compiler.inventory if isinstance(device, PseudoclockDevice)]
    compiler.all_pseudoclocks = pseudoclocks
    toplevel_devices = [device for device in compiler.inventory if device.parent_device is None]
    master_pseudoclocks = [pseudoclock for pseudoclock in pseudoclocks if pseudoclock.is_master_pseudoclock]
    if len(master_pseudoclocks) > 1:
        raise LabscriptError('Cannot have more than one master pseudoclock')
    if not toplevel_devices:
        raise LabscriptError('No toplevel devices and no master pseudoclock found')
    elif pseudoclocks:
        (master_pseudoclock,) = master_pseudoclocks
        compiler.master_pseudoclock = master_pseudoclock
        # Which pseudoclock requires the longest pulse in order to trigger it?
        compiler.trigger_duration = max_or_zero([pseudoclock.trigger_minimum_duration for pseudoclock in pseudoclocks if not pseudoclock.is_master_pseudoclock])
        
        trigger_clock_limits = [pseudoclock.trigger_device.clock_limit for pseudoclock in pseudoclocks if not pseudoclock.is_master_pseudoclock]
        if len(trigger_clock_limits) > 0:
            min_clock_limit = min(trigger_clock_limits)
            min_clock_limit = min([min_clock_limit, master_pseudoclock.clock_limit])
        else:
            min_clock_limit = master_pseudoclock.clock_limit
    
        # ensure we don't tick faster than attached devices to the master pseudoclock can handle
        clockline_limits = [clockline.clock_limit for pseudoclock in master_pseudoclock.child_devices for clockline in pseudoclock.child_devices]
        min_clock_limit = min(min_clock_limit, min(clockline_limits))
    
        # check the minimum trigger duration for the waitmonitor
        if compiler.wait_monitor is not None:
            wait_monitor_minimum_pulse_width = getattr(
                compiler.wait_monitor.acquisition_device,
                'wait_monitor_minimum_pulse_width',
                0,
            )
            compiler.trigger_duration = max(
                compiler.trigger_duration, wait_monitor_minimum_pulse_width
            )
            
        # Provide this, or the minimum possible pulse, whichever is longer:
        compiler.trigger_duration = max(2.0/min_clock_limit, compiler.trigger_duration) + 2*master_pseudoclock.clock_resolution
        # Must wait this long before providing a trigger, in case child clocks aren't ready yet:
        compiler.wait_delay = max_or_zero([pseudoclock.wait_delay for pseudoclock in pseudoclocks if not pseudoclock.is_master_pseudoclock])
        
        # Have the master clock trigger pseudoclocks at t = 0:
        max_delay = trigger_all_pseudoclocks()
    else:
        # No pseudoclocks, only other toplevel devices:
        compiler.master_pseudoclock = None
        compiler.trigger_duration = 0
        compiler.wait_delay = 0
        max_delay = 0
    return max_delay
    
def stop(t, target_cycle_time=None, cycle_time_delay_after_programming=False):
    """Indicate the end of an experiment at the given time, and initiate compilation of
    instructions, saving them to the HDF5 file. Configures some shot options.
    
    Args:
        t (float): The end time of the experiment.

        target_cycle_time (float, optional): default: `None`
            How long, in seconds, after the previous shot was started, should this shot be
            started by BLACS. This allows one to run shots at a constant rate even if they
            are of different durations. If `None`, BLACS will run the next shot immediately
            after the previous shot completes. Otherwise, BLACS will delay starting this
            shot until the cycle time has elapsed. This is a request only, and may not be
            met if running/programming/saving data from a shot takes long enough that it
            cannot be met. This functionality requires the BLACS `cycle_time` plugin to be
            enabled in labconfig. Its accuracy is also limited by software timing,
            requirements of exact cycle times beyond software timing should be instead done
            using hardware triggers to Pseudoclocks.

        cycle_time_delay_after_programming (bool, optional): default: `False`
            Whether the BLACS cycle_time plugin should insert the required delay for the
            target cycle time *after* programming devices, as opposed to before programming
            them. This is more precise, but may cause some devices to output their first
            instruction for longer than desired, since some devices begin outputting their
            first instruction as soon as they are programmed rather than when they receive
            their first clock tick. If not set, the *average* cycle time will still be just
            as close to as requested (so long as there is adequate time available), however
            the time interval between the same part of the experiment from one shot to the
            next will not be as precise due to variations in programming time.
    """
    # Indicate the end of an experiment and initiate compilation:
    if t == 0:
        raise LabscriptError('Stop time cannot be t=0. Please make your run a finite duration')
    for device in compiler.inventory:
        if isinstance(device, PseudoclockDevice):
            device.stop_time = t
    if target_cycle_time is not None:
        # Ensure we have a valid type for this
        target_cycle_time = float(target_cycle_time)
    compiler.shot_properties['target_cycle_time'] = target_cycle_time
    compiler.shot_properties['cycle_time_delay_after_programming'] = cycle_time_delay_after_programming
    generate_code()

# TO_DELETE:runmanager-batchompiler-agnostic
#   entire function load_globals can be deleted
def load_globals(hdf5_filename):
    import runmanager
    params = runmanager.get_shot_globals(hdf5_filename)
    with h5py.File(hdf5_filename,'r') as hdf5_file:
        for name in params.keys():
            if name in globals() or name in locals() or name in _builtins_dict:
                raise LabscriptError('Error whilst parsing globals from %s. \'%s\''%(hdf5_filename,name) +
                                     ' is already a name used by Python, labscript, or Pylab.'+
                                     ' Please choose a different variable name to avoid a conflict.')
            if name in keyword.kwlist:
                raise LabscriptError('Error whilst parsing globals from %s. \'%s\''%(hdf5_filename,name) +
                                     ' is a reserved Python keyword.' +
                                     ' Please choose a different variable name.')
            try:
                assert '.' not in name
                exec(name + ' = 0')
                exec('del ' + name )
            except:
                raise LabscriptError('ERROR whilst parsing globals from %s. \'%s\''%(hdf5_filename,name) +
                                     'is not a valid Python variable name.' +
                                     ' Please choose a different variable name.')
                                     
            # Workaround for the fact that numpy.bool_ objects dont 
            # match python's builtin True and False when compared with 'is':
            if type(params[name]) == bool_: # bool_ is numpy.bool_, imported from pylab
                params[name] = bool(params[name])                         
            # 'None' is stored as an h5py null object reference:
            if isinstance(params[name], h5py.Reference) and not params[name]:
                params[name] = None
            _builtins_dict[name] = params[name]
            
# TO_DELETE:runmanager-batchompiler-agnostic 
#   load_globals_values=True            
def labscript_init(hdf5_filename, labscript_file=None, new=False, overwrite=False, load_globals_values=True):
    """Initialises labscript and prepares for compilation.

    Args:
        hdf5_filename (str): Path to shot file to compile.
        labscript_file: Handle to the labscript file.
        new (bool, optional): If `True`, ensure a new shot file is created.
        overwrite (bool, optional): If `True`, overwrite existing shot file, if it exists.
        load_globals_values (bool, optional): If `True`, load global values 
            from the existing shot file.
    """
    # save the builtins for later restoration in labscript_cleanup
    compiler.save_builtins_state()
    
    if new:
        # defer file creation until generate_code(), so that filesystem
        # is not littered with h5 files when the user merely imports
        # labscript. If the file already exists, and overwrite is true, delete it so we get one fresh.
        if os.path.exists(hdf5_filename) and overwrite:
            os.unlink(hdf5_filename)
    elif not os.path.exists(hdf5_filename):
        raise LabscriptError('Provided hdf5 filename %s doesn\'t exist.'%hdf5_filename)
    # TO_DELETE:runmanager-batchompiler-agnostic 
    elif load_globals_values:
        load_globals(hdf5_filename) 
    # END_DELETE:runmanager-batchompiler-agnostic 
    
    compiler.hdf5_filename = hdf5_filename
    if labscript_file is None:
        import __main__
        labscript_file = __main__.__file__
    compiler.labscript_file = os.path.abspath(labscript_file)
    

def labscript_cleanup():
    """restores builtins and the labscript module to its state before
    labscript_init() was called"""
    compiler.reset()
