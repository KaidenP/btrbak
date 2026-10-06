import pytest

from btrbak import timespan


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1s", 1),
        ("2min", 120),
        ("3h", 10800),
        ("1d", 86400),
        ("2w", 2 * 7 * 86400),
        ("1mo", 30 * 86400),
        ("1y", 365 * 86400),
        (42, 42),
        (" 1d ", 86400),
    ],
)
def test_parse_valid(value, seconds):
    assert timespan.parse(value) == seconds


def test_parse_never():
    assert timespan.parse(-1) == timespan.NEVER
    assert timespan.parse("-1") == timespan.NEVER
    assert timespan.is_never(timespan.parse(-1))


@pytest.mark.parametrize("value", ["", "d", "1x", "0d", "-2", None, True, "1D", 1.5])
def test_parse_invalid(value):
    with pytest.raises(ValueError):
        timespan.parse(value)


def test_is_never_zero_is_false():
    assert not timespan.is_never(0)
