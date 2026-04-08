#!/usr/bin/env python
"""Scan WIP cards for ones stale >24h and nudge #reminders. Spec §5 (P2).

The "last movement" is read from DuckDB events (last transition into WIP for
the card), falling back to Card.updated_at if no event exists.

Wire into cron, e.g. every 4 hours on weekdays:
    0 */4 * * 1-5  cd /path/to/Weekflow && .venv/bin/python scripts/stale_wip_check.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban import discord, storage  # noqa: E402
from kanban.models import LANE_LABELS  # noqa: E402


def _last_wip_entry(events: list[dict], card_id: str) -> dt.datetime | None:
    latest: dt.datetime | None = None
    for ev in events:
        if ev["card_id"] != card_id or ev["to_col"] != "wip":
            continue
        try:
            ts = dt.datetime.fromisoformat(ev["ts"])
        except ValueError:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold-hours", type=float, default=24.0)
    args = ap.parse_args()

    storage.init_storage()
    events = storage.all_events()
    now = dt.datetime.now(dt.timezone.utc)
    stale: list[tuple[str, str, float]] = []

    for card in storage.load_cards():
        if card.col != "wip":
            continue
        last = _last_wip_entry(events, card.id)
        if last is None:
            try:
                last = dt.datetime.fromisoformat(card.updated_at)
            except ValueError:
                continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=dt.timezone.utc)
        hours = (now - last).total_seconds() / 3600
        if hours >= args.threshold_hours:
            stale.append((card.title, LANE_LABELS.get(card.lane, card.lane), hours))

    if not stale:
        print("No stale WIP cards.")
        return 0

    lines = ["⏰ **Stale WIP check**", ""]
    for title, lane, hours in stale:
        lines.append(f"  • {title} · {lane} · idle {hours:.0f}h")
    msg = "\n".join(lines)
    discord.post("reminders", msg)
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
