"""Opening-hours parsing (spec §11).

``Branch.opening_hours`` is stored as free text but has a canonical, parseable
shape: ``HH:MM - HH:MM`` on a 24-hour clock (e.g. ``10:00 - 22:00``). The
reservation service reads it to reject a booking that falls outside the branch's
opening hours, and the branch service uses the same parser to reject an edit that
would store an unparseable value. Keeping the one parser here means the two
layers cannot drift on what a valid value is.

Overnight spans (``close <= open``, e.g. ``18:00 - 02:00``) are not supported:
they are rejected as malformed rather than guessed at.
"""

import re
from typing import Optional, Tuple

from backend.app.exceptions import ValidationError

OPENING_HOURS_FORMAT_ERROR = (
    "营业时间格式不正确，应为 HH:MM - HH:MM（例如 10:00 - 22:00）。"
)

_PATTERN = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_opening_hours(text: Optional[str]) -> Tuple[int, int]:
    """Parse ``HH:MM - HH:MM`` into ``(open_minutes, close_minutes)``.

    Minutes are counted from midnight. Raises :class:`ValidationError` when the
    value is missing, not in the canonical format, out of range, or expresses an
    overnight span (``close <= open``).
    """
    match = _PATTERN.match(text or "")
    if not match:
        raise ValidationError(OPENING_HOURS_FORMAT_ERROR)
    open_hour, open_minute, close_hour, close_minute = (
        int(group) for group in match.groups()
    )
    if (
        open_hour > 23
        or close_hour > 23
        or open_minute > 59
        or close_minute > 59
    ):
        raise ValidationError(OPENING_HOURS_FORMAT_ERROR)
    open_minutes = open_hour * 60 + open_minute
    close_minutes = close_hour * 60 + close_minute
    if close_minutes <= open_minutes:
        raise ValidationError(OPENING_HOURS_FORMAT_ERROR)
    return open_minutes, close_minutes


def format_clock(minutes: int) -> str:
    """Render minutes-from-midnight back as ``HH:MM``."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


__all__ = ["OPENING_HOURS_FORMAT_ERROR", "parse_opening_hours", "format_clock"]
