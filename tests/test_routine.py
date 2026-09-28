"""The whole calibration against the simulated printer."""
import pytest

from nt_pkg import mesh
from nt_pkg.routine import Calibration
from fakes import FakeBridge, FakeClock, FakeSnapmaker

FAST = dict(passes=1, settle_s=1)


def heaters(machine):
    """Heaters that reach their target at once, and nozzles that are cold after M104 S0."""
    temps = {"bed": 75.0, "tool0": 240.0, "tool1": 215.0}

    def temperatures():
        cold = any(c.startswith("M104 T0 S0") for c in machine.sent)
        return dict(temps, tool0=40.0, tool1=40.0) if cold else temps
    return temperatures


def safe_end(machine):
    """The commands from the last M211 S1 on."""
    last = len(machine.sent) - 1 - machine.sent[::-1].index("M211 S1")
    return machine.sent[last:]


def run(machine, settings=None, abort_at=None, bridge=None):
    said = []
    clock = FakeClock()
    cal = Calibration((bridge or FakeBridge)(machine), heaters(machine), said.append,
                      dict(FAST, **(settings or {})), clock=clock)

    def say(payload):
        said.append(payload)
        if payload.get("type") == "wipe":
            cal.confirm_wipe()
        if abort_at and payload.get("phase") == abort_at:
            cal.abort()
    cal._say = say
    cal.run()                          # in this thread: no timing races in tests
    return cal, said


def test_full_run_writes_a_flat_mesh_and_levels_t1():
    m = FakeSnapmaker(h1=-1.083, t1_drop=1.115)
    cal, said = run(m)
    assert cal.phase == "done", cal.error
    assert m.saved == 1                                   # one M500
    assert m.grid["n"] == 11
    # every probed node now trips at -0.72 with the new mesh in force
    for _, _, x, y in mesh.probe_nodes()[::17]:
        expected = m.plate(x, y) - 0.72 - m.mesh_at(x, y)
        assert expected == pytest.approx(-0.72, abs=0.03)
    t1 = cal.result["t1"]
    assert t1["written"] and m.h1 == pytest.approx(-1.115, abs=0.021)
    assert t1["after"] == pytest.approx(m.h1)


def test_run_ends_safe():
    m = FakeSnapmaker()
    run(m)
    tail = safe_end(m)
    for command in ("G0 Z5 F600", "T0", "M104 T0 S0", "M104 T1 S0", "M107 P0", "M107 P1", "M140 S0"):
        assert command in tail
    assert m.soft_endstops and m.tool == 0


def test_offset_is_measured_but_not_written_when_told_not_to():
    m = FakeSnapmaker(h1=-1.083, t1_drop=1.115)
    cal, _ = run(m, dict(write_offset=False))
    assert cal.phase == "done"
    assert m.h1 == -1.083 and cal.result["t1"]["written"] is False


def test_abort_during_the_mesh_leaves_the_old_mesh_and_ends_safe():
    m = FakeSnapmaker()
    old = [col[:] for col in m.grid["z"]]
    cal, said = run(m, abort_at="mesh")
    assert cal.phase == "aborted"
    assert m.grid["z"] == old and m.saved == 0
    assert "M104 T0 S0" in safe_end(m) and m.soft_endstops


def test_refused_m421_puts_the_old_mesh_back():
    m = FakeSnapmaker(fail_m421=True)
    old = [col[:] for col in m.grid["z"]]
    cal, said = run(m)
    assert cal.phase == "failed" and "M421" in cal.error
    assert m.grid["n"] == 6 and m.saved == 0
    for got, want in zip(m.grid["z"], old):
        assert got == pytest.approx(want)


def test_refuses_to_touch_when_levelling_will_not_turn_on():
    # With levelling off, G-code Z 3 is about 39 mm below this plate.
    m = FakeSnapmaker(levelling_fails=True)
    cal, _ = run(m)
    assert cal.phase == "failed" and "levelling" in cal.error
    assert not any(c.startswith("G1 Z") or c.startswith("G0 Z3.00") for c in m.sent)
    assert m.saved == 0


def test_refuses_a_damaged_mesh():
    # A G1029 that stopped half way leaves 75 in some nodes.
    old = [[42.0] * 6 for _ in range(6)]
    old[5][5] = 75.0
    m = FakeSnapmaker(old_grid=old)
    cal, _ = run(m)
    assert cal.phase == "failed" and "damaged" in cal.error
    assert not any(c.startswith("G1 Z") for c in m.sent)


def test_cooling_runs_both_part_fans_and_stops_them():
    m = FakeSnapmaker()
    run(m)
    assert "M106 P0 S255" in m.sent and "M106 P1 S255" in m.sent
    assert "M107 P0" in m.sent and "M107 P1" in m.sent


