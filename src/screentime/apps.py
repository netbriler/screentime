"""Readable app names for bundle ids, found offline only: the shipped list, then this Mac's installed apps.

Nothing is looked up on the internet. Apps found neither way (mostly iPhone-only apps) go to the server with an
empty name; the server keeps the bundle id and can name them later.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys


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
