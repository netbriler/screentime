"""The two launchd agents, both every minute: the collector (Full Disk Access, copies the stores) and the tracker
(no access, sandboxed, reads the copies and sends them)."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from .config import config_path
from .collector import binary_path
from .sandbox import SANDBOX_EXEC, profile_path, write_profile

# SCREENTIME_AGENT_LABEL lets a test agent run next to the real one.
DEFAULT_LABEL = "com.netbriler.screentime"
LABEL = os.getenv("SCREENTIME_AGENT_LABEL") or DEFAULT_LABEL
INTERVAL_S = 60
LOG_MAX_BYTES = 5 * 1024 * 1024


COLLECTOR_LABEL = f"{LABEL}.collector"


def plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def log_dir() -> Path:
    # A test agent logs (and `--purge` deletes) apart from the real one.
    name = "screentime" if LABEL == DEFAULT_LABEL else LABEL
    return Path.home() / "Library" / "Logs" / name


def rotate_logs() -> None:
    """Move a log past ``LOG_MAX_BYTES`` to ``*.1``, keeping one old generation.

    launchd opened this run's log already, so this run still writes to the
    moved file; the next run starts a new one.
    """
    for path in log_dir().glob("*.log"):
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
        except OSError:
            pass  # a log we cannot move is no reason to skip the sync


def program() -> Path:
    """The installed ``screentime`` script of the running environment.

    For ``uv tool install`` / ``pipx`` this is the tool's own venv, which
    survives upgrades; unlike ``uvx``'s temporary cache environments.
    """
    script = Path(sys.executable).parent / "screentime"
    return script if script.exists() else Path(shutil.which("screentime") or script)


def collector_plist() -> dict[str, object]:
    return {
        "Label": COLLECTOR_LABEL,
        # The binary itself, not through Python or a shell: macOS checks Full Disk Access on this process.
        "ProgramArguments": [str(binary_path())],
        "StartInterval": INTERVAL_S,
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir() / "collector.log"),
        "StandardErrorPath": str(log_dir() / "collector.err.log"),
    }


def plist(executable: Path) -> dict[str, object]:
    return {
        "Label": LABEL,
        # Always inside the sandbox (sandbox.py): Full Disk Access reaches only the Screen Time stores.
        "ProgramArguments": [SANDBOX_EXEC, "-f", str(profile_path()), str(executable), "run"],
        # The label tells `run` which log directory to rotate.
        "EnvironmentVariables": {
            "SCREENTIME_CONFIG": str(config_path()),
            "SCREENTIME_AGENT_LABEL": LABEL,
        },
        "StartInterval": INTERVAL_S,
        "RunAtLoad": True,
        # One JSON line per run; `run` rotates them (rotate_logs).
        "StandardOutPath": str(log_dir() / "screentime.log"),
        "StandardErrorPath": str(log_dir() / "screentime.err.log"),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def loaded(label: str = LABEL) -> bool:
    return _launchctl("print", f"{_domain()}/{label}").returncode == 0


def _load(label: str, content: dict[str, object]) -> Path:
    path = plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(content))
    if loaded(label):
        _launchctl("bootout", f"{_domain()}/{label}")
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode:
        raise RuntimeError(f"launchctl bootstrap failed for {label}: {result.stderr.strip()}")
    return path


def install(executable: Path) -> list[Path]:
    """Write both plists and (re)load the agents; each runs once right away."""
    log_dir().mkdir(parents=True, exist_ok=True)
    write_profile(log_dir())
    return [_load(COLLECTOR_LABEL, collector_plist()), _load(LABEL, plist(executable))]


def uninstall() -> bool:
    """Unload and delete both agents; False if there was nothing to remove."""
    removed = False
    for label in (LABEL, COLLECTOR_LABEL):
        if loaded(label):
            _launchctl("bootout", f"{_domain()}/{label}")
            removed = True
        path = plist_path(label)
        if path.exists():
            path.unlink()
            removed = True
    return removed
