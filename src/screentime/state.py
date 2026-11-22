"""What the tracker remembers between runs, in one JSON file next to nothing else:

* each source's cursor, so a run reads only what is new;
* an outbox per server: events read but not yet accepted by that server. A server that is down keeps its events
  and gets them on a later run; it never holds up the others;
* app names from the App Store lookup, so each app is asked about once.

Events are written to the outbox before the source cursor moves, so nothing read is ever lost.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .config import data_dir

# Events kept per server while it is unreachable; the oldest are dropped beyond this.
MAX_OUTBOX = 200_000


def state_path() -> Path:
    return data_dir() / "state.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or state_path()
    try:
        state = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        state = {}
    state.setdefault("sources", {})
    state.setdefault("outbox", {})
    state.setdefault("last_run", {})
    return state


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    """Write atomically, so a crash or a sleep in the middle never leaves a broken file."""
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for name, events in state["outbox"].items():
        if len(events) > MAX_OUTBOX:
            state["outbox"][name] = events[-MAX_OUTBOX:]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
