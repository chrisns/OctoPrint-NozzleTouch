# coding=utf-8
"""The calibration behind the button.

1. Home, and start heating the bed and both nozzles. Wait for the nozzles only.
2. Retract both filaments, so the hot nozzles do not ooze, and park the head high.
3. Wait for the user to brush both nozzles clean and press Continue. A dirty tip trips early:
   PETG on a nozzle read 0.2 to 0.38 mm high (2026-09-25). If nobody presses Continue, the
   heaters go off after a time limit.
4. Cool the nozzles with the part fans, wait for the bed to reach its temperature, and let the
   plate settle. The mesh must be measured at print temperature.
   With skip_wipe, steps 1 to 3 do not run: the nozzles stay off, and are cooled if warm.
5. Home again, and refuse to go on unless bed levelling is on and the stored mesh is sane.
   Then touch the reachable nodes of the firmware's 11 x 11 grid with the cold T0
   nozzle. Before every row, touch a reference point and take out its drift: while the
   machine sits hot the trip height wanders by up to 0.2 mm an hour. Repeat for each pass and
   average (one pass is good to about 0.03 mm, two to about 0.02 mm).
6. Write the mesh: G1029 P11, one M421 per node, M420 S1, M500. If a step fails, put the old
   mesh back.
7. Touch the same points with T0 and T1 and set M218 T1 Z so both tips sit at one height.
8. Always: soft endstops on, nozzle up, T0 active, nozzle heaters and fans off.
"""
from __future__ import absolute_import

import threading
import time

from . import gcode, mesh
from .probe import ABS_FLOOR, Aborted, Touch

# A node that did not trip in the normal window is touched again in a wider one:
# from this far above the hint down to this far below it.
RETRY_ABOVE, RETRY_BELOW = 0.5, 1.1

DEFAULTS = dict(
    bed_temp=75, t0_temp=240, t1_temp=215, retract_mm=6.0,
    park=[160.0, 30.0, 60.0], wipe_timeout_min=30, cool_below=55.0, settle_s=60, passes=2,
    trip_to_zero=mesh.TRIP_TO_ZERO, reference=[178.0, 171.0],
    offset_points=[[178.0, 171.0], [100.0, 100.0], [260.0, 100.0], [100.0, 250.0], [260.0, 250.0]],
    write_offset=True, bed_off_at_end=True, skip_wipe=False,
)


class CalibrationError(Exception):
    """The calibration could not finish."""


