# Screen Time tracker

Reads Apple Screen Time on your Mac and sends it to your own server — the
[Second Brain](https://github.com/netbriler/second-brain) one — which stores it and answers questions about it.
A trimmed fork of [nichtlegacy/screentime](https://github.com/nichtlegacy/screentime) that only reads activity and
sends it: no local database, reports, Grafana, InfluxDB, Home Assistant, MCP, App Store lookups or setup wizard. The
server stores and analyses everything.

What it reads, read-only:

- **This Mac** — `~/Library/Application Support/Knowledge/knowledgeC.db` (`/app/usage`), copied to a temporary file
  first so Apple's database is never touched.
- **iPhone and iPad** — usage synced to this Mac through iCloud when *Share across devices* is on in Screen Time
  (`~/Library/Biome/streams/restricted/App.InFocus`), opened with `mode=ro&immutable=1`.

Each event is one stretch of one app in front: device, bundle id, app name, start and end. The name comes from a
shipped list or the app installed on this Mac (Spotlight); nothing is looked up online. Nothing else is read or sent,
and nothing goes anywhere but your servers.

## Install

```bash
uv tool install "git+https://github.com/netbriler/screentime"
mkdir -p ~/.config/screentime && touch ~/.config/screentime/config.toml && chmod 600 ~/.config/screentime/config.toml
# put the config below in it, with your server URL and token
screentime test                 # checks Full Disk Access and every server
screentime install              # background agent, runs every minute
```

macOS protects these stores, so the agent needs **Full Disk Access**: *System Settings → Privacy & Security → Full
Disk Access*, add the Python binary `screentime install` prints. Your terminal needs it too while you run
`screentime test` by hand; you can remove it afterwards. The grant is tied to that Python path, so it has to be
given again after uv upgrades Python (`screentime test` tells you).

### The sandbox

Full Disk Access goes to the whole Python binary, so the agent always runs inside the macOS sandbox
(`/usr/bin/sandbox-exec`, profile in `~/.config/screentime/sandbox.sb`, written by `screentime install`). The kernel
enforces it, whatever the code does:

- nothing in your home folder can be read except the Python that runs the tracker, its config, its own state and
  logs, and the Screen Time stores (`~/Library/Biome/streams/restricted/App.InFocus`, `~/Library/Biome/sync`,
  `~/Library/Application Support/Knowledge`);
- nothing can be written except its state, its logs and the temporary folder;
- the network stays open, to reach your servers.

The profile sits where the agent may read but not write, so it cannot loosen its own rules. `screentime test` runs a
self-check inside it: it tries to read your home folder and `~/.ssh` and to write to your home folder, and reports
whether the sandbox stopped it.

The same Python binary is shared by everything uv runs with that Python version; outside the sandbox those get Full
Disk Access too. To keep the grant narrow, install the tracker on a Python version you use for nothing else, for
example `uv tool install --python 3.11 …`.

## Config

```toml
[[servers]]
name = "Production"
url = "https://example.com/screentime-tracker/<path>/"   # SCREENTIME_TRACKER_PATH on the server
token = "<token>"                                       # SCREENTIME_TRACKER_TOKEN

[[servers]]                     # optional: more servers, e.g. a local one for development
name = "Local"
url = "http://localhost:8000/screentime-tracker/<path>/"
token = "<token>"

[sources]
biome = true                    # iPhone/iPad
knowledgec = true               # this Mac

[devices]                       # optional names by device id
# "<device id>" = "My iPhone"
```

## How it sends

Every run reads only what is new since the last one, queues it for every server and posts it:

```http
POST <url>
Authorization: Bearer <token>

{"devices": [{"id": "…", "name": "My iPhone", "platform": "iphone", "model": "iPhone17,1", "source": "biome"}],
 "events": [{"device_id": "…", "bundle_id": "com.google.ios.youtube", "app_name": "YouTube",
             "start": "2026-11-22T09:00:00+00:00", "end": "2026-11-22T09:12:30+00:00"}]}
```

Nothing is sent when nothing is new. Each server has its own queue in
`~/Library/Application Support/screentime/state.json`: one that is down keeps its events and gets them on a later
run, without holding up the others. Events are queued before the read position moves, so nothing is lost; an event
sent twice only updates itself on the server.

## Commands

| Command | |
|---|---|
| `screentime test` | check Full Disk Access and every server |
| `screentime run` | read and send now (what the agent runs) |
| `screentime install` / `uninstall` | the background agent (`~/Library/LaunchAgents/com.netbriler.screentime.plist`) |

Logs: `~/Library/Logs/screentime/` (one JSON line per run). The queue and the read positions are in
`~/Library/Application Support/screentime/state.json`.
