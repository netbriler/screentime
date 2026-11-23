"""One run: read new usage from the sources, queue it for every server, send what each server accepts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any

from . import session
from .apps import AppInfo, resolve
from .collector import snapshot_library
from .config import Settings, data_dir
from .locking import process_lock
from .sender import SendError, send_outbox
from .sources import Device, Source, UsageEvent
from .state import load_state, save_state

log = logging.getLogger(__name__)

# The collector's copy of the Screen Time stores; the tracker never reads the real ones. SCREENTIME_LIBRARY points
# elsewhere for development and tests.
LIBRARY = Path(os.environ.get("SCREENTIME_LIBRARY") or snapshot_library())
# The collector's exports: this Mac's knowledgeC rows, and the synced devices from Biome's sync.db.
KNOWLEDGEC_EXPORT = Path(os.environ.get("SCREENTIME_KNOWLEDGEC_EXPORT") or snapshot_library().parent / "knowledgec")
BIOME_EXPORT = Path(os.environ.get("SCREENTIME_BIOME_EXPORT") or snapshot_library().parent / "biome")


@dataclass
class RunResult:
    read: int = 0
    sent: dict[str, int] = field(default_factory=dict)
    queued: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    on_screen: bool | None = None  # this account's session on screen (False: another account is)


def _exported(path: Path) -> dict:
    """A JSON map the collector wrote, or an empty one before its first run."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def enabled_sources(settings: Settings) -> list[Source]:
    # Imported lazily so the package works without (or before) a source module.
    sources: list[Source] = []
    if settings.sources.get("biome", True):
        from .sources.biome import BiomeSource

        sources.append(
            BiomeSource(
                LIBRARY,
                models=_exported(KNOWLEDGEC_EXPORT / "peers.json"),
                peers=_exported(BIOME_EXPORT / "devices.json"),
            )
        )
    if settings.sources.get("knowledgec", True):
        from .sources.knowledgec import KnowledgeCExportSource

        sources.append(KnowledgeCExportSource(KNOWLEDGEC_EXPORT))
    return sources


# Events already sent are remembered this long after sending, so the rows the collector reads again (the last hour)
# are not sent again.
SENT_MEMORY = timedelta(hours=3)


def _new_events(events: list[UsageEvent], sent: dict[str, list[str]]) -> list[UsageEvent]:
    """Drop events sent before with the same end; remember the rest. ``sent`` lives in state.json."""
    now = datetime.now(UTC)
    horizon = (now - SENT_MEMORY).isoformat()
    for key in [key for key, (_, sent_at) in sent.items() if sent_at < horizon]:
        del sent[key]
    fresh = []
    for event in events:
        key = f"{event.device_id}|{event.bundle_id}|{event.start.isoformat()}"
        end = event.end.isoformat()
        if key not in sent or sent[key][0] != end:
            sent[key] = [end, now.isoformat()]
            fresh.append(event)
    return fresh


def _device(device: Device, settings: Settings) -> dict[str, Any]:
    return {**asdict(device), "name": settings.devices.get(device.id) or device.name}


def _event(event: UsageEvent, apps: dict[str, AppInfo], background: bool = False) -> dict[str, Any]:
    app = apps.get(event.bundle_id, AppInfo())
    return {
        "device_id": event.device_id,
        "bundle_id": event.bundle_id,
        "app_name": app.name,
        "app_category": app.category,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "background": background,
    }


def _background(event: UsageEvent, away: session.Away, now: datetime) -> bool:
    """This Mac's event while another account was on screen (see session); other devices are never marked."""
    return event.device_id.startswith("mac:") and session.in_background(event.start, event.end, away, now)


# Every server says in each reply what it holds: the start of its newest event per device. If that goes back
# compared with what it confirmed before (its database was reset or restored), the tracker reads the history again
# and queues for that server everything newer than what it still has.
CHECK_EVERY = timedelta(hours=1)  # with nothing new to send, ask each server this often anyway
REPLAY_FOR = timedelta(minutes=5)  # long enough for the collector to export knowledgeC again
# During a replay, healthy servers get only the last day again (they just update those events).
REPLAY_OVERLAP = timedelta(days=1)


def start_replay(state: dict[str, Any], *, server: str | None = None) -> None:
    """Read all history again for a while; ``server`` forgets what that server confirmed, so it gets everything."""
    if server is not None:
        state.setdefault("latest", {})[server] = {}
    state["sources"] = {}
    state["replay_until"] = (datetime.now(UTC) + REPLAY_FOR).isoformat()
    # The collector exports knowledgeC from the beginning again when its cursor is gone.
    (KNOWLEDGEC_EXPORT / "cursor").unlink(missing_ok=True)


