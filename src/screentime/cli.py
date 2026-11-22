"""``screentime`` command line: run, test, install, uninstall."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import logging
import sys

from . import __version__, launchd
from .config import config_path, load_settings
from .locking import LockBusyError
from .sender import SendError, post
from .sync import denied_paths, run

FDA_HINT = (
    "Give Full Disk Access (System Settings → Privacy & Security → Full Disk Access) to this file, "
    "which the background agent runs as:\n  {binary}"
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
    ok = True
    for path in denied_paths(settings):
        ok = False
        print(f"No Full Disk Access: cannot read {path}")
    if not ok:
        print(FDA_HINT.format(binary=launchd.fda_binary()))
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
    return 0 if ok else 1


def _install(args: argparse.Namespace) -> int:
    path = launchd.install(launchd.program())
    print(f"Agent installed: {path}; it runs every {launchd.INTERVAL_S} s.")
    print(FDA_HINT.format(binary=launchd.fda_binary()))
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
        ("run", _run, "read new usage and send it (what the agent runs)"),
        ("test", _test, "check Full Disk Access and the servers"),
        ("install", _install, "install the background agent"),
        ("uninstall", _uninstall, "remove the background agent"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(handler=handler)
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
