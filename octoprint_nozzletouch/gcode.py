# coding=utf-8
"""Serial helpers: send G-code, wait for proof that it ran, and read the replies.

The bridge follows OctoPrint-FilamentSensor. Each batch ends with M400, M114 and a
numbered M118 echo, and the caller waits for that number. M114 alone cannot be the
marker on a Snapmaker 2.0, because the firmware answers G92 with a position line too.

One thing is new here. When a floating nozzle is pushed up, the toolhead reports
"active extruder mismatch target: N!" at once, on its own schedule. When that message lands
inside the firmware's "ok", OctoPrint receives "oactive extruder mismatch target: 0!" and
then "k", sees no ok, and waits for ever with the queue stalled. Every nozzle touch raises
the message, so this happens. The bridge spots the split and gives OctoPrint the lost ack.

The parsers at the bottom are plain functions. The tests feed them lines captured from the
printer on 2026-09-25.
"""
from __future__ import absolute_import

import re
import threading

TAG = "plugin:nozzletouch"
SYNC = "NTSYNC"
_SYNC_RE = re.compile(SYNC + r"(?P<n>\d+)")

_COUNT_RE = re.compile(r"Count X:\s*(-?\d+)\s+Y:\s*(-?\d+)\s+Z:\s*(-?\d+)")
_POSITION_RE = re.compile(r"X:\s*(-?\d+\.?\d*)\s+Y:\s*(-?\d+\.?\d*)\s+Z:\s*(-?\d+\.?\d*)")
_OPTO_RE = re.compile(r"z_probe_(?P<side>left|right)_optocoupler:\s*(?P<state>TRIGGERED|open)")
_HOTEND_RE = re.compile(r"Hotend offsets:\s*\S+\s+(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)")
_GRID_AXIS_RE = re.compile(r"^(?:Recv:\s*)?(?:echo:)?\s*(?P<axis>[XY]):\s*(?P<start>-?\d+(?:\.\d+)?)"
                           r"\s+-\s+(?P<spacing>-?\d+(?:\.\d+)?)\s*$")
_GRID_SIZE_RE = re.compile(r"Set grid size\s*:\s*(\d+)")
_GRID_ROW_RE = re.compile(r"^(?:Recv:\s*)?\s*(?P<j>\d+)\s+(?P<values>[+-]\d+\.\d+(?:\s+[+-]\d+\.\d+)*)\s*$")
_STEPS_RE = re.compile(r"M92\b.*?\bZ(?P<z>\d+(?:\.\d+)?)")
_LEVELLING_RE = re.compile(r"Bed Leveling (?P<state>ON|OFF)")


class Timeout(Exception):
    """The printer did not answer in time."""


def is_split_ok(line):
    """True when the toolhead's mismatch message has swallowed a firmware 'ok'."""
    line = line.strip()
    if "mismatch" not in line or line.startswith("ok"):
        return False
    return line.startswith("o") or line.endswith("ok")


def optocoupler(lines, side):
    """True when the named nozzle sensor reads TRIGGERED, False when open, None if absent."""
    state = None
    for line in lines:
        match = _OPTO_RE.search(line)
        if match is not None and match.group("side") == side:
            state = match.group("state") == "TRIGGERED"
    return state


def count_z(lines, steps_per_mm):
    """Machine Z from the step counter of an M114 reply, or None. Bed levelling is in it."""
    value = None
    for line in lines:
        match = _COUNT_RE.search(line)
        if match is not None:
            value = int(match.group(3)) / float(steps_per_mm)
    return value


def t1_offset(lines):
    """The T1 hotend offset (x, y, z) from an M218 reply, or None."""
    for line in lines:
        match = _HOTEND_RE.search(line)
        if match is not None:
            return tuple(float(v) for v in match.groups())
    return None


def levelling(lines):
    """True when an M420 reply says "Bed Leveling ON", False for OFF, None if it says neither.

    This matters more than anything else here. On a printer with the quick swap kit the plate
    sits at machine Z ~42, and only the mesh lifts G-code Z 0 up to it. With levelling off,
    G0 Z3 drives the nozzle about 39 mm into the plate.
    """
    state = None
    for line in lines:
        match = _LEVELLING_RE.search(line)
        if match is not None:
            state = match.group("state") == "ON"
    return state


