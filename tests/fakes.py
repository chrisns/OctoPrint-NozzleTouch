"""Fakes that behave like the machine, not mocks that record calls.

FakeSnapmaker models what the calibration depends on: a plate with a tilt and a bump, the
firmware mesh applied to every move, and two floating nozzles whose optocouplers trip after
a fixed travel. T1 hangs lower than its M218 offset says, as it did on the real toolhead.
Replies use the wording the printer sent on 2026-09-25.
"""
import math
import re


class FakeSnapmaker(object):

    def __init__(self, plate=None, h1=-1.083, t1_drop=1.115, overtravel=0.72,
                 old_grid=None, fail_m421=False, steps=400.0, levelling_fails=False):
        # machine Z of the plate surface under the nozzle at (x, y)
        self.plate = plate or (lambda x, y: 41.0 + 0.0027 * x + 0.0003 * y
                               + 0.08 * math.exp(-((x - 80) ** 2 + (y - 43) ** 2) / 200.0))
        self.h1 = h1                    # M218 T1 Z in force
        self.t1_drop = t1_drop          # how far T1's tip really sits below T0's
        self.overtravel = overtravel    # carriage travel from first touch to trip
        self.steps = steps
        self.fail_m421 = fail_m421
        self.levelling_fails = levelling_fails
        self.tool = 0
        self.x = self.y = 0.0
        self.z = 60.0                   # G-code Z of the active tool
        self.relative = False
        self.soft_endstops = True
        self.levelling = True
        self.sent = []
        self.saved = 0
        n = 6
        self.grid = dict(n=n, start=(52.0, 43.0), spacing=(56.0, 57.0),
                         z=old_grid or [[42.0 + 0.002 * i for j in range(n)] for i in range(n)])
        self.temps = dict(bed=25.0, tool0=25.0, tool1=25.0)

    # -- the mesh the firmware applies ------------------------------------

    def mesh_at(self, x, y):
        g = self.grid
        n = g["n"]
        fx = min(max((x - g["start"][0]) / g["spacing"][0], 0.0), n - 1 - 1e-9)
        fy = min(max((y - g["start"][1]) / g["spacing"][1], 0.0), n - 1 - 1e-9)
        i, j = int(fx), int(fy)
        u, w = fx - i, fy - j
        z = g["z"]
        return ((z[i][j] * (1 - u) + z[i + 1][j] * u) * (1 - w)
                + (z[i][j + 1] * (1 - u) + z[i + 1][j + 1] * u) * w)

    def carriage(self):
        """Machine Z of the carriage (T0's tip height when T0 is down)."""
        mesh = self.mesh_at(self.x, self.y) if self.levelling else 0.0
        native = self.z if self.tool == 0 else self.z - self.h1
        return native + mesh

    def pressed(self, tool):
        tip = self.carriage() - (0.0 if tool == 0 else self.t1_drop)
        return tip <= self.plate(self.x, self.y) - self.overtravel + 1e-9

    # -- G-code -------------------------------------------------------------

    def execute(self, command):
        self.sent.append(command)
        words = command.split()
        code = words[0]
        params = dict((w[0], float(w[1:])) for w in words[1:] if len(w) > 1 and w[0].isalpha()
                      and re.match(r"^-?\d*\.?\d+$", w[1:]))
        replies = []
        if code == "G90":
            self.relative = False
        elif code == "G91":
            self.relative = True
        elif code in ("G0", "G1"):
            if "X" in params:
                self.x = self.x + params["X"] if self.relative else params["X"]
            if "Y" in params:
                self.y = self.y + params["Y"] if self.relative else params["Y"]
            if "Z" in params:
                z = self.z + params["Z"] if self.relative else params["Z"]
                floor = 0.0 if self.tool == 0 else self.h1
                if self.soft_endstops and z < floor:
                    z = floor
                self.z = z
        elif code == "G28":
            self.tool, self.z, self.relative = 0, 313.0, False
        elif code in ("T0", "T1"):
            self.tool = int(code[1])
        elif code == "M211":
            self.soft_endstops = params.get("S", 1) != 0
        elif code == "M420":
            if "S" in params:
                self.levelling = params["S"] != 0 and not self.levelling_fails
                if params["S"] and self.levelling_fails:
                    replies.append("Error:Failed to enable Bed Leveling")
                replies.append("echo:Bed Leveling %s" % ("ON" if self.levelling else "OFF"))
            if "V" in command:
                replies += ["Bilinear Leveling Grid:",
                            "      " + "      ".join(str(i) for i in range(self.grid["n"]))]
                for j in range(self.grid["n"]):
                    replies.append(" %d %s" % (j, " ".join("%+.3f" % self.grid["z"][i][j]
                                                          for i in range(self.grid["n"]))))
                replies += ["", "Subdivided with CATMULL ROM Leveling Grid:", " 0 +1.000 +2.000"]
        elif code == "M119":
            replies += ["Reporting endstop status", "z_min: open",
                        "z_probe_proximity_switch: open",
                        "z_probe_left_optocoupler: %s" % ("TRIGGERED" if self.tool == 1 or self.pressed(0) else "open"),
                        "z_probe_right_optocoupler: %s" % ("TRIGGERED" if self.tool == 0 or self.pressed(1) else "open")]
        elif code == "M114":
            replies.append("X:%.2f Y:%.2f Z:%.2f E:0.00 Count X:0 Y:0 Z:%d B:0"
                           % (self.x, self.y, self.z, int(round(self.carriage() * self.steps))))
        elif code == "M92":
            replies.append("echo: M92 X400.00 Y400.00 Z%.2f B888.89 Current E678.82" % self.steps)
        elif code == "M218":
            if "Z" in params:
                self.h1 = params["Z"]
            else:
                replies.append("echo:Hotend offsets: 0.00,0.00,0.000 25.56,0.60,%.3f" % self.h1)
        elif code == "G1029":
            n = int(params["P"])
            dx, dy = int(280 / (n - 1)), int(285 / (n - 1))
            self.grid = dict(n=n, start=(52.0, 43.0), spacing=(float(dx), float(dy)),
                             z=[[75.0] * n for _ in range(n)])
            replies += ["leveling OFF", "X:52 - %d" % dx, "Y:43 - %d" % dy, "leveling ON",
                        "Set grid size : %d" % n]
        elif code == "M421":
            if self.fail_m421 and self.grid["n"] == 11:     # refuse the new mesh, not the restore
                replies.append("Error:Mesh XY or IJ cannot be resolved")
            else:
                self.grid["z"][int(params["I"])][int(params["J"])] = params["Z"]
        elif code == "M500":
            self.saved += 1
            replies.append("echo:Settings Stored (1185 bytes; crc 39524)")
        elif code == "M118":
            replies.append(words[1])
        return replies


