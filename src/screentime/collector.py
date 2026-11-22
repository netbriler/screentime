"""The collector: a tiny C program (collector/screentime-collector.c) that alone gets Full Disk Access.

It runs as its own launchd agent. It copies the small Biome files (iPhone/iPad) into ``snapshot/Library`` and runs one
fixed query on knowledgeC (this Mac), writing the new rows to ``snapshot/knowledgec``; the Python tracker reads only
those. It has to be its own agent: macOS checks Full Disk Access on the process launchd started, so if Python
started the collector, Python would be the one asked for access.
"""

from __future__ import annotations

from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
import subprocess

from .config import data_dir


def binary_path() -> Path:
    return data_dir() / "bin" / "screentime-collector"


def snapshot_library() -> Path:
    """The copy of ~/Library the collector keeps: only the Screen Time stores."""
    return data_dir() / "snapshot" / "Library"


def build() -> Path:
    """Compile the collector with the Command Line Tools' clang; the linker signs it ad hoc."""
    target = binary_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with as_file(files("screentime").joinpath("collector/screentime-collector.c")) as source:
        result = subprocess.run(
            ["xcrun", "clang", "-O2", "-Wall", "-o", str(target), str(source), "-lsqlite3"],
            capture_output=True,
            text=True,
        )
    if result.returncode:
        raise RuntimeError(
            "Could not build the collector (install the Command Line Tools: `xcode-select --install`):\n"
            + result.stderr.strip()
        )
    target.chmod(0o755)
    return target


def snapshot_age() -> float | None:
    """Seconds since the collector last wrote anything, or None if it never has."""
    times = [path.stat().st_mtime for path in snapshot_library().parent.rglob("*") if path.is_file()]
    if not times:
        return None
    return datetime.now(UTC).timestamp() - max(times)