def _lost_data(before: dict[str, str], now: dict[str, str]) -> list[str]:
    """Devices whose newest event on the server went back, or vanished, since the last reply."""
    return [device for device, latest in before.items() if now.get(device, "") < latest]


def _key(event: dict[str, Any]) -> tuple[str, str, str, str]:
    return event["device_id"], event["bundle_id"], event["start"], event["end"]


def _wanted_in_replay(event: dict[str, Any], latest: dict[str, str]) -> bool:
    known = latest.get(event["device_id"])
    if not known:
        return True
    floor = (datetime.fromisoformat(known) - REPLAY_OVERLAP).isoformat()
    return event["start"] > floor


def _forget_removed_servers(state: dict[str, Any], names: set[str]) -> None:
    """Drop what is kept for servers no longer in the config: nothing would ever send their queues. A disabled
    server is still in the config and keeps its queue."""
    for key in ("outbox", "latest", "last_contact", "server_health"):
        kept = state.get(key)
        if isinstance(kept, dict):
            for name in [name for name in kept if name not in names]:
                del kept[name]


def run(settings: Settings) -> RunResult:
    result = RunResult()
    servers = [server for server in settings.servers if server.enabled]
    with process_lock(data_dir() / "screentime.lock"):
        state = load_state()
        _forget_removed_servers(state, {server.name for server in settings.servers})
        now = datetime.now(UTC)
        replay = now.isoformat() < state.get("replay_until", "")
        result.on_screen = session.on_console()
        away = session.load()  # the console agent's samples
        latest_by_server = state.setdefault("latest", {})
        devices: dict[str, dict[str, Any]] = {}
        for source in enabled_sources(settings):
            try:
                for device in source.devices():
                    devices[device.id] = _device(device, settings)
                events, source_state = source.read(state["sources"].get(source.name))
                fresh = {id(event) for event in _new_events(events, state.setdefault("sent", {}))}
            except Exception as exc:  # one broken source must not stop the other
                log.exception("%s failed", source.name)
                result.errors.append(f"{source.name}: {exc}")
                continue
            apps = resolve(
                {event.bundle_id for event in events},
                state.setdefault("app_names", {}),
                lookup=settings.lookup_enabled,
                countries=settings.lookup_countries,
                timeout=settings.request_timeout,
            )
            # In a replay, an old event read again is not new: only servers missing it get it (_wanted_in_replay).
            recent = (now - SENT_MEMORY).isoformat()
            payload = [
                (
                    id(event) in fresh and (not replay or event.start.isoformat() >= recent),
                    _event(event, apps, _background(event, away, now)),
                )
                for event in events
            ]
            result.read += sum(is_fresh for is_fresh, _ in payload)
            for server in servers:
                latest = latest_by_server.get(server.name, {})
                outbox = state["outbox"].setdefault(server.name, [])
                queued = {_key(event) for event in outbox}  # never queue the same event twice
                for is_fresh, event in payload:
                    if (is_fresh or (replay and _wanted_in_replay(event, latest))) and _key(event) not in queued:
                        outbox.append(event)
                        queued.add(_key(event))
            # The cursor moves only together with the queued events, in one atomic write.
            state["sources"][source.name] = source_state
            save_state(state)

        lost = []
        health = state.setdefault("server_health", {})
        for server in servers:
            outbox = state["outbox"].setdefault(server.name, [])
            contacts = state.setdefault("last_contact", {})
            due = (
                now.isoformat()
                > (
                    datetime.fromisoformat(contacts.get(server.name, "1970-01-01T00:00:00+00:00")) + CHECK_EVERY
                ).isoformat()
            )
            try:
                sent, latest = send_outbox(
                    server,
                    outbox,
                    list(devices.values()),
                    settings.request_timeout,
                    check=due,
                    ca_bundle=settings.ca_bundle,
                )
                result.sent[server.name] = sent
                # Reached: whatever failed before is over.
                health[server.name] = {"ok_at": now.isoformat()}
                if latest is not None:
                    contacts[server.name] = now.isoformat()
                    if gone := _lost_data(latest_by_server.get(server.name, {}), latest):
                        lost.append(server.name)
                        result.errors.append(f"{server.name}: lost data for {len(gone)} device(s); sending it again")
                        latest = {}  # it gets everything it no longer has
                    latest_by_server[server.name] = latest
            except SendError as exc:
                result.errors.append(str(exc))
                previous = health.get(server.name, {})
                health[server.name] = {
                    **previous,
                    "error": str(exc),
                    "error_since": previous.get("error_since") or now.isoformat(),
                }
            result.queued[server.name] = len(outbox)
            save_state(state)
        for name in lost:
            start_replay(state, server=name)
        state["last_run"] = asdict(result)
        save_state(state)
    return result
