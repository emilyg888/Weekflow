"""Discord webhook integration (spec §5).

Configuration: set one env var per channel with the webhook URL.
    WEEKFLOW_DISCORD_BACKLOG_BUCKET
    WEEKFLOW_DISCORD_DAILY_PULSE
    WEEKFLOW_DISCORD_REMINDERS
    WEEKFLOW_DISCORD_DONE_LOG
    WEEKFLOW_DISCORD_CARD_NOTIFY

Weekflow also reads local config from `/Users/emilygao/LocalDocuments/Projects/Weekflow/config.py`
via `API_KEYS["weekflow_discord_card_notify"]` for the card notification channel.

Missing env vars are treated as "channel not configured" — the post is a
no-op and logged to stderr rather than raising. This keeps the board usable
without Discord wired up.
"""
from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .models import COL_LABELS, LANE_LABELS, Card

CHANNELS = {
    "backlog-bucket": "WEEKFLOW_DISCORD_BACKLOG_BUCKET",
    "daily-pulse":    "WEEKFLOW_DISCORD_DAILY_PULSE",
    "reminders":      "WEEKFLOW_DISCORD_REMINDERS",
    "done-log":       "WEEKFLOW_DISCORD_DONE_LOG",
    "card-notify":    "WEEKFLOW_DISCORD_CARD_NOTIFY",
}

_MAX_DISCORD_MESSAGE = 1900
_MAX_EMBED_DESCRIPTION = 3800
_EMBED_COLOR = 0x7A8494
_WEEKFLOW_CONFIG = Path(__file__).resolve().parent.parent / "config.py"


def _load_weekflow_api_keys() -> dict:
    if not _WEEKFLOW_CONFIG.exists():
        return {}

    spec = importlib.util.spec_from_file_location("weekflow_config", _WEEKFLOW_CONFIG)
    if spec is None or spec.loader is None:
        return {}

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return {}

    keys = getattr(module, "API_KEYS", {})
    return keys if isinstance(keys, dict) else {}


def _webhook_url(channel: str) -> Optional[str]:
    env = CHANNELS.get(channel)
    if env is None:
        raise ValueError(f"Unknown Discord channel: {channel}")

    if env_value := os.environ.get(env):
        return env_value

    weekflow_keys = _load_weekflow_api_keys()
    if channel == "card-notify":
        for key_name in ("weekflow_discord_card_notify", "weekflow_card_notify_webhook"):
            value = weekflow_keys.get(key_name)
            if value and not str(value).startswith("YOUR_"):
                return str(value)

    return None


def _validate_webhook_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "Webhook URL must start with http:// or https://."
    if not parsed.netloc:
        return "Webhook URL is missing a hostname."
    return None


def _build_card_payloads(card: Card, full_content: str, source_name: Optional[str] = None) -> list[dict]:
    day_stamp = datetime.now().strftime("%A %d %b %Y")
    title = f"\U0001f4cb Weekflow — {day_stamp}"
    footer_base = f"Card {card.id[:8]}"
    if source_name:
        footer_base = f"{footer_base} · Source {source_name}"

    fields = [
        {"name": "Card", "value": card.title or "(untitled)", "inline": True},
        {"name": "Lane", "value": LANE_LABELS.get(card.lane, card.lane), "inline": True},
        {"name": "Column", "value": COL_LABELS.get(card.col, card.col), "inline": True},
        {"name": "Tag", "value": card.tag or "—", "inline": True},
        {"name": "Effort", "value": card.effort or "—", "inline": True},
    ]

    chunks = _chunk_text(full_content or "", limit=_MAX_EMBED_DESCRIPTION)
    if not chunks:
        chunks = ["(no content)"]

    payloads: list[dict] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        footer_text = footer_base if total == 1 else f"{footer_base} · Part {idx}/{total}"
        embed = {
            "title": title if idx == 1 else f"{title} (cont. {idx}/{total})",
            "color": _EMBED_COLOR,
            "description": chunk,
            "footer": {"text": footer_text},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if idx == 1:
            embed["fields"] = fields
        payloads.append({"embeds": [embed]})

    return payloads


def _send_with_requests(url: str, payload: dict) -> tuple[bool, Optional[str]]:
    try:
        import requests  # type: ignore
    except ImportError:
        return False, None

    try:
        resp = requests.post(
            url,
            json=payload,
            timeout=10,
            headers={"User-Agent": "Weekflow/1.0 Discord Webhook Client"},
        )
        resp.raise_for_status()
        return True, None
    except requests.HTTPError as e:  # type: ignore[attr-defined]
        body = e.response.text.strip() if e.response is not None else ""
        message = f"Discord webhook returned HTTP {e.response.status_code if e.response is not None else 'error'}."
        if body:
            message = f"{message} Response: {body}"
        print(f"[discord] post failed via requests: {message}", file=sys.stderr)
        return False, message
    except requests.RequestException as e:  # type: ignore[attr-defined]
        message = f"Discord request failed: {e}"
        print(f"[discord] post failed via requests: {message}", file=sys.stderr)
        return False, message


def _post_payload_result(channel: str, payload: dict) -> tuple[bool, Optional[str]]:
    url = _webhook_url(channel)
    if not url:
        message = f"Discord channel '{channel}' is not configured."
        print(f"[discord] {message}", file=sys.stderr)
        return False, message
    if validation_error := _validate_webhook_url(url):
        print(f"[discord] {validation_error}", file=sys.stderr)
        return False, validation_error

    ok, error = _send_with_requests(url, payload)
    if ok or error is not None:
        return ok, error

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Weekflow/1.0 Discord Webhook Client",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = 200 <= resp.status < 300
            if ok:
                return True, None
            return False, f"Discord webhook returned HTTP {resp.status}."
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        message = f"Discord webhook returned HTTP {e.code}."
        if body:
            message = f"{message} Response: {body}"
        print(f"[discord] post to '{channel}' failed: {message}", file=sys.stderr)
        return False, message
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        message = f"Discord request failed: {reason}"
        print(f"[discord] post to '{channel}' failed: {message}", file=sys.stderr)
        return False, message


def post(channel: str, message: str) -> bool:
    """Post `message` to `channel`. Returns True on success, False if unconfigured or failed."""
    ok, _ = _post_payload_result(channel, {"content": message})
    return ok


def post_result(channel: str, message: str) -> tuple[bool, Optional[str]]:
    """Post `message` to `channel`. Returns success plus an optional error reason."""
    return _post_payload_result(channel, {"content": message})


def _chunk_text(text: str, limit: int = _MAX_DISCORD_MESSAGE) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []

    chunks: list[str] = []
    remaining = stripped
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


def post_card(card: Card, full_content: str, source_name: Optional[str] = None) -> bool:
    """Send a card summary and its full content to the dedicated card webhook."""
    ok, _ = post_card_result(card, full_content, source_name)
    return ok


def post_card_result(card: Card, full_content: str, source_name: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """Send a card summary and its full content to the dedicated card webhook."""
    for payload in _build_card_payloads(card, full_content, source_name):
        ok, error = _post_payload_result("card-notify", payload)
        if not ok:
            return False, error
    return True, None
