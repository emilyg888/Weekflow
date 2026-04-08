#!/usr/bin/env python
"""Sunday reset: archive Done, clear column, run parser, write reflect report.

Spec §7. Wire into cron at 06:00 Sunday:
    0 6 * * 0  cd /path/to/Weekflow && .venv/bin/python scripts/sunday_reset.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban import ai_parser, discord, reports, storage  # noqa: E402


def main() -> int:
    storage.init_storage()
    archived = storage.archive_done()
    print(f"Archived {archived} Done card(s).")

    # Reflect first so the retro feed-back has fresh patterns BEFORE the parser runs.
    report_path = reports.generate()
    print(f"Wrote reflect report: {report_path}")

    retro = reports.feedback_to_backlog(report_path)
    if retro:
        print(f"Appended retro patterns to: {retro}")

    cands = ai_parser.parse_all()
    print(f"Parsed {len(cands)} backlog candidate(s).")

    # Discord summary + Ready curation prompt (spec §7 P2).
    discord.post(
        "backlog-bucket",
        f"🌅 Sunday reset complete — archived {archived}, staged {len(cands)}. Report: `{report_path.name}`",
    )
    discord.post(
        "backlog-bucket",
        "📋 **Curate Ready** — pick at most 5 cards from staging to commit to this week.",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
