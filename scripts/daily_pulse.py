#!/usr/bin/env python
"""Morning pulse: post current WIP + top 2 Ready picks to #daily-pulse. Spec §5.

Wire into cron / launchd at your preferred morning time, e.g.:
    0 7 * * *  cd /path/to/Weekflow && .venv/bin/python scripts/daily_pulse.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban import discord, storage  # noqa: E402
from kanban.models import LANE_LABELS  # noqa: E402


def main() -> int:
    storage.init_storage()
    cards = storage.load_cards()
    wip = [c for c in cards if c.col == "wip"]
    ready = [c for c in cards if c.col == "ready"][:2]

    lines = ["🌅 **Daily Pulse**", ""]
    lines.append(f"**In progress ({len(wip)}):**")
    if wip:
        for c in wip:
            lines.append(f"  • {c.title} · {LANE_LABELS[c.lane]} · {c.effort or 'no effort'}")
    else:
        lines.append("  _(empty — pull from Ready)_")
    lines.append("")
    lines.append("**Top picks from Ready:**")
    if ready:
        for c in ready:
            lines.append(f"  • {c.title} · {LANE_LABELS[c.lane]}")
    else:
        lines.append("  _(Ready is empty)_")

    msg = "\n".join(lines)
    ok = discord.post("daily-pulse", msg)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
