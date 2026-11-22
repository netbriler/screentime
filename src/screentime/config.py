"""Settings from ``~/.config/screentime/config.toml``.

```toml
[[servers]]
name = "Production"
url = "https://example.com/screentime-tracker/<path>/"
token = "<SCREENTIME_TRACKER_TOKEN>"

[[servers]]            # more servers are optional, e.g. a local one for development
name = "Local"
url = "http://localhost:8000/screentime-tracker/<path>/"
token = "<token>"
enabled = true

[sources]
biome = true           # iPhone/iPad usage synced to this Mac through iCloud
knowledgec = true      # this Mac's own usage

[devices]              # optional display names by device id
# "00000000-0000-0000-0000-000000000000" = "My iPhone"
# A table adds the platform and marks a work account (not personal use); a Biome stream that is not among the synced
# devices, such as another user account on this Mac, is read only when it is listed like this:
# "11111111-1111-1111-1111-111111111111" = { name = "Mac (work account)", platform = "mac", work = true }

[lookup]               # name apps the Mac does not know (mostly iPhone apps) from Apple's App Store lookup
enabled = true         # only bundle ids go to itunes.apple.com; false keeps everything offline
countries = ["us", "id", "pl", "th", "ge", "ua"]  # storefronts in order; the next only for apps not found yet
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import tomllib


DEFAULT_COUNTRIES = ("us", "id", "pl", "th", "ge", "ua")


@dataclass(frozen=True)
class Server:
    name: str
    url: str
    token: str
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    servers: list[Server] = field(default_factory=list)
    sources: dict[str, bool] = field(default_factory=dict)
    devices: dict[str, str] = field(default_factory=dict)  # device id -> display name
    # Devices described in [devices] as a table: platform, and work = true for a work account (not personal use).
    # A Biome stream not among the synced devices (e.g. another user account on this Mac) is read only when listed.
    device_details: dict[str, dict] = field(default_factory=dict)
    request_timeout: float = 30.0
    lookup_enabled: bool = True  # name unknown apps from Apple's public App Store lookup
    # App Store storefronts to ask, in order: the next one only for apps the previous ones did not have.
    lookup_countries: tuple[str, ...] = DEFAULT_COUNTRIES


def config_path() -> Path:
    return Path(os.getenv("SCREENTIME_CONFIG", "~/.config/screentime/config.toml")).expanduser()


def data_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "screentime"
    return Path(os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "screentime"


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    if not path.exists():
        return Settings()
    raw = tomllib.loads(path.read_text("utf-8"))
    servers = [
        Server(
            name=str(item.get("name") or f"Server {index + 1}"),
            url=str(item["url"]).strip(),
            token=str(item["token"]).strip(),
            enabled=bool(item.get("enabled", True)),
        )
        for index, item in enumerate(raw.get("servers") or [])
        if item.get("url") and item.get("token")
    ]
    return Settings(
        servers=servers,
        sources={str(key): bool(value) for key, value in (raw.get("sources") or {}).items()},
        devices=_device_names(raw.get("devices") or {}),
        device_details=_device_details(raw.get("devices") or {}),
        request_timeout=float(raw.get("request_timeout", 30.0)),
        lookup_enabled=bool((raw.get("lookup") or {}).get("enabled", True)),
        lookup_countries=_countries(raw.get("lookup") or {}),
    )


def _device_names(devices: dict) -> dict[str, str]:
    """``"<id>" = "Name"`` or ``"<id>" = { name = "Name", … }``."""
    names = {}
    for key, value in devices.items():
        name = value.get("name") if isinstance(value, dict) else value
        if name:
            names[str(key)] = str(name)
    return names


def _device_details(devices: dict) -> dict[str, dict]:
    return {
        str(key): {"platform": str(value.get("platform") or "mac"), "work": bool(value.get("work", False))}
        for key, value in devices.items()
        if isinstance(value, dict)
    }


def _countries(lookup: dict) -> tuple[str, ...]:
    countries = lookup.get("countries")
    if countries is None and lookup.get("country"):  # the older single-country setting
        countries = [lookup["country"]]
    cleaned = tuple(dict.fromkeys(str(c).strip().lower() for c in (countries or DEFAULT_COUNTRIES) if str(c).strip()))
    return cleaned or DEFAULT_COUNTRIES
