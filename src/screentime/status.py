"""``screentime status``: whether everything works, on one screen, without touching the network."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from . import collector, launchd
from .config import Settings, config_path
from .state import load_state, state_path
from .sync import enabled_sources

# Colours only on a terminal, plain text when piped or saved to a file.
_COLOURS = ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
GREEN, RED, YELLOW, DIM, BOLD, RESET = _COLOURS if sys.stdout.isatty() else ("",) * 6
STALE_AFTER = 5 * 60  # both agents run every minute; older than this means something stopped


def _agent(label: str) -> dict[str, str] | None:
    result = subprocess.run(
        ["launchctl", "print", f"gui/{_uid()}/{label}"], capture_output=True, text=True, check=False
    )
    if result.returncode:
        return None
    info = {}
    for key in ("state", "runs", "last exit code"):
        match = re.search(rf"^\s*{key} = (.+)$", result.stdout, re.MULTILINE)
        if match:
            info[key] = match.group(1).strip()
    return info


def _uid() -> int:
    return os.getuid()


def _last_line(path: Path) -> tuple[dict[str, Any] | None, float | None]:
    """The last JSON line of a log and when the log was written, or (None, None)."""
    try:
        lines = [line for line in path.read_text("utf-8", errors="replace").splitlines() if line.strip()]
        mtime = path.stat().st_mtime
    except OSError:
        return None, None
    for line in reversed(lines):
        try:
            return json.loads(line), mtime
        except ValueError:
            continue
    return None, mtime


def _last_error(path: Path) -> str:
    try:
        lines = [line for line in path.read_text("utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""


def _timestamp(value: str | None) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def _ago(timestamp: float | None) -> str:
    if timestamp is None:
        return "never"
    seconds = int(datetime.now().timestamp() - timestamp)
    if seconds < 90:
        return f"{seconds} s ago"
    if seconds < 90 * 60:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


def _mark(ok: bool | None) -> str:
    return f"{GREEN}●{RESET}" if ok else f"{YELLOW}●{RESET}" if ok is None else f"{RED}●{RESET}"


def show(settings: Settings) -> int:
    problems = 0
    print(f"{BOLD}Screen Time tracker{RESET}")

    # Agents
    print(
        f"\n{BOLD}Agents{RESET}  {DIM}(collector and tracker every {launchd.INTERVAL_S} s, console every "
        f"{launchd.CONSOLE_INTERVAL_S} s){RESET}"
    )
    for label, title in (
        (launchd.COLLECTOR_LABEL, "collector"),
        (launchd.LABEL, "tracker"),
        (launchd.CONSOLE_LABEL, "console"),
    ):
        info = _agent(label)
        if info is None:
            problems += 1
            print(f"  {_mark(False)} {title:10} not loaded — allow it in System Settings → General → Login Items &")
            print("               Extensions → Allow in the Background, then run `screentime install`")
            continue
        exit_code = info.get("last exit code", "?")
        ok = exit_code in ("0", "(never exited)")
        problems += not ok
        print(f"  {_mark(ok)} {title:10} loaded, runs {info.get('runs', '?')}, last exit {exit_code}")

    # Collector
    log = launchd.log_dir()
    run, written = _last_line(log / "collector.log")
    print(f"\n{BOLD}Collector{RESET}  {DIM}(Full Disk Access, {collector.binary_path()}){RESET}")
    if run is None:
        problems += 1
        print(f"  {_mark(False)} has not run yet")
    else:
        stale = written is not None and datetime.now().timestamp() - written > STALE_AFTER
        denied = "authorization denied" in _last_error(log / "collector.err.log") and run.get("failed")
        ok = not run.get("failed") and not stale
        problems += not ok
        print(
            f"  {_mark(ok)} last run {_ago(written)}: copied {run.get('copied', 0)} files, {run.get('rows', 0)} rows, "
            f"wrote {int(run.get('bytes_written', 0)) / 1024:.1f} KB, failed {run.get('failed', 0)}"
        )
        if denied:
            print(f"    {RED}No Full Disk Access.{RESET} Give it to the collector (and nothing else):")
            print(f"    {collector.binary_path()}")
        elif run.get("failed"):
            print(f"    {_last_error(log / 'collector.err.log')}")

    # Tracker
    run, written = _last_line(log / "screentime.log")
    state = load_state()
    print(f"\n{BOLD}Tracker{RESET}  {DIM}(no access, sandboxed){RESET}")
    if run is None:
        problems += 1
        print(f"  {_mark(False)} has not run yet")
    else:
        stale = written is not None and datetime.now().timestamp() - written > STALE_AFTER
        # Server errors are shown with their server below; here only the tracker's own.
        servers = tuple(f"{server.name}: " for server in settings.servers)
        errors = [error for error in run.get("errors", []) if not error.startswith(servers)]
        ok = not errors and not stale
        problems += not ok
        sent = sum(run.get("sent", {}).values())
        print(f"  {_mark(ok)} last run {_ago(written)}: read {run.get('read', 0)} new events, sent {sent}")
        for error in errors:
            print(f"    {RED}{error}{RESET}")

    # Servers
    print(f"\n{BOLD}Servers{RESET}  {DIM}(`screentime test` checks the tokens){RESET}")
    if not settings.servers:
        problems += 1
        print(f"  {_mark(False)} none configured in {config_path()}")
    for server in settings.servers:
        queued = len(state["outbox"].get(server.name, []))
        if not server.enabled:
            print(f"  {_mark(None)} {server.name:12} disabled")
            continue
        health = state.get("server_health", {}).get(server.name, {})
        error = health.get("error")
        problems += bool(error)
        print(f"  {_mark(not error)} {server.name:12} {queued} queued  {DIM}{server.url.split('?')[0][:60]}{RESET}")
        if error:
            print(f"    {RED}{error}{RESET} {DIM}(since {_ago(_timestamp(health.get('error_since')))}){RESET}")
            if health.get("ok_at"):
                print(f"    {DIM}last reached {_ago(_timestamp(health['ok_at']))}{RESET}")
        elif health.get("ok_at"):
            print(f"    {DIM}reached {_ago(_timestamp(health['ok_at']))}{RESET}")

    # Devices
    print(f"\n{BOLD}Devices{RESET}")
    for source in enabled_sources(settings):
        try:
            devices = source.devices()
        except Exception as exc:  # a status screen shows problems instead of failing on them
            print(f"  {_mark(False)} {source.name}: {exc}")
            continue
        for device in devices:
            name = settings.devices.get(device.id) or device.name
            print(f"  {_mark(True)} {name:28} {device.platform:7} {DIM}{device.id}{RESET}")
        if not devices:
            print(f"  {_mark(None)} {source.name}: no devices yet")

    print(f"\n{DIM}Config {config_path()}\nState  {state_path()}\nLogs   {log}{RESET}")
    print(f"\n{_mark(not problems)} {'Everything works.' if not problems else f'{problems} problem(s) above.'}")
    return 0 if not problems else 1
