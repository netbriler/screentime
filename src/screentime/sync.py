"""One run: read new usage from the sources, queue it for every server, send what each server accepts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import logging
import os
from pathlib import Path
from typing import Any

from .apps import app_name
from .config import Settings, data_dir
from .locking import process_lock
from .sender import SendError, send_outbox
from .sources import Device, Source, UsageEvent
from .state import load_state, save_state

log = logging.getLogger(__name__)

# SCREENTIME_LIBRARY points at a copied ~/Library for development and tests.
LIBRARY = Path(os.environ.get("SCREENTIME_LIBRARY") or Path.home() / "Library")
BIOME_STREAM = LIBRARY / "Biome" / "streams" / "restricted" / "App.InFocus"
KNOWLEDGEC_DB = LIBRARY / "Application Support" / "Knowledge" / "knowledgeC.db"


@dataclass
class RunResult:
    read: int = 0
    sent: dict[str, int] = field(default_factory=dict)
    queued: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def enabled_sources(settings: Settings) -> list[Source]:
    # Imported lazily so the package works without (or before) a source module.
    sources: list[Source] = []
    if settings.sources.get("biome", True):
        from .sources.biome import BiomeSource

        sources.append(BiomeSource(LIBRARY))
    if settings.sources.get("knowledgec", True):
        from .sources.knowledgec import KnowledgeCSource

        sources.append(KnowledgeCSource(KNOWLEDGEC_DB))
    return sources


def _device(device: Device, settings: Settings) -> dict[str, Any]:
    return {**asdict(device), "name": settings.devices.get(device.id) or device.name}


def _event(event: UsageEvent) -> dict[str, Any]:
    return {
        "device_id": event.device_id,
        "bundle_id": event.bundle_id,
        "app_name": app_name(event.bundle_id),
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
            except Exception as exc:  # one broken source must not stop the other
                log.exception("%s failed", source.name)
                result.errors.append(f"{source.name}: {exc}")
                continue
            payload = [_event(event) for event in events]
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


def denied_paths(settings: Settings) -> list[Path]:
    """Enabled source stores this process may not open: Full Disk Access is missing."""
    paths = []
    for name, path in (("biome", BIOME_STREAM), ("knowledgec", KNOWLEDGEC_DB)):
        if not settings.sources.get(name, True):
            continue
        try:
            if path.is_dir():
                next(path.iterdir(), None)
            else:
                path.open("rb").close()
        except PermissionError:
            paths.append(path)
        except OSError:
            pass  # a missing store is fine: a Mac without an iPhone has no Biome stream
    return paths
