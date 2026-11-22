"""Readable app names for bundle ids: the shipped list, then this Mac's installed apps (Spotlight), then, unless
turned off, Apple's public App Store lookup for the rest (mostly iPhone-only apps).

Only bundle ids go to Apple, nothing about usage. Lookups are cached in state.json: a found name is kept, a miss is
asked again after a week. Apps found no way go to the server with an empty name.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import requests


@cache
def _shipped() -> dict[str, str]:
    data = json.loads(files("screentime").joinpath("data/apps.json").read_text("utf-8"))
    return {bundle.lower(): entry[0] for bundle, entry in data.get("apps", {}).items()}


@cache
def _installed(bundle_id: str) -> str:
    """Name of an app installed on this Mac with this bundle id, from Spotlight; empty when not found."""
    if sys.platform != "darwin" or '"' in bundle_id:
        return ""
    try:
        paths = subprocess.run(
            ["mdfind", f'kMDItemCFBundleIdentifier == "{bundle_id}"'],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return ""
    apps = sorted((p for p in paths if p.endswith(".app")), key=lambda p: not p.startswith("/Applications/"))
    return Path(apps[0]).stem if apps else ""


def app_name(bundle_id: str) -> str:
    return _shipped().get(bundle_id.lower()) or _installed(bundle_id)


LOOKUP_URL = "https://itunes.apple.com/lookup"
LOOKUP_BATCH = 50
LOOKUP_BATCHES_PER_RUN = 4  # Apple allows about 20 requests a minute
LOOKUP_RETRY = timedelta(days=7)


def resolve(
    bundle_ids: set[str], cache: dict[str, Any], *, lookup: bool, country: str, timeout: float
) -> dict[str, str]:
    """Names for these bundle ids; ``cache`` (from state.json) keeps App Store answers between runs."""
    names = {bundle_id: app_name(bundle_id) for bundle_id in bundle_ids}
    now = datetime.now(UTC)
    for bundle_id, name in list(names.items()):
        if not name and (cached := cache.get(bundle_id)):
            names[bundle_id] = cached.get("name") or ""
    if not lookup:
        return names
    due = [
        bundle_id
        for bundle_id, name in names.items()
        if not name
        and (
            bundle_id not in cache
            or not cache[bundle_id].get("name")
            and now - datetime.fromisoformat(cache[bundle_id]["checked_at"]) > LOOKUP_RETRY
        )
    ]
    for start in range(0, min(len(due), LOOKUP_BATCH * LOOKUP_BATCHES_PER_RUN), LOOKUP_BATCH):
        batch = due[start : start + LOOKUP_BATCH]
        try:
            found = _itunes(batch, country, timeout)
        except (requests.RequestException, ValueError):
            break  # offline or rate-limited: try again on a later run
        for bundle_id in batch:
            name = found.get(bundle_id.lower(), "")
            cache[bundle_id] = {"name": name, "checked_at": now.isoformat()}
            if name:
                names[bundle_id] = name
    return names


def _itunes(bundle_ids: list[str], country: str, timeout: float) -> dict[str, str]:
    response = requests.get(
        LOOKUP_URL,
        params={"bundleId": ",".join(bundle_ids), "country": country, "lang": "en_us"},
        timeout=timeout,
    )
    response.raise_for_status()
    found = {}
    for result in response.json().get("results", []):
        if result.get("bundleId") and result.get("trackName"):
            # "Spotify: Music and Podcasts" -> "Spotify"
            name = str(result["trackName"]).split(":")[0].split(" - ")[0].strip()
            found[str(result["bundleId"]).lower()] = name or str(result["trackName"])
    return found
