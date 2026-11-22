# Screen Time tracker

Reads Apple Screen Time on your Mac — the Mac's own app usage and the iPhone/iPad usage synced to it — and sends it
to your own server, the [Second Brain](https://github.com/netbriler/second-brain) one, which stores and analyses it.

A trimmed fork of [nichtlegacy/screentime](https://github.com/nichtlegacy/screentime) that only collects activity and
sends it: no local database, reports, Grafana, InfluxDB, Home Assistant, MCP or setup wizard. Unlike the original, the
Python code never gets access to your files.

## How it works

Screen Time lives in two stores that macOS protects: only a program with **Full Disk Access** can read them. That
access is granted per program, and granting it to Python would let any script run with that Python read your mail,
messages and documents. So the work is split:

```
┌──────────────────────────────┐   Full Disk Access        ┌───────────────────────────────┐  no access, sandboxed
│ screentime-collector (C)     │ ────────────────────────▶ │ screentime run (Python)       │ ───────────────────▶ your servers
│ ~300 lines, no network,      │  writes only to its own   │ parses, names apps, queues,   │  HTTPS, Bearer token
│ fixed paths and one query    │  folder                   │ sends                         │
└──────────────────────────────┘                           └───────────────────────────────┘
```

**The collector** (`src/screentime/collector/screentime-collector.c`) is the only part with Full Disk Access. It runs
as its own launchd agent every minute and does exactly two things, with paths and queries fixed in the code:

- **iPhone/iPad** — copies the small, append-only segment files of
  `~/Library/Biome/streams/restricted/App.InFocus/remote` and `~/Library/Biome/sync/sync.db`. Only new or changed
  files are copied; copies of deleted segments are removed.
- **This Mac** — runs one read-only query on `~/Library/Application Support/Knowledge/knowledgeC.db` (stream
  `/app/usage`, this Mac's rows only) and writes the new rows to small `new-<time>.jsonl` files, plus the synced
  devices' hardware models to `peers.json`. The database itself is never copied.

Everything goes to `~/Library/Application Support/screentime/snapshot`, readable by you only. The collector takes no
arguments, finds your home folder from the account database (not `$HOME`) and never opens a network connection, so
it cannot be pointed at other files or send anything anywhere. A run writes kilobytes, not copies of databases; its
log shows `bytes_written` for each run. macOS ties Full Disk Access to the binary's code signature: if the file is
replaced, the grant stops applying.

**The tracker** (`screentime run`, Python) has no access at all. Every minute it reads the collector's output, turns
it into events, names the apps, drops the ones it already sent, queues them for every server and sends them. It runs
inside the macOS sandbox (`/usr/bin/sandbox-exec`, profile in `~/.config/screentime/sandbox.sb`): the kernel lets it
read nothing in your home folder but its Python, its config and its own folder, and write nothing but its own folder,
its logs and temporary files. The real Screen Time stores are out of its reach even by mistake.

Each event is one stretch of one app in front: device, bundle id, app name, start and end. App names come from a
shipped list, the app installed on this Mac (Spotlight), or else Apple's public App Store lookup
(`itunes.apple.com/lookup`, on by default): only bundle ids go to Apple, each app is asked about once, and
`[lookup] enabled = false` turns it off. Usage goes nowhere but your servers.

## Install

Needs macOS and the Command Line Tools (`xcode-select --install`), which build the collector on your Mac.

```bash
uv tool install "git+https://github.com/netbriler/screentime"
mkdir -p ~/.config/screentime && touch ~/.config/screentime/config.toml && chmod 600 ~/.config/screentime/config.toml
# put the config below in it, with your server URL and token
screentime install              # builds the collector, installs both agents
```

Then give **Full Disk Access to the collector only**: *System Settings → Privacy & Security → Full Disk Access → +*,
and pick the file `screentime install` prints:

```
~/Library/Application Support/screentime/bin/screentime-collector
```

(in the file dialog, Cmd+Shift+G opens a path). Python needs nothing. Check everything with:

```bash
screentime test
```

It reports when the collector last wrote, whether every server accepts the token, and runs a self-check inside the
sandbox that tries to read your home folder and `~/.ssh` and to write to your home folder.

After `screentime install` again (an update rebuilds the collector), macOS sees a new binary: give it Full Disk Access
again.

## Config

`~/.config/screentime/config.toml`:

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

[lookup]                        # App Store names for apps the Mac does not know (mostly iPhone apps)
enabled = true                  # false: no requests to Apple at all
country = "us"
```

## What it sends

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
| `screentime install` | build the collector, install both agents and the sandbox profile |
| `screentime test` | collector freshness, every server, sandbox self-check |
| `screentime run` | read and send now (what the tracker agent runs) |
| `screentime uninstall` | remove both agents |

| Where | |
|---|---|
| `~/Library/LaunchAgents/com.netbriler.screentime.collector.plist` | collector agent |
| `~/Library/LaunchAgents/com.netbriler.screentime.plist` | tracker agent (through `sandbox-exec`) |
| `~/Library/Application Support/screentime/` | collector binary, its output, the tracker's queue |
| `~/Library/Logs/screentime/` | one JSON line per run from each agent |
