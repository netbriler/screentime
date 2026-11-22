"""The macOS sandbox the background agent runs in.

The agent needs Full Disk Access to read Screen Time, and macOS grants it to the whole Python binary. The sandbox
takes back everything else, enforced by the kernel rather than by this code:

* nothing in the home folder can be read except the Python that runs the tracker, its config, its own state and
  logs, and the two Screen Time stores;
* nothing can be written except its own state, its logs and the temporary folder;
* the network stays open, to reach the servers.

The profile lives in ``~/.config/screentime``, which the sandboxed agent may read but never write, so it cannot loosen
its own rules.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from .config import config_path, data_dir

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


def profile_path() -> Path:
    return config_path().parent / "sandbox.sb"


def _quote(path: Path | str) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(log_dir: Path) -> str:
    # Imported here: sync imports the sources, which this module does not otherwise need.
    from .sync import BIOME_STREAM, KNOWLEDGEC_DB, LIBRARY

    home = Path.home()
    python_dirs = {
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(os.path.realpath(sys.executable)).parent.parent,
        # The package itself: inside the tool's environment, or the source folder of an editable install.
        Path(__file__).resolve().parent.parent,
    }
    readable = [
        *sorted(python_dirs),
        config_path().parent,
        data_dir(),
        log_dir,
        BIOME_STREAM,
        LIBRARY / "Biome" / "sync",  # names of the synced iPhones and iPads
        KNOWLEDGEC_DB.parent,
    ]
    writable = [data_dir(), log_dir]
    lines = [
        "(version 1)",
        "(allow default)",
        "",
        ";; No writing anywhere but the tracker's own state, its logs and the temporary folders.",
        "(deny file-write*)",
        "(allow file-write*",
        *(f"  (subpath {_quote(path)})" for path in writable),
        '  (subpath "/private/var/folders")',
        '  (subpath "/private/tmp")',
        '  (literal "/dev/null"))',
        "",
        ";; Nothing in the home folder can be read but what the tracker needs.",
        f"(deny file-read* (subpath {_quote(home)}))",
        "(allow file-read*",
        *(f"  (subpath {_quote(path)})" for path in readable),
        ")",
        ";; Names and sizes only (no contents), so paths under the home folder still resolve.",
        "(allow file-read-metadata)",
        "",
    ]
    return "\n".join(lines)


def write_profile(log_dir: Path) -> Path:
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile(log_dir), "utf-8")
    path.chmod(0o644)
    return path


def check() -> list[str]:
    """Run inside the sandbox: what it lets through that it should not. Empty means it holds."""
    problems = []
    home = Path.home()
    # The home folder itself and ~/.ssh are not guarded by macOS privacy controls, so only the sandbox stops these.
    for probe in (home, home / ".ssh", home / "Library" / "Mail", home / "Library" / "Messages", home / "Documents"):
        try:
            if probe.is_dir():
                next(probe.iterdir(), None)
            elif probe.exists():
                probe.read_bytes()
            else:
                continue
            problems.append(f"could read {probe}")
        except PermissionError:
            pass
        except OSError:
            pass
    target = home / ".screentime-sandbox-probe"
    try:
        target.write_text("probe")
        target.unlink()
        problems.append(f"could write {target}")
    except PermissionError:
        pass
    return problems
