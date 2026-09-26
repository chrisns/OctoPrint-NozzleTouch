# coding=utf-8
"""One nozzle touch.

The nozzles of the Snapmaker 2.0 dual toolhead float. When the plate pushes one up, its
optocoupler reads TRIGGERED in M119. This module lowers a cold nozzle in small steps and
reads M119 after every step, so the nozzle never presses further than one step past the
trip, and never goes below a hard floor.

It does not use G30. On this firmware a G30 probe move targets machine Z -2 while the plate
sits at machine Z ~42, so a sensor that failed to trip would drive the nozzle 44 mm into the
plate at homing speed.

With bed levelling on, a perfect mesh trips the sensor at the same G-code Z everywhere, so
the machine Z of the trip (from the M114 step counter) is what the new mesh is built from.
"""
from __future__ import absolute_import

from . import gcode

Z_SAFE = 3.0
COARSE, FINE, BACKOFF = 0.10, 0.02, 0.30
START_ABOVE_HINT = 0.4
FLOOR_BELOW_HINT = 0.8
ABS_FLOOR = -2.0


class Aborted(Exception):
    """The user pressed Abort."""


class Touch(object):
    """Touches points with one nozzle. `sensor` is "left" (T0) or "right" (T1)."""

    def __init__(self, bridge, sensor, steps_per_mm, aborted=lambda: False):
        self._bridge = bridge
        self._sensor = sensor
        self._steps = steps_per_mm
        self._aborted = aborted

    def _state(self, commands, timeout=60.0):
        if self._aborted():
            raise Aborted()
        lines = self._bridge.run(list(commands) + ["M119"], timeout)
        return gcode.optocoupler(lines, self._sensor), lines

    def at(self, x, y, hint, floor=None):
        """Touch (x, y). Returns (trip G-code Z, trip machine Z), or None if nothing tripped.

        hint is the expected trip height, usually the last point's result.
        """
        floor = floor if floor is not None else max(ABS_FLOOR, hint - FLOOR_BELOW_HINT)
        start = min(1.0, hint + START_ABOVE_HINT)
        state, _ = self._state(["G90", "G0 Z%.2f F900" % Z_SAFE,
                                "G0 X%.2f Y%.2f F4000" % (x, y), "G0 Z%.2f F300" % start], 120.0)
        if state is None:
            raise RuntimeError("M119 did not report the %s nozzle sensor" % self._sensor)
        if state:
            start = min(1.5, start + 0.5)
            state, _ = self._state(["G0 Z%.2f F300" % start])
            if state:
                self._bridge.send(["G0 Z%.2f F900" % Z_SAFE])
                return None
        z = start
        # coarse: find the trip
        while True:
            if round(z - COARSE, 3) < floor:
                self._bridge.send(["G0 Z%.2f F900" % Z_SAFE])
                return None
            z = round(z - COARSE, 3)
            state, _ = self._state(["G1 Z%.3f F120" % z])
            if state:
                break
        # back off, then measure it finely
        z = round(z + BACKOFF, 3)
        state, _ = self._state(["G1 Z%.3f F120" % z])
        if state:
            self._bridge.send(["G0 Z%.2f F900" % Z_SAFE])
            return None
        while True:
            if round(z - FINE, 3) < floor:
                self._bridge.send(["G0 Z%.2f F900" % Z_SAFE])
                return None
            z = round(z - FINE, 3)
            state, lines = self._state(["G1 Z%.3f F120" % z])
            if state:
                break
        machine = gcode.count_z(lines, self._steps)
        self._bridge.send(["G0 Z%.2f F900" % Z_SAFE])
        if machine is None:
            raise RuntimeError("M114 did not report the step counter")
        return z, machine
