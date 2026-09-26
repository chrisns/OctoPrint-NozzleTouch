# coding=utf-8
"""The grid, and the arithmetic that turns nozzle touches into a firmware mesh.

The Snapmaker 2.0 A350 firmware keeps a bilinear mesh of up to 11 x 11 points over a fixed
area: X 52 to 332 and Y 43 to 328, whatever the grid size. The spacing is a whole number of
millimetres, so an 11 point grid is 28 x 28, not 28 x 28.5. The dual toolhead's own
auto-level measures that grid with an inductive sensor, which sees the metal under the
coating, and sets the zero with one nozzle touch plus a fixed kit compensation. On a
QS + bracing kit printer that zero sat about 0.3 mm above the plate.

This module does the sums for a mesh measured by the nozzle itself. It has no printer and no
OctoPrint in it, so the tests can feed it numbers.
"""
from __future__ import absolute_import, division

import math

GRID = 11
GRID_START = (52.0, 43.0)       # firmware bilinear_grid_manual() on the A350
GRID_SPAN = (280.0, 285.0)      # the same for any grid size
T0_REACH = (326.0, 308.0)       # T0 soft endstop maximum X and Y
# The sensor trips this far below the height where the nozzle first touches the plate.
# 0.72 mm puts G-code Z 0 on the plate surface (measured on the A350 dual, 2026-09-25).
TRIP_TO_ZERO = 0.72


def probe_nodes(grid=GRID, start=GRID_START, span=GRID_SPAN, reach=T0_REACH):
    """The nodes the nozzle can touch, in serpentine order, as (i, j, x, y).

    Nodes past the nozzle's reach are left out. write_values() extrapolates them.
    """
    dx, dy = span[0] / (grid - 1), span[1] / (grid - 1)
    nodes = []
    for j in range(grid):
        row = [(i, j, start[0] + i * dx, start[1] + j * dy) for i in range(grid)]
        row = [n for n in row if n[2] <= reach[0] + 1e-6 and n[3] <= reach[1] + 1e-6]
        nodes.extend(row if j % 2 == 0 else row[::-1])
    return nodes


def rows_of(nodes):
    """Group the nodes by row, keeping the order. A drift reference goes before each row."""
    rows = []
    for node in nodes:
        if not rows or rows[-1][0][1] != node[1]:
            rows.append([])
        rows[-1].append(node)
    return rows


def average_passes(samples):
    """Mean machine Z of each node over all passes. samples: [(x, y, z)] -> {(x, y): z}."""
    acc = {}
    for x, y, z in samples:
        acc.setdefault((round(x, 3), round(y, 3)), []).append(float(z))
    return {key: sum(v) / len(v) for key, v in acc.items()}


def as_grid(points):
    """{(x, y): z} -> (xs, ys, z[i][j]). Every (x, y) combination must be present."""
    xs = sorted({x for x, _ in points})
    ys = sorted({y for _, y in points})
    z = [[None] * len(ys) for _ in xs]
    for (x, y), value in points.items():
        z[xs.index(x)][ys.index(y)] = value
    missing = [(x, y) for i, x in enumerate(xs) for j, y in enumerate(ys) if z[i][j] is None]
    if missing:
        raise ValueError("the measured grid has holes at %s" % missing[:5])
    return xs, ys, z


def _segment(value, axis):
    """Index of the interval to use for value, and the fraction along it. Outside the
    axis the end interval is used, which extrapolates in a straight line."""
    last = len(axis) - 2
    k = 0
    while k < last and value > axis[k + 1]:
        k += 1
    return k, (value - axis[k]) / (axis[k + 1] - axis[k])


def interpolate(xs, ys, z, x, y):
    """Bilinear inside the measured grid, straight-line extrapolation outside it."""
    i, u = _segment(x, xs)
    j, w = _segment(y, ys)
    a, b = z[i][j], z[i + 1][j]
    c, d = z[i][j + 1], z[i + 1][j + 1]
    return (a * (1 - u) + b * u) * (1 - w) + (c * (1 - u) + d * u) * w


def write_values(points, start, spacing, grid=GRID, trip_to_zero=TRIP_TO_ZERO):
    """The firmware mesh: z[i][j] for the firmware's own nodes.

    points: {(x, y): machine Z of the trip}. start and spacing come from the firmware's reply
    to G1029 P<grid>, because its spacing is rounded to whole millimetres.
    """
    xs, ys, z = as_grid(points)
    return [[interpolate(xs, ys, z, start[0] + i * spacing[0], start[1] + j * spacing[1])
             + trip_to_zero for j in range(grid)] for i in range(grid)]


