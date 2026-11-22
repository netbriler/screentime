"""``screentime`` command line: status, test, run, resend, install, uninstall."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import subprocess
import sys

from . import __version__, collector, launchd, sandbox, status
from .config import config_path, load_settings
from .locking import LockBusyError
from .sender import SendError, post
from .config import data_dir
from .locking import process_lock
from .state import load_state, save_state
from .sync import run, start_replay

FDA_HINT = (
    "Give Full Disk Access (System Settings → Privacy & Security → Full Disk Access) to the collector, and to "
    "nothing else:\n  {binary}"
)


def _run(args: argparse.Namespace) -> int:
    launchd.rotate_logs()
    settings = load_settings()
    if not settings.servers:
        print(f"No servers configured in {config_path()}", file=sys.stderr)
        return 2
    try:
        result = run(settings)
    except LockBusyError:
        return 0  # the previous run is still sending; this one has nothing to add
    # One JSON line per run: the agent's log.
    print(json.dumps(asdict(result), ensure_ascii=False))
    return 1 if result.errors and not result.sent else 0


def _test(args: argparse.Namespace) -> int:
    settings = load_settings()
    ok = _test_collector()
    for server in settings.servers:
        if not server.enabled:
            print(f"{server.name}: disabled")
            continue
        try:
            post(server, {"devices": [], "events": []}, settings.request_timeout)
            print(f"{server.name}: token accepted")
        except SendError as exc:
            ok = False
            print(exc)
    if not settings.servers:
        ok = False
        print(f"No servers configured in {config_path()}")
    ok = _test_sandbox() and ok
    return 0 if ok else 1


def _test_collector() -> bool:
    age = collector.snapshot_age()
    if age is None:
        print("Collector: no copy yet. " + FDA_HINT.format(binary=collector.binary_path()))
        print(f"  Its log: {launchd.log_dir() / 'collector.err.log'}")
        return False
    print(f"Collector: last copy {age:.0f} s ago")
    if age > 15 * 60:
        print(f"  That is old; check {launchd.log_dir() / 'collector.err.log'}")
    return True


def _test_sandbox() -> bool:
    """Run the sandbox self-check the way the agent runs, inside the installed profile."""
    profile = sandbox.profile_path()
    if not profile.exists():
        print("Sandbox: not set up yet (`screentime install` writes it)")
        return True
    result = subprocess.run(
        [sandbox.SANDBOX_EXEC, "-f", str(profile), str(launchd.program()), "sandbox-check"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(result.stdout.strip() or result.stderr.strip() or f"Sandbox: check exited with {result.returncode}")
    return result.returncode == 0


def _sandbox_check(args: argparse.Namespace) -> int:
    problems = sandbox.check()
    if problems:
        print("Sandbox: NOT holding — " + "; ".join(problems))
        return 1
    print("Sandbox: holds (home folder unreadable, no writes outside the tracker's own folders)")
    return 0


def _status(args: argparse.Namespace) -> int:
    return status.show(load_settings())


def _resend(args: argparse.Namespace) -> int:
    """Read all history again and queue it for one server (or all), e.g. after its database was reset."""
    settings = load_settings()
    names = [server.name for server in settings.servers]
    if args.server and args.server not in names:
        print(f"No server named {args.server!r}; configured: {', '.join(names) or 'none'}", file=sys.stderr)
        return 2
    with process_lock(data_dir() / "screentime.lock"):
        state = load_state()
        for name in [args.server] if args.server else names:
            start_replay(state, server=name)
        save_state(state)
    target = args.server or "every server"
    print(f"History will be read again and sent to {target} within two minutes (macOS keeps about four weeks).")
    return 0


def _install(args: argparse.Namespace) -> int:
    binary = collector.build()
    for path in launchd.install(launchd.program()):
        print(f"Agent installed: {path}")
    print(f"Both run every {launchd.INTERVAL_S} s. The tracker needs no access and runs in the sandbox.")
    print(FDA_HINT.format(binary=binary))
    return 0


def _uninstall(args: argparse.Namespace) -> int:
    print("Agent removed." if launchd.uninstall() else "No agent was installed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="screentime", description="Send Screen Time usage to your servers.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("status", _status, "show whether everything works"),
        ("run", _run, "read new usage and send it (what the agent runs)"),
        ("test", _test, "check Full Disk Access and the servers"),
        ("install", _install, "install the background agent"),
        ("uninstall", _uninstall, "remove the background agent"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(handler=handler)
    resend = commands.add_parser("resend", help="send all history again, e.g. after a server's database was reset")
    resend.add_argument("--server", help="only this server (its name in the config); default: all")
    resend.set_defaults(handler=_resend)
    # Run by `screentime test` inside the sandbox; not meant to be called by hand.
    commands.add_parser("sandbox-check").set_defaults(handler=_sandbox_check)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
