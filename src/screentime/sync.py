"""One run: read new usage from the sources, queue it for every server, send what each server accepts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
from typing import Any

from .apps import resolve
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


def _event(event: UsageEvent, names: dict[str, str]) -> dict[str, Any]:
    return {
        "device_id": event.device_id,
        "bundle_id": event.bundle_id,
        "app_name": names.get(event.bundle_id, ""),
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
    }


def run(settings: Settings) -> RunResult:
    result = RunResult()
    servers = [server for server in settings.servers if server.enabled]
    with process_lock(data_dir() / "screentime.lock"):
        state = load_state()
        devices: dict[str, dict[str, Any]] = {}
        for source in enabled_sources(settings):
            try:
                for device in source.devices():
                    devices[device.id] = _device(device, settings)
                events, source_state = source.read(state["sources"].get(source.name))
                events = _new_events(events, state.setdefault("sent", {}))
            except Exception as exc:  # one broken source must not stop the other
                log.exception("%s failed", source.name)
                result.errors.append(f"{source.name}: {exc}")
                continue
            names = resolve(
                {event.bundle_id for event in events},
                state.setdefault("app_names", {}),
                lookup=settings.lookup_enabled,
                country=settings.lookup_country,
                timeout=settings.request_timeout,
            )
            payload = [_event(event, names) for event in events]
            result.read += len(payload)
            for server in servers:
                state["outbox"].setdefault(server.name, []).extend(payload)
            # The cursor moves only together with the queued events, in one atomic write.
            state["sources"][source.name] = source_state
            save_state(state)

        for server in servers:
            outbox = state["outbox"].setdefault(server.name, [])
            try:
                result.sent[server.name] = send_outbox(
                    server, outbox, list(devices.values()), settings.request_timeout
                )
            except SendError as exc:
                result.errors.append(str(exc))
            result.queued[server.name] = len(outbox)
            save_state(state)
        state["last_run"] = asdict(result)
        save_state(state)
    return result
