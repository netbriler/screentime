"""Whether this account's session is on screen, so this Mac's usage from a switched-out session can be marked.

With fast user switching, Screen Time in the account left behind keeps counting the app that was in front as used
the whole time another account is on screen. A third launchd agent runs ``python -m screentime.session`` every
``launchd.CONSOLE_INTERVAL_S`` seconds in this session, on screen or not, and looks at who owns ``/dev/console``: the
account on screen. Each stretch with someone else on it is kept in ``away.json`` as ``[start, end]`` (``end`` null
while it lasts); only this agent writes the file, the tracker only reads it. Events are only marked, never cut.

It imports nothing but this module and the config's paths, so a run costs a Python start and one ``stat``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile

from .config import data_dir

CONSOLE = "/dev/console"
KEEP = timedelta(days=35)  # longer than macOS keeps history, so a resend marks old events the same way
# Less time than this with another account on screen does not mark an event: a sample is seconds apart, and an app
# left in front during a switch is either a glance or the whole time away.
MIN_OVERLAP = timedelta(seconds=60)

Away = list[list[str | None]]


def away_path() -> Path:
    return data_dir() / "away.json"


def on_console() -> bool | None:
    """True when this account is on screen, None when it cannot be told."""
    try:
        return os.stat(CONSOLE).st_uid == os.getuid()
    except OSError:
        return None


def load(path: Path | None = None) -> Away:
    try:
        data = json.loads((path or away_path()).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(away: Away, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".away-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(away, handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def record(away: Away, now: datetime, on: bool | None) -> bool:
    """Open or close a stretch in ``away`` from one sample; True when it changed."""
    is_open = bool(away) and away[-1][1] is None
    before = len(away)
    changed = False
    if on is False and not is_open:
        away.append([now.isoformat(), None])
        changed = True
    elif on is True and is_open:
        away[-1][1] = now.isoformat()
        changed = True
    horizon = (now - KEEP).isoformat()
    away[:] = [stretch for stretch in away if stretch[1] is None or stretch[1] >= horizon]
    return changed or len(away) != before


def sample(path: Path | None = None, now: datetime | None = None) -> bool | None:
    """Take one sample and write ``away.json`` when it changes anything."""
    path = path or away_path()
    away = load(path)
    on = on_console()
    if record(away, now or datetime.now(UTC), on):
        _save(away, path)
    return on


def in_background(start: datetime, end: datetime, away: Away, now: datetime) -> bool:
    """Whether a stretch from ``start`` to ``end`` was at least a minute while another account was on screen."""
    overlap = timedelta()
    for away_start, away_end in away:
        stretch_start = datetime.fromisoformat(away_start)
        stretch_end = datetime.fromisoformat(away_end) if away_end else now
        overlap += max(timedelta(), min(end, stretch_end) - max(start, stretch_start))
    return overlap >= MIN_OVERLAP


if __name__ == "__main__":
    sample()
