"""Parsers and the bridge, fed with lines the printer sent on 2026-09-25."""
import logging

import pytest

from nt_pkg import gcode
from fakes import FakePrinter

M119 = [
    "Reporting endstop status",
    "x_min: open", "x_max: open", "y_min: open", "y_max: open", "z_min: open", "z_max: open",
    "z_probe_proximity_switch: open",
    "z_probe_left_optocoupler: TRIGGERED",
    "z_probe_right_optocoupler: TRIGGERED",
    "filament_extruder0: open", "filament_extruder1: open",
]

M420_V = [
    "Bilinear Leveling Grid:",
    "      0      1      2      3      4      5",
    " 0 +41.972 +42.069 +42.268 +42.345 +42.408 +42.537",
    " 1 +42.030 +42.180 +42.358 +42.428 +42.535 +42.625",
    " 2 +42.032 +42.188 +42.389 +42.493 +42.605 +42.735",
    " 3 +42.032 +42.244 +42.428 +42.539 +42.672 +42.777",
    " 4 +42.007 +42.182 +42.352 +42.465 +42.620 +42.739",
    " 5 +41.952 +42.064 +42.239 +42.365 +42.548 +42.719",
    "",
    "Subdivided with CATMULL ROM Leveling Grid:",
    " 0 +41.972 +41.991 +42.010",
]


def test_optocouplers():
    assert gcode.optocoupler(M119, "left") is True
    assert gcode.optocoupler(M119, "right") is True
    assert gcode.optocoupler([l.replace("left_optocoupler: TRIGGERED", "left_optocoupler: open")
                              for l in M119], "left") is False
    assert gcode.optocoupler(["ok"], "left") is None


def test_count_z_uses_the_step_counter():
    lines = ["X:304.00 Y:128.50 Z:-1.12 E:-14.00 Count X:134400 Y:60600 Z:16656 B:0"]
    assert gcode.count_z(lines, 400.0) == pytest.approx(41.64)
    assert gcode.count_z(["ok"], 400.0) is None


def test_t1_offset():
    assert gcode.t1_offset(["echo:Hotend offsets: 0.00,0.00,0.000 25.56,0.60,-1.150"]) == (25.56, 0.6, -1.15)
    assert gcode.t1_offset(["ok"]) is None


def test_grid_geometry_from_g1029():
    lines = ["leveling OFF", "X:52 - 28", "Y:43 - 28", "leveling ON", "Set grid size : 11", "ok"]
    assert gcode.grid_geometry(lines) == (11, (52.0, 43.0), (28.0, 28.0))
    assert gcode.grid_geometry(["Set grid size : 11"]) is None


def test_grid_geometry_ignores_position_lines():
    lines = ["X:160.00 Y:30.00 Z:60.00 E:-8.00 Count X:76800 Y:21200 Z:40934 B:0",
             "X:52 - 28", "Y:43 - 28", "Set grid size : 11"]
    assert gcode.grid_geometry(lines) == (11, (52.0, 43.0), (28.0, 28.0))


def test_bilinear_grid_is_x_major_and_stops_at_the_subdivided_grid():
    z = gcode.bilinear_grid(M420_V)
    assert len(z) == 6 and len(z[0]) == 6
    assert z[0][0] == 41.972          # X52 Y43
    assert z[5][0] == 42.537          # X332 Y43: the row is along X
    assert z[0][5] == 41.952          # X52 Y328


def test_bilinear_grid_accepts_recv_prefix():
    z = gcode.bilinear_grid(["Recv: " + l for l in M420_V])
    assert z is not None and z[5][5] == 42.719


def test_steps_per_mm():
    lines = ["echo: M92 X400.00 Y400.00 Z400.00 B888.89 Current E667.22, BACKUP SINGLE E212.21"]
    assert gcode.z_steps_per_mm(lines) == 400.0
    assert gcode.z_steps_per_mm(["ok"]) == 400.0


@pytest.mark.parametrize("line, split", [
    ("oactive extruder mismatch target: 0!", True),
    ("active extruder mismatch target: 1!ok", True),
    ("active extruder mismatch target: 0!", False),
    ("clear extruder mismatch error!", False),
    ("ok", False),
    ("ok T:46.00 /0.00 B:75.20 /75.00", False),
])
def test_split_ok(line, split):
    assert gcode.is_split_ok(line) is split


def _bridge(**kw):
    printer = FakePrinter(**kw)
    bridge = gcode.GcodeBridge(printer, logging.getLogger("test"))
    printer.bridge = bridge
    return printer, bridge


def test_bridge_returns_the_lines_up_to_its_marker():
    printer, bridge = _bridge(replies={"M119": M119})
    lines = bridge.run(["M119"], timeout=1.0)
    assert "z_probe_left_optocoupler: TRIGGERED" in lines
    assert printer.sent[-3:] == ["M400", "M114", "M118 NTSYNC1"]


def test_bridge_gives_a_lost_ok_back(monkeypatch):
    # the split arrives as a reply line: the bridge must send one fake ack
    printer, bridge = _bridge(replies={"G1": ["oactive extruder mismatch target: 0!", "k"]})
    fired = []
    monkeypatch.setattr(gcode.threading, "Timer", lambda delay, fn: type(
        "T", (), {"start": lambda self: fired.append(fn())})())
    bridge.run(["G1 Z-0.9 F120"], timeout=1.0)
    assert printer.acks == 1 and bridge.fake_acks == 1


def test_bridge_acks_once_when_the_marker_never_comes():
    printer, bridge = _bridge(swallow_marker=True)
    lines = bridge.run(["M119"], timeout=0.05)
    assert printer.acks == 1 and isinstance(lines, list)


def test_bridge_refuses_when_disconnected():
    _, bridge = _bridge(operational=False)
    with pytest.raises(RuntimeError):
        bridge.run(["M119"])


def test_levelling_state():
    assert gcode.levelling(["leveling ON", "echo:Bed Leveling ON", "echo:Fade Height OFF", "ok"]) is True
    assert gcode.levelling(["Error:Failed to enable Bed Leveling", "echo:Bed Leveling OFF"]) is False
    assert gcode.levelling(["Recv: echo:Bed Leveling ON"]) is True
    # G1029 says "leveling ON" in lower case, which is not the M420 report
    assert gcode.levelling(["leveling OFF", "leveling ON", "ok"]) is None


def test_bridge_leaves_a_split_alone_when_told_to(monkeypatch):
    printer, bridge = _bridge(replies={"G1": ["oactive extruder mismatch target: 0!", "k"]})
    bridge.fix_split_ok = False
    fired = []
    monkeypatch.setattr(gcode.threading, "Timer", lambda delay, fn: type(
        "T", (), {"start": lambda self: fired.append(fn())})())
    bridge.run(["G1 Z-0.9 F120"], timeout=1.0)
    assert printer.acks == 0 and bridge.fake_acks == 0
