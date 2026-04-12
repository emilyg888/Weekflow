"""Card schema and board constants — single source of truth for vocabulary."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --- Board vocabulary -------------------------------------------------------

LANES: list[tuple[str, str]] = [
    ("deep", "Deep Work"),
    ("growth", "Growth"),
    ("health", "Healthy Living"),
    ("admin", "Life Admin"),
]
LANE_IDS = [k for k, _ in LANES]
LANE_LABELS = dict(LANES)

COLS: list[tuple[str, str]] = [
    ("ready", "Ready"),
    ("wip", "WIP"),
    ("review", "Review"),
    ("done", "Done"),
]
COL_IDS = [k for k, _ in COLS]
COL_LABELS = dict(COLS)

# WIP caps are enforced across all lanes (totals), per spec §1.2.
COL_LIMITS: dict[str, Optional[int]] = {
    "ready": 5,
    "wip": 3,
    "review": 2,
    "done": None,  # unbounded until Sunday reset
}

TAGS = ["Architecture", "Learning", "Experiment", "Admin", "Health", "Dev", "Done"]
EFFORTS = ["", "15m", "30m", "45m", "1h", "2h"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Card -------------------------------------------------------------------

@dataclass
class Card:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    title: str = ""
    notes: str = ""
    lane: str = "deep"
    col: str = "ready"
    tag: str = "Architecture"
    effort: str = ""
    ai_generated: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    archived: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Card":
        # Tolerate extra or missing keys so old files keep loading.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
