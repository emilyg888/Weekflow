"""Weekly reflect report (spec §7).

Generates a markdown summary per ISO week:
  - done count by lane
  - avg cycle time per lane
  - WIP breach count (events that left a column above its cap)
  - simple pattern callouts

Writes to /reports/reflect_{week_start}.md
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import storage
from .models import COL_LABELS, COL_LIMITS, LANE_LABELS

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def _iso_week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _count_breaches_by_week_lane(events: list[dict]) -> dict[tuple[dt.date, str], int]:
    """Walk events in order, maintain running per-column totals, count transitions
    that left a capped column above its limit.
    """
    totals: Counter = Counter()
    breaches: dict[tuple[dt.date, str], int] = defaultdict(int)
    for ev in events:
        if ev["from_col"]:
            totals[ev["from_col"]] -= 1
        totals[ev["to_col"]] += 1
        limit = COL_LIMITS.get(ev["to_col"])
        if limit is not None and totals[ev["to_col"]] > limit:
            try:
                ts = dt.datetime.fromisoformat(ev["ts"])
            except ValueError:
                continue
            week = _iso_week_start(ts.date())
            breaches[(week, ev["lane"])] += 1
    return breaches


def _fmt_cycle(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def generate(week_start: Optional[dt.date] = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if week_start is None:
        week_start = _iso_week_start(dt.date.today())
    week_end = week_start + dt.timedelta(days=6)

    events = storage.all_events()

    # Filter to this week's done events.
    done_by_lane: Counter = Counter()
    cycle_by_lane: defaultdict[str, list[float]] = defaultdict(list)
    for ev in events:
        if ev["to_col"] != "done":
            continue
        try:
            ts = dt.datetime.fromisoformat(ev["ts"])
        except ValueError:
            continue
        if not (week_start <= ts.date() <= week_end):
            continue
        done_by_lane[ev["lane"]] += 1
        # Find matching ready→* event for this card earlier in the log
        card_id = ev["card_id"]
        ready_ts: Optional[dt.datetime] = None
        for e2 in events:
            if e2["card_id"] == card_id and e2["to_col"] == "ready":
                try:
                    ready_ts = dt.datetime.fromisoformat(e2["ts"])
                except ValueError:
                    continue
                break
        if ready_ts:
            cycle_by_lane[ev["lane"]].append((ts - ready_ts).total_seconds())

    breaches = _count_breaches_by_week_lane(events)

    # Render markdown.
    lines: list[str] = []
    lines.append(f"# Weekly Reflect · {week_start.isoformat()} → {week_end.isoformat()}")
    lines.append("")
    lines.append("## Throughput by lane")
    lines.append("")
    lines.append("| Lane | Done | Avg cycle | Breaches |")
    lines.append("|------|------|-----------|----------|")
    total_done = 0
    for lane_id, lane_label in LANE_LABELS.items():
        done = done_by_lane.get(lane_id, 0)
        total_done += done
        cycles = cycle_by_lane.get(lane_id, [])
        avg = sum(cycles) / len(cycles) if cycles else None
        breach = breaches.get((week_start, lane_id), 0)
        lines.append(f"| {lane_label} | {done} | {_fmt_cycle(avg)} | {breach} |")
    lines.append("")
    lines.append(f"**Total shipped this week: {total_done}**")
    lines.append("")

    # Patterns — simple heuristics.
    lines.append("## Patterns")
    lines.append("")
    top_lane = done_by_lane.most_common(1)
    if top_lane and top_lane[0][1] > 0:
        lines.append(f"- Most momentum in **{LANE_LABELS.get(top_lane[0][0], top_lane[0][0])}** "
                     f"({top_lane[0][1]} cards done).")
    dormant = [lid for lid, label in LANE_LABELS.items() if done_by_lane.get(lid, 0) == 0]
    if dormant:
        lines.append(f"- No movement in: {', '.join(LANE_LABELS[l] for l in dormant)}.")
    total_breaches = sum(v for (w, _), v in breaches.items() if w == week_start)
    if total_breaches:
        lines.append(f"- WIP cap was exceeded **{total_breaches}** time(s) — review whether "
                     f"columns are gating the right amount of work.")
    else:
        lines.append("- WIP caps held all week. ✅")
    lines.append("")

    out = REPORTS_DIR / f"reflect_{week_start.isoformat()}.md"
    out.write_text("\n".join(lines))
    return out


def feedback_to_backlog(report_path: Path) -> Optional[Path]:
    """Append the Patterns section of `report_path` to /backlog/raw/weekly_retro.md
    so it gets picked up by the next AI parse cycle. Spec §7 (P2).
    """
    if not report_path.exists():
        return None
    text = report_path.read_text()
    # Extract the Patterns section.
    if "## Patterns" not in text:
        return None
    patterns = text.split("## Patterns", 1)[1].strip()
    if not patterns:
        return None

    from .storage import BACKLOG_DIR
    raw = BACKLOG_DIR / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / "weekly_retro.md"

    header = f"\n\n## Retro from {report_path.stem}\n\n"
    existing = target.read_text() if target.exists() else "# Weekly retros (auto-appended)\n"
    target.write_text(existing + header + patterns + "\n")
    return target