def grid_geometry(lines):
    """(size, (x0, y0), (dx, dy)) from the reply to G1029 P<size>, or None."""
    axes, size = {}, None
    for line in lines:
        match = _GRID_AXIS_RE.search(line.strip())
        if match is not None:
            axes[match.group("axis")] = (float(match.group("start")), float(match.group("spacing")))
        match = _GRID_SIZE_RE.search(line)
        if match is not None:
            size = int(match.group(1))
    if size is None or set(axes) != {"X", "Y"}:
        return None
    return size, (axes["X"][0], axes["Y"][0]), (axes["X"][1], axes["Y"][1])


def bilinear_grid(lines):
    """The stored mesh from an M420 V reply, as z[i][j] (i along X), or None.

    The printout has one row per Y index. Reading stops at the subdivided grid that follows.
    """
    rows = {}
    reading = False
    for line in lines:
        text = line[5:] if line.startswith("Recv:") else line
        if "Bilinear Leveling Grid" in text:
            reading = True
            continue
        if not reading:
            continue
        if "Subdivided" in text or "Leveling" in text:
            break
        match = _GRID_ROW_RE.search(text)
        if match is not None:
            rows[int(match.group("j"))] = [float(v) for v in match.group("values").split()]
    if not rows:
        return None
    ny = max(rows) + 1
    nx = len(rows[0])
    if sorted(rows) != list(range(ny)) or any(len(r) != nx for r in rows.values()):
        return None
    return [[rows[j][i] for j in range(ny)] for i in range(nx)]


def z_steps_per_mm(lines, default=400.0):
    for line in lines:
        match = _STEPS_RE.search(line)
        if match is not None:
            return float(match.group("z"))
    return default


def errors(lines):
    return [line for line in lines if "Error" in line or "error:" in line.lower()]


class GcodeBridge(object):
    """Sends G-code and waits for the reply that proves it ran."""

    def __init__(self, printer, logger):
        self._printer = printer
        self._logger = logger
        self._lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._event = threading.Event()
        self._collecting = False
        self._interrupted = False
        self._lines = []
        self._marker = 0
        self.fake_acks = 0
        # The plugin turns this off outside a calibration when the user says so.
        self.fix_split_ok = True

    # -- OctoPrint hook ---------------------------------------------------

    def on_gcode_received(self, comm_instance, line, *args, **kwargs):
        if line and self.fix_split_ok and is_split_ok(line):
            # Not from inside the comm thread's own callback: give it a moment.
            self.fake_acks += 1
            self._logger.info("nozzletouch: split ok (%r), sending a fake ack", line.strip())
            threading.Timer(0.05, self._printer.fake_ack).start()
        if self._collecting and line:
            with self._receive_lock:
                if not self._collecting:
                    return line
                self._lines.append(line)
                match = _SYNC_RE.search(line)
                if match is not None and int(match.group("n")) == self._marker:
                    self._event.set()
        return line

    def interrupt(self):
        """Wake a waiting caller at once, for an abort or a disconnect."""
        self._interrupted = True
        self._event.set()

    # -- primitives -------------------------------------------------------

    def run(self, commands, timeout=60.0):
        """Send commands, wait for them to finish, and return every reply line."""
        if not self._printer.is_operational():
            raise RuntimeError("printer is not connected")
        with self._lock:
            with self._receive_lock:
                self._lines = []
                self._interrupted = False
                self._event.clear()
                self._marker += 1
                marker = self._marker
                self._collecting = True
            try:
                payload = list(commands) + ["M400", "M114", "M118 %s%d" % (SYNC, marker)]
                self._printer.commands(payload, tags={TAG})
                if not self._event.wait(timeout):
                    # A lost ok the split detector did not recognise: one ack, one more wait.
                    self.fake_acks += 1
                    self._logger.warning("nozzletouch: no marker %d after %.0fs, sending a fake ack",
                                         marker, timeout)
                    self._printer.fake_ack()
                    if not self._event.wait(min(timeout, 30.0)):
                        raise Timeout("the printer did not reach marker %d" % marker)
                if self._interrupted:
                    raise RuntimeError("interrupted while waiting for the printer")
                with self._receive_lock:
                    return list(self._lines)
            finally:
                with self._receive_lock:
                    self._collecting = False

    def send(self, commands):
        """Send commands and return at once."""
        if not self._printer.is_operational():
            raise RuntimeError("printer is not connected")
        self._printer.commands(list(commands), tags={TAG})