def test_heaters_go_off_when_nobody_wipes():
    m = FakeSnapmaker()
    said = []
    clock = FakeClock()
    cal = Calibration(FakeBridge(m), lambda: {"bed": 75.0, "tool0": 240.0, "tool1": 215.0},
                      said.append, dict(FAST, wipe_timeout_min=2), clock=clock)
    cal.run()
    assert cal.phase == "failed" and "Continue" in cal.error
    assert clock.now - 1000.0 >= 120.0
    assert "M104 T0 S0" in safe_end(m) and "M104 T1 S0" in safe_end(m)


def test_a_node_off_the_trend_is_found_in_the_wider_window():
    # With a good old mesh every node trips near -0.72. One node sits 0.9 mm lower: past
    # the normal window's floor (hint - 0.8), inside the retry's (hint - 1.1).
    def plane(x, y):
        return 41.0 + 0.0027 * x + 0.0003 * y

    def plate(x, y):
        dip = 0.9 if abs(x - 136.0) < 1 and abs(y - 128.5) < 1 else 0.0
        return plane(x, y) - dip
    old = [[plane(52 + i * 56, 43 + j * 57) for j in range(6)] for i in range(6)]
    m = FakeSnapmaker(plate=plate, old_grid=old)
    cal, _ = run(m)
    assert cal.phase == "done", cal.error
    lows = [c for c in m.sent if c.startswith("G1 Z-1.6")]
    assert lows, "the dipped node was not touched in the wider window"


def test_an_abort_that_wakes_the_bridge_reads_as_aborted():
    m = FakeSnapmaker()

    class Waking(FakeBridge):
        def run(self, commands, timeout=60.0):
            if cal._abort.is_set():
                raise RuntimeError("interrupted while waiting for the printer")
            return FakeBridge.run(self, commands, timeout)

    said = []
    cal = Calibration(Waking(m), heaters(m), said.append, FAST, clock=FakeClock())

    def say(payload):
        said.append(payload)
        if payload.get("type") == "wipe":
            cal.confirm_wipe()
        if payload.get("phase") == "home":
            cal._abort.set()                 # as the plugin does, without the wipe event
    cal._say = say
    cal.run()
    assert cal.phase == "aborted"
    assert said[-1]["type"] == "aborted"


def test_done_is_said_once_the_safe_end_is_sent():
    m = FakeSnapmaker()
    counts = []
    said = []
    cal = Calibration(FakeBridge(m), heaters(m), said.append, FAST, clock=FakeClock())

    def say(payload):
        if payload.get("type") == "wipe":
            cal.confirm_wipe()
        if payload.get("type") == "done":
            counts.append(len(m.sent))
    cal._say = say
    cal.run()
    assert counts and m.sent[counts[0] - 1] == "M140 S0"


def test_result_reports_the_change_from_the_old_mesh():
    m = FakeSnapmaker()
    cal, _ = run(m)
    result = cal.result
    assert len(result["change"]) == 11 and len(result["change"][0]) == 11
    assert result["change_max"] == pytest.approx(max(abs(v) for col in result["change"] for v in col), abs=1e-3)
    assert result["probed_area"] == [52.0, 43.0, 304.0, pytest.approx(299.5)]


def test_skip_wipe_touches_with_cold_nozzles_and_never_heats_them():
    m = FakeSnapmaker(h1=-1.083, t1_drop=1.115)
    said = []
    cal = Calibration(FakeBridge(m), lambda: {"bed": 75.0, "tool0": 30.0, "tool1": 30.0},
                      said.append, dict(FAST, skip_wipe=True), clock=FakeClock())
    cal.run()
    assert cal.phase == "done", cal.error
    assert not any(p.get("type") == "wipe" for p in said)
    assert not any(c.startswith("M104 T0 S2") or c.startswith("M104 T1 S2") for c in m.sent)
    assert not any(" E-" in c for c in m.sent)                 # no retract
    assert not any(c.startswith("M106") for c in m.sent)       # already cold: no fans
    assert m.saved == 1


def test_the_brush_step_does_not_wait_for_the_bed():
    m = FakeSnapmaker()
    order = []
    bed = {"hot": False}

    def temperatures():
        cold = any(c.startswith("M104 T0 S0") for c in m.sent)
        return {"bed": 75.0 if bed["hot"] else 30.0,
                "tool0": 40.0 if cold else 240.0, "tool1": 40.0 if cold else 215.0}

    cal = Calibration(FakeBridge(m), temperatures, order.append, FAST, clock=FakeClock())

    def say(payload):
        order.append(payload)
        if payload.get("type") == "wipe":
            cal.confirm_wipe()
        if "Waiting for the bed" in payload.get("message", ""):
            bed["hot"] = True                                   # the bed gets there later
    cal._say = say
    cal.run()
    assert cal.phase == "done", cal.error
    kinds = [p.get("type") for p in order]
    msgs = [p.get("message", "") for p in order]
    wipe = kinds.index("wipe")
    wait_bed = next(i for i, t in enumerate(msgs) if "Waiting for the bed" in t)
    assert wipe < wait_bed
