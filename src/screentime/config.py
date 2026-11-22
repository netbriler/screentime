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

[lookup]               # name apps the Mac does not know (mostly iPhone apps) from Apple's App Store lookup
enabled = true         # only bundle ids go to itunes.apple.com; false keeps everything offline
country = "us"
```
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import tomllib


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
    devices: dict[str, str] = field(default_factory=dict)
    request_timeout: float = 30.0
    lookup_enabled: bool = True  # name unknown apps from Apple's public App Store lookup
    lookup_country: str = "us"  # App Store storefront to ask


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
        devices={str(key): str(value) for key, value in (raw.get("devices") or {}).items() if value},
        request_timeout=float(raw.get("request_timeout", 30.0)),
        lookup_enabled=bool((raw.get("lookup") or {}).get("enabled", True)),
        lookup_country=str((raw.get("lookup") or {}).get("country") or "us").lower(),
    )