def firmware_geometry(grid):
    """The start and spacing the firmware uses for a grid of this size: whole millimetres."""
    return GRID_START, (float(int(GRID_SPAN[0] / (grid - 1))), float(int(GRID_SPAN[1] / (grid - 1))))


def change_from(old, values, start, spacing):
    """How far each new node moved from the old mesh, as z[i][j], or None.

    The old mesh can have another size, so it is interpolated at the new nodes.
    """
    if not old or len(old) < 2 or len(old[0]) < 2:
        return None
    old_start, old_spacing = firmware_geometry(len(old))
    xs = [old_start[0] + i * old_spacing[0] for i in range(len(old))]
    ys = [old_start[1] + j * old_spacing[1] for j in range(len(old[0]))]
    return [[values[i][j] - interpolate(xs, ys, old, start[0] + i * spacing[0], start[1] + j * spacing[1])
             for j in range(len(values[i]))] for i in range(len(values))]


def mesh_problem(z, band=5.0):
    """Why a stored mesh is not safe to move under, or None.

    Every value must be near the others. G1029 fills a new grid with 75, so a write that
    stopped half way leaves some nodes 33 mm above the rest.
    """
    if not z:
        return "the printer did not report a mesh"
    flat = [v for column in z for v in column]
    low, high = min(flat), max(flat)
    if high - low > band:
        return ("the stored mesh runs from %.2f to %.2f mm. A plate is not that far out of flat, "
                "so the mesh is damaged" % (low, high))
    return None


def _solve3(m, v):
    """Solve a 3 x 3 linear system by Cramer's rule."""
    def det(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    d = det(m)
    if abs(d) < 1e-12:
        raise ValueError("the points do not span a plane")
    out = []
    for col in range(3):
        a = [row[:] for row in m]
        for r in range(3):
            a[r][col] = v[r]
        out.append(det(a) / d)
    return out


def plane_fit(points):
    """Least-squares plane z = a x + b y + c through {(x, y): z}. Returns (a, b, c)."""
    sxx = sxy = syy = sx = sy = n = sxz = syz = sz = 0.0
    for (x, y), z in points.items():
        sxx += x * x; sxy += x * y; syy += y * y; sx += x; sy += y; n += 1
        sxz += x * z; syz += y * z; sz += z
    return _solve3([[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, n]], [sxz, syz, sz])


def summary(points):
    """Tilt and flatness of the plate, for the report."""
    a, b, c = plane_fit(points)
    residual = [z - (a * x + b * y + c) for (x, y), z in points.items()]
    rms = math.sqrt(sum(r * r for r in residual) / len(residual))
    return dict(tilt_x_per_100mm=round(a * 100, 3), tilt_y_per_100mm=round(b * 100, 3),
                flatness_min=round(min(residual), 3), flatness_max=round(max(residual), 3),
                flatness_rms=round(rms, 3), nodes=len(points))


def spread(samples):
    """How well the passes agree: the largest difference between passes at one node."""
    acc = {}
    for x, y, z in samples:
        acc.setdefault((round(x, 3), round(y, 3)), []).append(float(z))
    diffs = [max(v) - min(v) for v in acc.values() if len(v) > 1]
    return round(max(diffs), 3) if diffs else None


def t1_offset(current, t0_trips, t1_trips):
    """The new M218 T1 Z from touches of both nozzles at the same points.

    Trips are G-code Z, measured with the current offset in force. If T1 trips d higher
    than T0, T1's tip reached the plate while the carriage was d higher, so T1 hangs d
    lower than T0. A more negative M218 T1 Z raises T1, so the new value is current - d.

    Both sensors are taken to trip after the same travel. The firmware's own nozzle-height
    routine adds a compensation for each nozzle (0.573 and 0.497 on this toolhead). A print
    ladder showed that this puts T1 about 0.07 mm too low, so it is not used here.
    Returns (new offset, mean difference, spread of the differences).
    """
    diffs = [b - a for a, b in zip(t0_trips, t1_trips) if a is not None and b is not None]
    if not diffs:
        raise ValueError("no point gave a trip on both nozzles")
    mean = sum(diffs) / len(diffs)
    return round(float(current) - mean, 3), round(mean, 3), round(max(diffs) - min(diffs), 3)
