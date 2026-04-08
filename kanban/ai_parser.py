"""AI backlog parser (spec §3).

Reads /backlog/raw/*.{md,txt}, sends each to an LLM to extract structured
card candidates, writes:
  - /backlog/processed/task_candidates.json  — staging queue (flat list)
  - /backlog/ai_generated/tasks_YYYY-MM-DD.json — per-day snapshot

Mock mode: if OPENAI_API_KEY is not set, falls back to a deterministic
heuristic extractor so the pipeline is usable locally without credentials.

.mp3 (Whisper) and .png (vision) inputs are mentioned in the spec; implement
extension hooks but keep the core parser text-only for now.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import EFFORTS, LANE_IDS, TAGS, Card
from .storage import BACKLOG_DIR


RAW_DIR = BACKLOG_DIR / "raw"
PROCESSED_DIR = BACKLOG_DIR / "processed"
AI_DIR = BACKLOG_DIR / "ai_generated"
STAGING_FILE = PROCESSED_DIR / "task_candidates.json"

SYSTEM_PROMPT = """You convert free-form backlog notes into structured kanban cards.
For each distinct actionable intent in the input, output ONE card.

Return a JSON object with key "cards" whose value is an array of objects with fields:
  title  — <= 80 chars, imperative voice
  lane   — one of: deep, growth, health, admin
  tag    — one of: Architecture, Learning, Experiment, Admin, Health, Dev, Done
  effort — one of: 15m, 30m, 45m, 1h, 2h, or ""

Rules:
- Deep Work = architecture, coding, design, deep research
- Growth    = study, experiments, reading
- Health    = rowing, recovery, meal prep
- Admin     = admin, follow-ups, housekeeping
- Do not invent cards not grounded in the input.
"""


@dataclass
class Candidate:
    title: str
    lane: str = "deep"
    tag: str = "Architecture"
    effort: str = ""
    source_file: str = ""

    def to_card(self) -> Card:
        return Card(
            title=self.title[:80],
            lane=self.lane if self.lane in LANE_IDS else "deep",
            col="ready",
            tag=self.tag if self.tag in TAGS else "Architecture",
            effort=self.effort if self.effort in EFFORTS else "",
            ai_generated=True,
        )

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "lane": self.lane,
            "tag": self.tag,
            "effort": self.effort,
            "source_file": self.source_file,
        }


# --- Extractors -------------------------------------------------------------

def _read_raw_files() -> list[tuple[Path, str]]:
    if not RAW_DIR.exists():
        return []
    out: list[tuple[Path, str]] = []
    for p in sorted(RAW_DIR.iterdir()):
        if p.suffix.lower() in (".md", ".txt") and p.is_file():
            try:
                out.append((p, p.read_text()))
            except OSError:
                continue
    return out


def _mock_extract(text: str) -> list[dict]:
    """Heuristic fallback: one candidate per bullet or non-empty line."""
    lines = [ln.strip() for ln in text.splitlines()]
    cards: list[dict] = []
    for ln in lines:
        if not ln or ln.startswith("#"):
            continue
        # strip bullet markers
        cleaned = re.sub(r"^[-*+]\s+", "", ln)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        if not cleaned:
            continue
        lower = cleaned.lower()
        if any(k in lower for k in ("row", "sleep", "meal", "yoga", "recovery", "walk")):
            lane, tag = "health", "Health"
        elif any(k in lower for k in ("study", "read", "course", "learn", "aws")):
            lane, tag = "growth", "Learning"
        elif any(k in lower for k in ("email", "invoice", "admin", "follow", "call", "book")):
            lane, tag = "admin", "Admin"
        else:
            lane, tag = "deep", "Architecture"
        cards.append({"title": cleaned[:80], "lane": lane, "tag": tag, "effort": ""})
    return cards


def _llm_extract(text: str) -> list[dict]:
    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        return _mock_extract(text)
    if not os.environ.get("OPENAI_API_KEY"):
        return _mock_extract(text)

    client = OpenAI()
    model = os.environ.get("WEEKFLOW_LLM_MODEL", "gpt-4o-mini")
    try:
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        payload = json.loads(resp.choices[0].message.content or "{}")
        cards = payload.get("cards", [])
        return cards if isinstance(cards, list) else []
    except Exception as e:  # noqa: BLE001 — fallback on any API/parse failure
        print(f"[ai_parser] LLM call failed, using mock extractor: {e}")
        return _mock_extract(text)


# --- Orchestration ----------------------------------------------------------

def parse_all(use_llm: Optional[bool] = None) -> list[Candidate]:
    """Parse every raw file into candidates. Appends to staging."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    AI_DIR.mkdir(parents=True, exist_ok=True)

    extractor = _llm_extract if (use_llm if use_llm is not None else bool(os.environ.get("OPENAI_API_KEY"))) else _mock_extract

    candidates: list[Candidate] = []
    for path, text in _read_raw_files():
        for raw in extractor(text):
            candidates.append(
                Candidate(
                    title=str(raw.get("title", "")).strip(),
                    lane=str(raw.get("lane", "deep")),
                    tag=str(raw.get("tag", "Architecture")),
                    effort=str(raw.get("effort", "")),
                    source_file=path.name,
                )
            )

    # Write per-day snapshot.
    today = dt.date.today().isoformat()
    (AI_DIR / f"tasks_{today}.json").write_text(
        json.dumps([c.to_dict() for c in candidates], indent=2)
    )

    # Append to staging queue (not overwriting — user may have unreviewed items).
    existing = load_staging()
    merged = existing + [c.to_dict() for c in candidates]
    STAGING_FILE.write_text(json.dumps(merged, indent=2))
    return candidates


def load_staging() -> list[dict]:
    if not STAGING_FILE.exists():
        return []
    try:
        return json.loads(STAGING_FILE.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def write_staging(items: list[dict]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_FILE.write_text(json.dumps(items, indent=2))


def remove_from_staging(index: int) -> Optional[dict]:
    items = load_staging()
    if 0 <= index < len(items):
        removed = items.pop(index)
        write_staging(items)
        return removed
    return None
