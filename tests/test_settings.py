"""The settings checks: every value ends up in a G-code command."""
import pytest

from nt_pkg import routine
from nt_pkg.settings import DEFAULTS, SettingsError, parse_points, routine_config


def test_defaults_pass_and_match_the_routine():
    config = routine_config(DEFAULTS)
    for key, value in routine.DEFAULTS.items():
        assert config[key] == pytest.approx(value) if isinstance(value, float) else config[key] == value


def test_numbers_are_coerced_from_text():
    config = routine_config(dict(DEFAULTS, bed_temp="80", passes="3", trip_to_zero="0.7"))
    assert config["bed_temp"] == 80 and config["passes"] == 3 and config["trip_to_zero"] == 0.7


@pytest.mark.parametrize("key, value", [
    ("bed_temp", 130), ("t0_temp", 120), ("passes", 0), ("passes", 9),
    ("trip_to_zero", 2.0), ("cool_below", 95), ("retract_mm", -1), ("bed_temp", "hot"),
])
def test_out_of_range_numbers_are_refused(key, value):
    with pytest.raises(SettingsError):
        routine_config(dict(DEFAULTS, **{key: value}))


def test_points_parse_in_several_forms():
    assert parse_points("p", "178, 171; 100,100\n260 100", 2) == [[178, 171], [100, 100], [260, 100]]
    assert parse_points("p", [[178, 171], [100, 100]], 2) == [[178, 171], [100, 100]]
    assert parse_points("p", [160, 30, 60], 3) == [[160, 30, 60]]


@pytest.mark.parametrize("text", ["178", "178, 171, 5", "178, abc", ""])
def test_bad_points_are_refused(text):
    with pytest.raises(SettingsError):
        parse_points("p", text, 2)


@pytest.mark.parametrize("key, value", [
    ("reference", "20, 171"),                       # off the touched area, towards the frame
    ("reference", "178, 171; 100, 100"),            # two points
    ("offset_points", "178, 171; 330, 100"),        # past T0's reach
    ("park", "160, 30, 5"),                         # too low to park over a plate
    ("park", "400, 30, 60"),
])
def test_points_outside_the_safe_area_are_refused(key, value):
    with pytest.raises(SettingsError):
        routine_config(dict(DEFAULTS, **{key: value}))


def test_booleans_from_text():
    assert routine_config(dict(DEFAULTS, write_offset="false"))["write_offset"] is False
    assert routine_config(dict(DEFAULTS, write_offset="true"))["write_offset"] is True
