"""Grid, drift, averaging, interpolation and the T1 offset."""
import pytest

from nt_pkg import mesh


def test_probe_nodes_leave_out_what_t0_cannot_reach():
    nodes = mesh.probe_nodes()
    assert len(nodes) == 100                       # 10 x 10 of the 11 x 11 grid
    assert max(n[2] for n in nodes) == pytest.approx(304.0)
    assert max(n[3] for n in nodes) == pytest.approx(299.5)


def test_probe_nodes_run_as_a_serpentine():
    rows = mesh.rows_of(mesh.probe_nodes())
    assert len(rows) == 10
    assert [n[0] for n in rows[0]] == list(range(10))
    assert [n[0] for n in rows[1]] == list(range(9, -1, -1))


def test_average_passes():
    avg = mesh.average_passes([(52, 43, 41.0), (52, 43, 41.1), (80, 43, 41.2)])
    assert avg[(52, 43)] == pytest.approx(41.05)
    assert avg[(80, 43)] == pytest.approx(41.2)


def test_as_grid_refuses_holes():
    with pytest.raises(ValueError):
        mesh.as_grid({(0, 0): 1.0, (1, 0): 1.0, (0, 1): 1.0})


def plane(x, y):
    return 41.0 + 0.0027 * x + 0.0003 * y


def measured_plane():
    return {(x, y): plane(x, y) for _, _, x, y in mesh.probe_nodes()}


def test_interpolate_is_exact_on_a_plane_inside_and_outside():
    xs, ys, z = mesh.as_grid(measured_plane())
    for x, y in ((52, 43), (100, 100), (304, 299.5), (332, 323), (20, 10)):
        assert mesh.interpolate(xs, ys, z, x, y) == pytest.approx(plane(x, y))


def test_write_values_follow_the_firmware_spacing():
    # probed at 28 x 28.5, written at the firmware's 28 x 28
    values = mesh.write_values(measured_plane(), (52.0, 43.0), (28.0, 28.0), 11, 0.72)
    assert len(values) == 11 and len(values[0]) == 11
    assert values[0][0] == pytest.approx(plane(52, 43) + 0.72)
    assert values[10][10] == pytest.approx(plane(332, 323) + 0.72)
    assert values[3][7] == pytest.approx(plane(52 + 3 * 28, 43 + 7 * 28) + 0.72)


def test_summary_of_a_plane_is_flat():
    s = mesh.summary(measured_plane())
    assert s["tilt_x_per_100mm"] == pytest.approx(0.27)
    assert s["flatness_rms"] == pytest.approx(0.0, abs=1e-6)
    assert s["nodes"] == 100


def test_spread_between_passes():
    assert mesh.spread([(1, 1, 41.0), (1, 1, 41.03), (2, 1, 41.0)]) == pytest.approx(0.03)
    assert mesh.spread([(1, 1, 41.0)]) is None


def test_t1_offset_raises_a_low_t1():
    # 2026-09-25: M218 -1.127 in force, T1 tripped 0.032 mm higher than T0
    new, mean, spread = mesh.t1_offset(-1.127, [-0.82, -0.76, -0.80, -0.72, -0.78],
                                       [-0.78, -0.72, -0.78, -0.66, -0.78])
    assert mean == pytest.approx(0.032)
    assert new == pytest.approx(-1.159)
    assert spread == pytest.approx(0.06)


def test_t1_offset_skips_points_without_a_trip():
    new, mean, _ = mesh.t1_offset(-1.0, [-0.8, None, -0.8], [-0.7, -0.7, None])
    assert mean == pytest.approx(0.1) and new == pytest.approx(-1.1)
    with pytest.raises(ValueError):
        mesh.t1_offset(-1.0, [None], [None])


def test_firmware_geometry_is_whole_millimetres():
    assert mesh.firmware_geometry(11) == ((52.0, 43.0), (28.0, 28.0))
    assert mesh.firmware_geometry(6) == ((52.0, 43.0), (56.0, 57.0))


def test_change_from_an_old_mesh_of_another_size():
    old = [[plane(52 + i * 56, 43 + j * 57) for j in range(6)] for i in range(6)]
    new = [[plane(52 + i * 28, 43 + j * 28) + 0.05 for j in range(11)] for i in range(11)]
    change = mesh.change_from(old, new, (52.0, 43.0), (28.0, 28.0))
    assert all(v == pytest.approx(0.05) for col in change for v in col)
    assert mesh.change_from(None, new, (52.0, 43.0), (28.0, 28.0)) is None


def test_mesh_problem():
    assert mesh.mesh_problem([[42.0, 42.3], [41.9, 42.1]]) is None
    assert "damaged" in mesh.mesh_problem([[42.0, 75.0], [41.9, 42.1]])
    assert mesh.mesh_problem(None)
