"""App names and categories for bundle ids: the shipped list, then this Mac's installed apps (Spotlight and their
Info.plist), then, unless turned off, Apple's public App Store lookup for the rest (mostly iPhone-only apps).

The category (Social, Media, Coding, Gaming, …) is a hint for the server; what counts as useful is decided there.
Only bundle ids go to Apple, nothing about usage. Lookups are cached in state.json: a found app is kept, a miss is
asked again after a week. Apps found no way go to the server with an empty name and category.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.resources import files
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from typing import Any, NamedTuple

import requests


class AppInfo(NamedTuple):
    name: str = ""
    category: str = ""


# LSApplicationCategoryType of Mac apps (without "public.app-category.") -> category; any "*-games" type is Gaming.
MAC_CATEGORIES = {
    "developer-tools": "Coding",
    "productivity": "Productivity",
    "business": "Productivity",
    "education": "Productivity",
    "reference": "Productivity",
    "graphics-design": "Productivity",
    "utilities": "Utilities",
    "lifestyle": "Utilities",
    "weather": "Utilities",
    "travel": "Utilities",
    "navigation": "Utilities",
    "music": "Media",
    "video": "Media",
    "photography": "Media",
    "entertainment": "Media",
    "news": "Media",
    "sports": "Media",
    "social-networking": "Social",
    "finance": "Finance",
    "healthcare-fitness": "Health & Fitness",
    "medical": "Health & Fitness",
    "games": "Gaming",
}


@cache
def _data() -> dict[str, Any]:
    return json.loads(files("screentime").joinpath("data/apps.json").read_text("utf-8"))


@cache
def _shipped() -> dict[str, AppInfo]:
    return {bundle.lower(): AppInfo(entry[0], entry[1]) for bundle, entry in _data().get("apps", {}).items()}


@cache
def _installed(bundle_id: str) -> AppInfo:
    """An app installed on this Mac with this bundle id, from Spotlight and its Info.plist; empty when not found."""
    if sys.platform != "darwin" or '"' in bundle_id:
        return AppInfo()
    try:
        paths = subprocess.run(
            ["mdfind", f'kMDItemCFBundleIdentifier == "{bundle_id}"'],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return AppInfo()
    for path in sorted((p for p in paths if p.endswith(".app")), key=lambda p: not p.startswith("/Applications/")):
        try:
            info = plistlib.loads((Path(path) / "Contents" / "Info.plist").read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            return AppInfo(Path(path).stem)  # the sandbox may refuse apps in the home folder: the name still helps
        kind = str(info.get("LSApplicationCategoryType", "")).removeprefix("public.app-category.")
        category = "Gaming" if kind.endswith("-games") else MAC_CATEGORIES.get(kind, "")
        return AppInfo(str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or Path(path).stem), category)
    return AppInfo()


def app_info(bundle_id: str) -> AppInfo:
    """Offline only: the shipped list, then the installed app."""
    shipped = _shipped().get(bundle_id.lower())
    if shipped:
        return shipped
    return _installed(bundle_id)


LOOKUP_URL = "https://itunes.apple.com/lookup"
LOOKUP_BATCH = 50
LOOKUP_BATCHES_PER_RUN = 4  # Apple allows about 20 requests a minute
LOOKUP_RETRY = timedelta(days=7)


def resolve(
    bundle_ids: set[str],
    cache: dict[str, Any],
    *,
    lookup: bool,
    country: str,
    timeout: float,
) -> dict[str, AppInfo]:
    """Names and categories for these bundle ids; ``cache`` (from state.json) keeps App Store answers between runs."""
    apps = {bundle_id: app_info(bundle_id) for bundle_id in bundle_ids}
    now = datetime.now(UTC)
    for bundle_id, info in list(apps.items()):
        cached = cache.get(bundle_id)
        if not info.name and cached and "category" in cached:
            apps[bundle_id] = AppInfo(cached.get("name") or "", cached.get("category") or "")
    if not lookup:
        return apps
    due = [
        bundle_id
        for bundle_id, info in apps.items()
        if not info.name
        and (
            bundle_id not in cache
            # cached before categories were kept: ask again once
            or "category" not in cache[bundle_id]
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
            info = found.get(bundle_id.lower(), AppInfo())
            cache[bundle_id] = {"name": info.name, "category": info.category, "checked_at": now.isoformat()}
            if info.name:
                apps[bundle_id] = info
    return apps


def _itunes(bundle_ids: list[str], country: str, timeout: float) -> dict[str, AppInfo]:
    response = requests.get(
        LOOKUP_URL,
        params={"bundleId": ",".join(bundle_ids), "country": country, "lang": "en_us"},
        timeout=timeout,
    )
    response.raise_for_status()
    genres = _data().get("genres", {})
    found = {}
    for result in response.json().get("results", []):
        if not result.get("bundleId") or not result.get("trackName"):
            continue
        # "Spotify: Music and Podcasts" -> "Spotify"
        name = str(result["trackName"]).split(":")[0].split(" - ")[0].strip() or str(result["trackName"])
        candidates = [result.get("primaryGenreName"), *result.get("genres", [])]
        category = next((genres[g] for g in candidates if g in genres), "Other")
        found[str(result["bundleId"]).lower()] = AppInfo(name, category)
    return found
