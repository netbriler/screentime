"""Whether this account's session is on screen, so this Mac's usage from a switched-out session can be marked.

With fast user switching, Screen Time in the account left behind keeps counting the app that was in front as used
the whole time another account is on screen. The tracker runs every minute in this session, on screen or not, and
looks at who owns ``/dev/console``: the account on screen. Each stretch it finds the console with someone else is
kept in state.json as ``away`` ``[start, end]`` (``end`` null while it lasts). Events are only marked, never cut.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
from typing import Any

CONSOLE = "/dev/console"
KEEP = timedelta(days=35)  # longer than macOS keeps history, so a resend marks old events the same way
# A sample comes once a minute, so a stretch is known to a minute; less overlap than this is not counted.
MIN_OVERLAP = timedelta(seconds=60)


def on_console() -> bool | None:
    """True when this account is on screen, None when it cannot be told."""
    try:
        return os.stat(CONSOLE).st_uid == os.getuid()
    except OSError:
        return None


def record(state: dict[str, Any], now: datetime, on: bool | None) -> None:
    """Open or close an ``away`` stretch in ``state`` from one sample."""
    away: list[list[str | None]] = state.setdefault("away", [])
    is_open = bool(away) and away[-1][1] is None
    if on is False and not is_open:
        away.append([now.isoformat(), None])
    elif on is True and is_open:
        away[-1][1] = now.isoformat()
    horizon = (now - KEEP).isoformat()
    away[:] = [stretch for stretch in away if stretch[1] is None or stretch[1] >= horizon]


def in_background(start: datetime, end: datetime, away: list[list[str | None]], now: datetime) -> bool:
    """Whether a stretch from ``start`` to ``end`` was at least a minute while another account was on screen."""
    overlap = timedelta()
    for away_start, away_end in away:
        stretch_start = datetime.fromisoformat(away_start)
        stretch_end = datetime.fromisoformat(away_end) if away_end else now
        overlap += max(timedelta(), min(end, stretch_end) - max(start, stretch_start))
    return overlap >= MIN_OVERLAP
