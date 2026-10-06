"""Human-readable timespan parsing.

Values look like ``1d``, ``12h``, ``2w``, ``1mo``. The special sentinel ``-1``
means "never" and is accepted for ``freq.*`` configuration values only.
"""

import re

NEVER = -1

_UNITS = {
    "s": 1,
    "min": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
    "mo": 30 * 86400,
    "y": 365 * 86400,
}

_PATTERN = re.compile(r"^(\d+)(s|min|h|d|w|mo|y)$")


def is_never(seconds: int) -> bool:
    """Return True when *seconds* is the ``-1`` "never" sentinel."""
    return seconds == NEVER


def parse(value) -> int:
    """Parse a timespan into a number of seconds.

    Returns :data:`NEVER` for ``-1``.

    A bare positive integer is accepted as a number of *seconds* (not a unit
    quantity); the string form of the same number (e.g. ``"3600"``) is
    rejected. This asymmetry is historical and retained for compatibility with
    existing configs.
    """
    if isinstance(value, bool):
        raise ValueError(f"invalid timespan: {value!r}")

    if isinstance(value, int):
        if value == NEVER:
            return NEVER
        if value > 0:
            return value
        raise ValueError(
            f"invalid timespan: {value!r} (integer values must be positive, or -1)"
        )

    text = str(value).strip()
    if text == "-1":
        return NEVER

    match = _PATTERN.match(text)
    if not match:
        raise ValueError(
            f"invalid timespan: {value!r} (expected e.g. '1d', '12h', '2w', '1mo', or -1)"
        )

    count = int(match.group(1))
    if count <= 0:
        raise ValueError(f"invalid timespan: {value!r} (must be positive)")
    return count * _UNITS[match.group(2)]
