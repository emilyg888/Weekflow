"""Discord webhook integration (spec §5).

Configuration: set one env var per channel with the webhook URL.
    WEEKFLOW_DISCORD_BACKLOG_BUCKET
    WEEKFLOW_DISCORD_DAILY_PULSE
    WEEKFLOW_DISCORD_REMINDERS
    WEEKFLOW_DISCORD_DONE_LOG

Missing env vars are treated as "channel not configured" — the post is a
no-op and logged to stderr rather than raising. This keeps the board usable
without Discord wired up.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Optional

CHANNELS = {
    "backlog-bucket": "WEEKFLOW_DISCORD_BACKLOG_BUCKET",
    "daily-pulse":    "WEEKFLOW_DISCORD_DAILY_PULSE",
    "reminders":      "WEEKFLOW_DISCORD_REMINDERS",
    "done-log":       "WEEKFLOW_DISCORD_DONE_LOG",
}


def _webhook_url(channel: str) -> Optional[str]:
    env = CHANNELS.get(channel)
    if env is None:
        raise ValueError(f"Unknown Discord channel: {channel}")
    return os.environ.get(env)


def post(channel: str, message: str) -> bool:
    """Post `message` to `channel`. Returns True on success, False if unconfigured or failed."""
    url = _webhook_url(channel)
    if not url:
        print(f"[discord] channel '{channel}' not configured; skipping", file=sys.stderr)
        return False
    payload = json.dumps({"content": message}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except urllib.error.URLError as e:
        print(f"[discord] post to '{channel}' failed: {e}", file=sys.stderr)
        return False