class Calibration(threading.Thread):
    """Runs the whole sequence once. `say` receives progress dicts for the UI."""

    def __init__(self, bridge, temperatures, say, settings=None, clock=time, logger=None):
        threading.Thread.__init__(self, name="nozzletouch-calibration")
        self.daemon = True
        self._bridge = bridge
        self._temperatures = temperatures
        self._say = say
        self._s = dict(DEFAULTS, **(settings or {}))
        self._clock = clock
        self._logger = logger
        self._abort = threading.Event()
        self._wiped = threading.Event()
        self.phase = "starting"
        self.result = None
        self.error = None

    # -- control from the plugin -----------------------------------------

    def abort(self):
        self._abort.set()
        self._wiped.set()

    def confirm_wipe(self):
        self._wiped.set()

    @property
    def waiting_for_wipe(self):
        return self.phase == "wipe" and not self._wiped.is_set()

    # -- helpers ----------------------------------------------------------

    def _check(self):
        if self._abort.is_set():
            raise Aborted()

    def _tell(self, phase, message, **extra):
        self.phase = phase
        payload = dict(type="progress", phase=phase, message=message)
        payload.update(extra)
        self._say(payload)

    def _wait(self, seconds):
        end = self._clock.time() + seconds
        while self._clock.time() < end:
            self._check()
            self._clock.sleep(min(0.5, max(0.0, end - self._clock.time())))

    def _wait_temps(self, wanted, below=False, timeout=1200):
        """Wait until every heater in `wanted` ({name: target}) has reached its target."""
        end = self._clock.time() + timeout
        while True:
            self._check()
            now = self._temperatures()
            ok = all(now.get(name) is not None and
                     (now[name] <= target if below else now[name] >= target - 2.0)
                     for name, target in wanted.items())
            if ok:
                return
            if self._clock.time() > end:
                raise CalibrationError("the heaters did not reach %s in %d s" % (wanted, timeout))
            self._clock.sleep(2.0)

    # -- the phases -------------------------------------------------------

    def _heat_and_retract(self):
        s = self._s
        self._tell("heat", "Homing and heating the nozzles: T0 %d C, T1 %d C. The bed heats to %d C "
                           "meanwhile." % (s["t0_temp"], s["t1_temp"], s["bed_temp"]))
        self._bridge.run(["M140 S%d" % s["bed_temp"], "M104 T0 S%d" % s["t0_temp"],
                          "M104 T1 S%d" % s["t1_temp"], "G28"], 240.0)
        # Only the nozzles: the bed has until the nozzles are brushed and cold.
        self._wait_temps({"tool0": s["t0_temp"], "tool1": s["t1_temp"]})
        self._tell("retract", "Retracting %.0f mm on both nozzles." % s["retract_mm"])
        for tool in (0, 1):
            self._bridge.run(["T%d" % tool, "M83", "G1 E-%.1f F1200" % s["retract_mm"]], 120.0)
        x, y, z = s["park"]
        self._bridge.run(["T0", "G90", "G0 Z%.1f F900" % z, "G0 X%.1f Y%.1f F4000" % (x, y)], 120.0)

    def _wait_for_wipe(self):
        limit = self._s["wipe_timeout_min"]
        self._tell("wipe", "Brush both nozzle tips clean with a brass wire brush, then press Continue. "
                           "The heaters stay on for %d min at most." % limit)
        self._say(dict(type="wipe", timeout_min=limit))
        end = self._clock.time() + limit * 60.0
        while not self._wiped.is_set():
            if self._clock.time() > end:
                raise CalibrationError("nobody pressed Continue in %d min, so the heaters are off" % limit)
            self._clock.sleep(0.5)
        self._check()

    def _start_bed_only(self):
        s = self._s
        self._tell("heat", "Skipping the brush step. Homing, and heating the bed to %d C." % s["bed_temp"])
        self._bridge.run(["M140 S%d" % s["bed_temp"], "M104 T0 S0", "M104 T1 S0", "G28"], 240.0)

    def _cool(self):
        s = self._s
        now = self._temperatures()
        hot = [t for t in ("tool0", "tool1") if (now.get(t) or 0.0) > s["cool_below"]]
        self._bridge.run(["M104 T0 S0", "M104 T1 S0"], 60.0)
        if hot:
            self._tell("cool", "Cooling both nozzles below %.0f C with both part fans."
                       % s["cool_below"])
            # Without P, M106 drives only the active nozzle's fan on the dual toolhead.
            self._bridge.run(["M106 P0 S255", "M106 P1 S255"], 60.0)
            self._wait_temps({"tool0": s["cool_below"], "tool1": s["cool_below"]}, below=True, timeout=1800)
            self._bridge.run(["M107 P0", "M107 P1"], 30.0)
        self._tell("cool", "Waiting for the bed to reach %d C." % s["bed_temp"])
        self._wait_temps({"bed": s["bed_temp"]})
        self._tell("cool", "Letting the plate settle for %d s." % s["settle_s"])
        self._wait(s["settle_s"])

    def _prepare(self):
        self._tell("home", "Homing, and reading the mesh, offsets and steps in use now.")
        lines = self._bridge.run(["G28", "M211 S0", "M420 S1"], 240.0)
        if gcode.levelling(lines) is not True:
            raise CalibrationError("M420 S1 did not turn bed levelling on. Without it the nozzle "
                                   "would go far below the plate. Run the touchscreen auto-level once, "
                                   "then run this again")
        steps = gcode.z_steps_per_mm(self._bridge.run(["M92"], 30.0))
        old_mesh = gcode.bilinear_grid(self._bridge.run(["M420 V"], 60.0))
        problem = mesh.mesh_problem(old_mesh)
        if problem:
            raise CalibrationError(problem + ". Run the touchscreen auto-level once, then run this again")
        offset = gcode.t1_offset(self._bridge.run(["M218"], 30.0))
        if offset is None:
            raise CalibrationError("M218 did not report the T1 hotend offset")
        right = gcode.optocoupler(self._bridge.run(["M119"], 30.0), "right")
        if not right:
            raise CalibrationError("T1 is not raised, so T0 is not the active nozzle")
        return steps, old_mesh, offset[2]

    def _probe_mesh(self, steps):
        s = self._s
        touch = Touch(self._bridge, "left", steps, self._abort.is_set)
        nodes = mesh.probe_nodes()
        total = len(nodes) * s["passes"]
        samples, done, ref0, hint = [], 0, None, -s["trip_to_zero"]
        for pas in range(1, s["passes"] + 1):
            order = nodes if pas % 2 else nodes[::-1]
            for row in mesh.rows_of(order):
                if ref0 is None:                  # the first touch: the widest window
                    ref = touch.at(s["reference"][0], s["reference"][1], 0.6, floor=-2.0)
                else:
                    ref = touch.at(s["reference"][0], s["reference"][1], hint)
                if ref is None:
                    raise CalibrationError("the reference point did not trip")
                if ref0 is None:
                    ref0, hint = ref, ref[0]
                drift = ref[1] - ref0[1]
                self._tell("mesh", "Pass %d, row %d: reference drift %+.3f mm." % (pas, row[0][1], drift),
                           done=done, total=total)
                for i, j, x, y in row:
                    r = touch.at(x, y, hint) or touch.at(
                        x, y, hint + RETRY_ABOVE, floor=max(ABS_FLOOR, hint - RETRY_BELOW))
                    if r is None:
                        raise CalibrationError("node X%.0f Y%.0f did not trip" % (x, y))
                    hint = r[0]
                    samples.append((x, y, r[1] - drift))
                    done += 1
                    self._tell("mesh", "Pass %d: node %d of %d, X%.0f Y%.0f, trip %+.2f."
                               % (pas, done, total, x, y, r[0]), done=done, total=total)
        return samples

    def _write_mesh(self, samples, old_mesh):
        s = self._s
        self._tell("write", "Writing the %d x %d mesh." % (mesh.GRID, mesh.GRID))
        points = mesh.average_passes(samples)
        geometry = gcode.grid_geometry(self._bridge.run(["G1029 P%d" % mesh.GRID], 60.0))
        try:
            if geometry is None or geometry[0] != mesh.GRID:
                raise CalibrationError("G1029 did not confirm an %d point grid" % mesh.GRID)
            _, start, spacing = geometry
            values = mesh.write_values(points, start, spacing, mesh.GRID, s["trip_to_zero"])
            lines = self._bridge.run(["M421 I%d J%d Z%.4f" % (i, j, values[i][j])
                                      for j in range(mesh.GRID) for i in range(mesh.GRID)], 180.0)
            if gcode.errors(lines):
                raise CalibrationError("M421 was refused: %s" % gcode.errors(lines)[:2])
            if gcode.levelling(self._bridge.run(["M420 S1"], 60.0)) is not True:
                raise CalibrationError("the firmware would not turn levelling on with the new mesh")
        except Exception:
            self._restore(old_mesh)
            raise
        if not any("Settings Stored" in l for l in self._bridge.run(["M500"], 60.0)):
            raise CalibrationError("M500 did not confirm that the mesh was saved")
        return points, values, start, spacing

    def _restore(self, old_mesh):
        if not old_mesh:
            self._tell("write", "Could not restore the old mesh: it was not readable. "
                                "Power-cycle the printer to load the saved one.")
            return
        n = len(old_mesh)
        self._bridge.run(["G1029 P%d" % n] + ["M421 I%d J%d Z%.4f" % (i, j, old_mesh[i][j])
                                             for j in range(n) for i in range(n)], 180.0)
        self._tell("write", "The old %d x %d mesh is back in place (not saved again)." % (n, n))

    def _offsets(self, steps, current):
        s = self._s
        self._tell("offset", "Touching %d points with T0, then with T1." % len(s["offset_points"]))
        t0 = Touch(self._bridge, "left", steps, self._abort.is_set)
        t1 = Touch(self._bridge, "right", steps, self._abort.is_set)
        trips0, trips1 = [], []
        for x, y in s["offset_points"]:
            r = t0.at(x, y, -s["trip_to_zero"])
            trips0.append(r[0] if r else None)
            self._tell("offset", "T0 at X%.0f Y%.0f: trip %s." % (x, y, "%+.2f" % r[0] if r else "none"))
        self._bridge.run(["G90", "G0 Z8 F900", "T1"], 120.0)
        if gcode.optocoupler(self._bridge.run(["M119"], 30.0), "right"):
            raise CalibrationError("the T1 sensor reads TRIGGERED after the toolchange")
        for (x, y), hint in zip(s["offset_points"], trips0):
            r = t1.at(x, y, hint if hint is not None else -s["trip_to_zero"], floor=mesh_floor(hint))
            trips1.append(r[0] if r else None)
            self._tell("offset", "T1 at X%.0f Y%.0f: trip %s." % (x, y, "%+.2f" % r[0] if r else "none"))
        self._bridge.run(["G90", "G0 Z8 F900", "T0"], 120.0)
        new, mean, spread = mesh.t1_offset(current, trips0, trips1)
        written = False
        if s["write_offset"]:
            lines = self._bridge.run(["T0", "M218 T1 Z%.3f" % new, "M218"], 60.0)
            found = gcode.t1_offset(lines)
            if found is None or abs(found[2] - new) > 0.0015:
                raise CalibrationError("M218 read back %s, not %.3f" % (found, new))
            written = True
        points = [dict(x=x, y=y, t0=a, t1=b) for (x, y), a, b in zip(s["offset_points"], trips0, trips1)]
        return dict(before=current, after=new, mean_difference=mean, spread=spread,
                    written=written, points=points)

    def _safe_end(self):
        s = self._s
        commands = ["M211 S1", "G91", "G0 Z5 F600", "G90", "T0",
                    "M104 T0 S0", "M104 T1 S0", "M107 P0", "M107 P1"]
        if s["bed_off_at_end"]:
            commands.append("M140 S0")
        try:
            self._bridge.send(commands)
        except Exception:                                    # noqa: BLE001
            if self._logger:
                self._logger.exception("nozzletouch: could not send the safe end")

    # -- the thread -------------------------------------------------------

    def run(self):
        started = self._clock.time()
        outcome = None
        try:
            outcome = self._run(started)
        finally:
            # Always, even for an error nobody expected. Say the outcome only once it is safe.
            self._safe_end()
        self._say(outcome)

    def _run(self, started):
        try:
            if self._s["skip_wipe"]:
                self._start_bed_only()
            else:
                self._heat_and_retract()
                self._wait_for_wipe()
            self._cool()
            steps, old_mesh, current = self._prepare()
            samples = self._probe_mesh(steps)
            points, values, start, spacing = self._write_mesh(samples, old_mesh)
            offset = self._offsets(steps, current)
            change = mesh.change_from(old_mesh, values, start, spacing)
            xs = [x for x, _ in points]
            ys = [y for _, y in points]
            self.result = dict(
                finished=time.time(), minutes=round((self._clock.time() - started) / 60.0, 1),
                passes=self._s["passes"], summary=mesh.summary(points), pass_spread=mesh.spread(samples),
                grid=dict(size=mesh.GRID, start=list(start), spacing=list(spacing)),
                probed_area=[min(xs), min(ys), max(xs), max(ys)],
                mesh=[[round(v, 4) for v in col] for col in values],
                old_mesh=old_mesh, trip_to_zero=self._s["trip_to_zero"], t1=offset,
                change=[[round(v, 4) for v in col] for col in change] if change else None,
                change_max=round(max(abs(v) for col in change for v in col), 3) if change else None,
                fake_acks=getattr(self._bridge, "fake_acks", 0),
            )
            self.phase = "done"
            return dict(type="done", result=self.result)
        except Exception as exception:                       # noqa: BLE001
            # An abort also wakes the bridge, which then raises from inside its wait.
            if isinstance(exception, Aborted) or self._abort.is_set():
                self.phase, self.error = "aborted", "aborted"
                return dict(type="aborted", message="Stopped. The nozzle is up and the soft endstops are on.")
            else:
                self.phase, self.error = "failed", str(exception)
                if self._logger:
                    self._logger.exception("nozzletouch: calibration failed")
                return dict(type="failed", message=str(exception))


def mesh_floor(hint):
    """T1's floor: far enough below T0's trip for any sane offset error, never below -2."""
    if hint is None:
        return -2.0
    return max(-2.0, hint - 0.8)
