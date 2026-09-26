"""One touch, against a simulated floating nozzle."""
import pytest

from nt_pkg.probe import Aborted, Touch
from fakes import FakeBridge, FakeSnapmaker


def touch_on(machine, sensor="left", aborted=lambda: False):
    return Touch(FakeBridge(machine), sensor, 400.0, aborted)


def test_touch_finds_the_trip_within_one_fine_step():
    m = FakeSnapmaker(plate=lambda x, y: 41.5)
    m.soft_endstops = False
    trip, machine = touch_on(m).at(150.0, 150.0, hint=-0.9)
    true_trip = 41.5 - 0.72 - m.mesh_at(150.0, 150.0)
    assert true_trip - 0.02 <= trip <= true_trip + 1e-9
    assert machine == pytest.approx(41.5 - 0.72, abs=0.021)


def test_touch_ends_raised():
    m = FakeSnapmaker(plate=lambda x, y: 41.5)
    m.soft_endstops = False
    touch_on(m).at(150.0, 150.0, hint=-0.9)
    assert m.z == pytest.approx(3.0)


def test_touch_never_goes_below_its_floor():
    m = FakeSnapmaker(plate=lambda x, y: 38.0)     # far below: nothing can trip
    m.soft_endstops = False
    assert touch_on(m).at(150.0, 150.0, hint=-0.9, floor=-1.5) is None
    lowest = min(float(c.split("Z")[1].split()[0]) for c in m.sent
                 if c.startswith("G1 Z"))
    assert lowest >= -1.5 - 1e-9


def test_touch_gives_up_when_the_sensor_is_already_pressed():
    m = FakeSnapmaker(plate=lambda x, y: 60.0)     # plate far above the start height
    m.soft_endstops = False
    assert touch_on(m).at(150.0, 150.0, hint=-0.9) is None


def test_touch_with_t1():
    m = FakeSnapmaker(plate=lambda x, y: 41.5, h1=-1.083, t1_drop=1.115)
    m.soft_endstops = False
    m.tool = 1
    trip1, _ = touch_on(m, "right").at(150.0, 150.0, hint=-0.9)
    m.tool = 0
    trip0, _ = touch_on(m).at(150.0, 150.0, hint=-0.9)
    assert trip1 - trip0 == pytest.approx(0.032, abs=0.021)


def test_abort_stops_at_the_next_step():
    m = FakeSnapmaker(plate=lambda x, y: 41.5)
    with pytest.raises(Aborted):
        touch_on(m, aborted=lambda: True).at(150.0, 150.0, hint=-0.9)
