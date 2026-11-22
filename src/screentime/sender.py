"""Posts events to a server: ``POST <url>`` with ``Authorization: Bearer <token>`` and JSON

```json
{
  "devices": [{"id": "…", "name": "My iPhone", "platform": "iphone", "model": "iPhone17,1", "source": "biome"}],
  "events": [{"device_id": "…", "bundle_id": "com.google.ios.youtube", "app_name": "YouTube", "app_category": "Media",
              "start": "2026-11-22T09:00:00+00:00", "end": "2026-11-22T09:12:30+00:00"}]
}
```

An event is identified by device, bundle id and start, so sending one twice only updates it.
"""

from __future__ import annotations

from typing import Any

import requests

from .config import Server

BATCH_SIZE = 2000


class SendError(RuntimeError):
    pass


def post(server: Server, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        response = requests.post(
            server.url,
            json=body,
            headers={"Authorization": f"Bearer {server.token}"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SendError(f"{server.name}: cannot reach the server ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        raise SendError(f"{server.name}: HTTP {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError as exc:
        raise SendError(f"{server.name}: the reply is not JSON") from exc


def send_outbox(
    server: Server,
    outbox: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    timeout: float,
    *,
    check: bool = False,
) -> tuple[int, dict[str, str] | None]:
    """Send the outbox in batches; sent events are removed from it. Raises SendError and keeps the rest on failure.

    Returns how many events were sent and the server's ``latest`` (newest stored event per device) from its last
    reply. With ``check`` and nothing to send, an empty request still asks for ``latest``.
    """
    sent, latest = 0, None
    while outbox:
        batch = outbox[:BATCH_SIZE]
        reply = post(server, {"devices": devices, "events": batch}, timeout)
        latest = reply.get("latest", latest)
        del outbox[: len(batch)]
        sent += len(batch)
    if check and not sent:
        latest = post(server, {"devices": devices, "events": []}, timeout).get("latest")
    return sent, latest
