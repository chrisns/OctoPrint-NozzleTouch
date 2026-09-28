# coding=utf-8
"""Every setting the plugin reads, and the checks that keep them safe to send.

This lives apart from the plugin class so the tests can check it without OctoPrint.
Points are stored as text ("178, 171") because OctoPrint's settings pane edits text well
and nested lists badly.

Every value here ends up in a G-code command. With soft endstops off, which the touches
need, the firmware also stops checking X and Y, so a point outside the plate drives the
head into the frame. The checks run on save and again before every run.
"""
from __future__ import absolute_import

import re

from . import mesh


class SettingsError(ValueError):
    """A setting was refused before it could reach the machine."""


DEFAULTS = dict(
    bed_temp=75,
    t0_temp=240,
    t1_temp=215,
    retract_mm=6.0,
    park="160, 30, 60",
    wipe_timeout_min=30,
    cool_below=55.0,
    settle_s=60,
    soak_min=10,
    passes=2,
    trip_to_zero=mesh.TRIP_TO_ZERO,
    reference="178, 171",
    offset_points="178, 171; 100, 100; 260, 100; 100, 250; 260, 250",
    write_offset=True,
    bed_off_at_end=True,
    fix_split_ok=True,
)

# (low, high) for each number. Temperatures stay where filament is soft enough to brush off.
RANGES = dict(
    bed_temp=(40, 110),
    t0_temp=(170, 280),
    t1_temp=(170, 280),
    retract_mm=(0.0, 15.0),
    wipe_timeout_min=(1, 240),
    cool_below=(30.0, 70.0),
    settle_s=(0, 900),
    soak_min=(0, 60),
    passes=(1, 4),
    trip_to_zero=(0.3, 1.2),
)
INTEGERS = ("bed_temp", "t0_temp", "t1_temp", "wipe_timeout_min", "settle_s", "soak_min", "passes")
BOOLEANS = ("write_offset", "bed_off_at_end", "fix_split_ok")

# The nodes the nozzle touches. Reference and offset points must sit inside them, so that
# they are on the plate and inside the measured mesh.
PROBED = (mesh.GRID_START[0], mesh.GRID_START[1],
          mesh.GRID_START[0] + mesh.GRID_SPAN[0] * 9 / 10.0,       # X 304
          mesh.GRID_START[1] + mesh.GRID_SPAN[1] * 9 / 10.0)       # Y 299.5
PARK = ((0.0, 320.0), (0.0, 300.0), (20.0, 300.0))

_NUMBER = r"-?\d+(?:\.\d+)?"


def _number(key, value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SettingsError("%s is %r, which is not a number" % (key, value))
    low, high = RANGES[key]
    if not low <= number <= high:
        raise SettingsError("%s is %g. It must be from %g to %g" % (key, number, low, high))
    return int(round(number)) if key in INTEGERS else number


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_text(value):
    """Lists from a hand-edited config.yaml become the text form."""
    if isinstance(value, (list, tuple)):
        if value and all(isinstance(p, (list, tuple)) for p in value):
            return "; ".join(", ".join(str(v) for v in p) for p in value)
        return ", ".join(str(v) for v in value)
    return str(value or "")


def parse_points(key, text, size):
    """'178, 171; 100, 100' -> [[178.0, 171.0], [100.0, 100.0]]. Each point has `size` numbers."""
    points = []
    for chunk in re.split(r"[;\n]+", _as_text(text)):
        chunk = chunk.strip()
        if not chunk:
            continue
        numbers = re.findall(_NUMBER, chunk)
        if len(numbers) != size or re.sub(_NUMBER, "", chunk).strip(" ,\t") != "":
            raise SettingsError("%s: %r is not %d numbers" % (key, chunk, size))
        points.append([float(n) for n in numbers])
    if not points:
        raise SettingsError("%s is empty" % key)
    return points


def _inside_probed(key, point):
    x, y = point
    x0, y0, x1, y1 = PROBED
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        raise SettingsError("%s: X%g Y%g is outside the touched area, X%g to %g and Y%g to %g"
                            % (key, x, y, x0, x1, y0, y1))


def routine_config(raw):
    """The checked settings, in the form the routine takes. Raises SettingsError."""
    raw = dict(DEFAULTS, **(raw or {}))
    config = {}
    for key in RANGES:
        config[key] = _number(key, raw[key])
    for key in BOOLEANS:
        config[key] = _boolean(raw[key])

    reference = parse_points("reference", raw["reference"], 2)
    if len(reference) != 1:
        raise SettingsError("reference must be one point")
    _inside_probed("reference", reference[0])
    config["reference"] = reference[0]

    points = parse_points("offset_points", raw["offset_points"], 2)
    if len(points) > 9:
        raise SettingsError("offset_points has %d points. Use 9 or fewer" % len(points))
    for point in points:
        _inside_probed("offset_points", point)
    config["offset_points"] = points

    park = parse_points("park", raw["park"], 3)
    if len(park) != 1:
        raise SettingsError("park must be one point")
    for value, (low, high), axis in zip(park[0], PARK, "XYZ"):
        if not low <= value <= high:
            raise SettingsError("park %s%g is outside %g to %g" % (axis, value, low, high))
    config["park"] = park[0]
    return config