class FakeBridge(object):
    """The GcodeBridge interface over a FakeSnapmaker."""

    def __init__(self, machine):
        self.machine = machine
        self.fake_acks = 0

    def run(self, commands, timeout=60.0):
        lines = []
        for command in list(commands) + ["M400", "M114"]:
            lines.extend(self.machine.execute(command))
        return lines

    def send(self, commands):
        for command in commands:
            self.machine.execute(command)


class FakeClock(object):
    """Time that passes only when someone sleeps, and heaters that follow it."""

    def __init__(self, machine=None):
        self.now = 1000.0
        self.machine = machine

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class FakePrinter(object):
    """Just enough of OctoPrint's PrinterInterface for the bridge tests."""

    def __init__(self, replies=None, operational=True, swallow_marker=False):
        self.operational = operational
        self.sent = []
        self.replies = dict(replies or {})
        self.bridge = None
        self.acks = 0
        self.swallow_marker = swallow_marker
        self._pending = None

    def is_operational(self):
        return self.operational

    def fake_ack(self):
        self.acks += 1
        if self.swallow_marker and self._pending is not None:
            self.bridge.on_gcode_received(None, self._pending)

    def commands(self, commands, tags=None):
        self._pending = None
        self.sent.extend(commands)
        for command in commands:
            for line in self.replies.get(command.split()[0], []):
                self.bridge.on_gcode_received(None, line)
            if command.startswith("M118 "):
                if self.swallow_marker:
                    self._pending = command.split()[1]
                else:
                    self.bridge.on_gcode_received(None, command.split()[1])
