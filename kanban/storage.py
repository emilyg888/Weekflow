"""Persistence layer: cards.json is the source of truth, DuckDB mirrors + logs.

Design:
- cards.json  — authoritative current state (spec §4.1)
- events.json — append-only JSON mirror of every transition (spec §4.1)
- kanban.duckdb
    - cards   — upserted mirror of cards.json (spec §4.2)
    - events  — append-only transition log (spec §4.2)
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Optional

import duckdb

from .models import Card, now_iso

# --- Paths ------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BACKLOG_DIR = ROOT / "backlog"
CARDS_JSON = DATA_DIR / "cards.json"
EVENTS_JSON = DATA_DIR / "events.json"
DB_PATH = ROOT / "kanban.duckdb"

_lock = threading.Lock()


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (BACKLOG_DIR / "raw").mkdir(parents=True, exist_ok=True)
    (BACKLOG_DIR / "processed").mkdir(parents=True, exist_ok=True)
    (BACKLOG_DIR / "ai_generated").mkdir(parents=True, exist_ok=True)
    if not CARDS_JSON.exists():
        CARDS_JSON.write_text("[]")
    if not EVENTS_JSON.exists():
        EVENTS_JSON.write_text("[]")


# --- DuckDB -----------------------------------------------------------------

def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(DB_PATH))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id                 VARCHAR PRIMARY KEY,
            title              VARCHAR,
            lane               VARCHAR,
            col                VARCHAR,
            tag                VARCHAR,
            effort             VARCHAR,
            ai_generated       BOOLEAN,
            created_at         VARCHAR,
            updated_at         VARCHAR,
            scheduled_at       VARCHAR,
            calendar_event_id  VARCHAR,
            archived           BOOLEAN
        );
        -- Forward-compat: add column if an older DB is open.
        ALTER TABLE cards ADD COLUMN IF NOT EXISTS calendar_event_id VARCHAR;
        """
    )
    con.execute(
        """
        CREATE SEQUENCE IF NOT EXISTS events_event_id_seq;
        CREATE TABLE IF NOT EXISTS events (
            event_id  BIGINT DEFAULT nextval('events_event_id_seq') PRIMARY KEY,
            card_id   VARCHAR,
            from_col  VARCHAR,
            to_col    VARCHAR,
            lane      VARCHAR,
            ts        VARCHAR
        );
        """
    )
    # Analytics views (spec §4.2 P1).
    # weekly_summary: per ISO week × lane — done count, avg cycle time, breach count.
    # cycle_time:     per card — first ready → first done duration in seconds.
    con.execute(
        """
        CREATE OR REPLACE VIEW cycle_time AS
        WITH ready_ts AS (
            SELECT card_id, MIN(CAST(ts AS TIMESTAMP)) AS ready_at
            FROM events WHERE to_col='ready' GROUP BY card_id
        ),
        done_ts AS (
            SELECT card_id, MIN(CAST(ts AS TIMESTAMP)) AS done_at, ANY_VALUE(lane) AS lane
            FROM events WHERE to_col='done' GROUP BY card_id
        )
        SELECT
            d.card_id,
            d.lane,
            r.ready_at,
            d.done_at,
            date_diff('second', r.ready_at, d.done_at) AS cycle_seconds
        FROM done_ts d LEFT JOIN ready_ts r USING (card_id)
        WHERE r.ready_at IS NOT NULL;
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW weekly_summary AS
        SELECT
            date_trunc('week', CAST(e.ts AS TIMESTAMP)) AS week,
            e.lane,
            COUNT(*) AS done_count,
            AVG(ct.cycle_seconds) AS avg_cycle_seconds
        FROM events e
        LEFT JOIN cycle_time ct USING (card_id)
        WHERE e.to_col='done'
        GROUP BY 1, 2
        ORDER BY 1 DESC, 2;
        """
    )
    return con


def init_storage() -> None:
    _ensure_dirs()
    _connect().close()


# --- JSON helpers -----------------------------------------------------------

def _read_cards_file() -> list[dict]:
    _ensure_dirs()
    try:
        return json.loads(CARDS_JSON.read_text() or "[]")
    except json.JSONDecodeError:
        return []


def _write_cards_file(cards: list[dict]) -> None:
    CARDS_JSON.write_text(json.dumps(cards, indent=2))


def _append_event_file(event: dict) -> None:
    try:
        events = json.loads(EVENTS_JSON.read_text() or "[]")
    except json.JSONDecodeError:
        events = []
    events.append(event)
    EVENTS_JSON.write_text(json.dumps(events, indent=2))


# --- Public API -------------------------------------------------------------

def load_cards() -> list[Card]:
    return [Card.from_dict(d) for d in _read_cards_file() if not d.get("archived")]


def _upsert_card_row(con: duckdb.DuckDBPyConnection, card: Card) -> None:
    con.execute(
        """
        INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (id) DO UPDATE SET
            title=excluded.title,
            lane=excluded.lane,
            col=excluded.col,
            tag=excluded.tag,
            effort=excluded.effort,
            ai_generated=excluded.ai_generated,
            updated_at=excluded.updated_at,
            scheduled_at=excluded.scheduled_at,
            calendar_event_id=excluded.calendar_event_id,
            archived=excluded.archived
        """,
        [
            card.id, card.title, card.lane, card.col, card.tag, card.effort,
            card.ai_generated, card.created_at, card.updated_at,
            card.scheduled_at, card.calendar_event_id, card.archived,
        ],
    )


def add_card(card: Card) -> Card:
    with _lock:
        rows = _read_cards_file()
        rows.append(card.to_dict())
        _write_cards_file(rows)
        con = _connect()
        try:
            _upsert_card_row(con, card)
            event = {
                "card_id": card.id,
                "from_col": None,
                "to_col": card.col,
                "lane": card.lane,
                "ts": card.created_at,
            }
            con.execute(
                "INSERT INTO events (card_id, from_col, to_col, lane, ts) VALUES (?,?,?,?,?)",
                [event["card_id"], event["from_col"], event["to_col"], event["lane"], event["ts"]],
            )
        finally:
            con.close()
        _append_event_file(event)
    return card


def update_card(card: Card) -> Card:
    with _lock:
        rows = _read_cards_file()
        for i, r in enumerate(rows):
            if r["id"] == card.id:
                rows[i] = card.to_dict()
                break
        _write_cards_file(rows)
        con = _connect()
        try:
            _upsert_card_row(con, card)
        finally:
            con.close()
    return card


def move_card(card_id: str, to_col: str, to_lane: Optional[str] = None) -> Optional[Card]:
    """Transition a card. Returns the updated card, or None if not found / no-op."""
    with _lock:
        rows = _read_cards_file()
        target = None
        for r in rows:
            if r["id"] == card_id:
                target = r
                break
        if target is None:
            return None

        from_col = target["col"]
        from_lane = target["lane"]
        new_lane = to_lane or from_lane
        if from_col == to_col and from_lane == new_lane:
            return Card.from_dict(target)

        ts = now_iso()
        target["col"] = to_col
        target["lane"] = new_lane
        target["updated_at"] = ts
        _write_cards_file(rows)

        card = Card.from_dict(target)
        con = _connect()
        try:
            _upsert_card_row(con, card)
            con.execute(
                "INSERT INTO events (card_id, from_col, to_col, lane, ts) VALUES (?,?,?,?,?)",
                [card.id, from_col, to_col, new_lane, ts],
            )
        finally:
            con.close()
        _append_event_file(
            {"card_id": card.id, "from_col": from_col, "to_col": to_col, "lane": new_lane, "ts": ts}
        )
    return card


def delete_card(card_id: str) -> None:
    """Soft-delete: archived=True. Spec §2.2."""
    with _lock:
        rows = _read_cards_file()
        for r in rows:
            if r["id"] == card_id:
                r["archived"] = True
                r["updated_at"] = now_iso()
                break
        _write_cards_file(rows)
        con = _connect()
        try:
            con.execute("UPDATE cards SET archived=TRUE, updated_at=? WHERE id=?", [now_iso(), card_id])
        finally:
            con.close()


def get_card(card_id: str) -> Optional[Card]:
    for c in load_cards():
        if c.id == card_id:
            return c
    return None


def all_events() -> list[dict]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT event_id, card_id, from_col, to_col, lane, ts FROM events ORDER BY event_id"
        ).fetchall()
    finally:
        con.close()
    return [
        {"event_id": r[0], "card_id": r[1], "from_col": r[2], "to_col": r[3], "lane": r[4], "ts": r[5]}
        for r in rows
    ]


def archive_done() -> int:
    """Soft-archive every card currently in Done. Returns count archived."""
    with _lock:
        rows = _read_cards_file()
        n = 0
        for r in rows:
            if r.get("col") == "done" and not r.get("archived"):
                r["archived"] = True
                r["updated_at"] = now_iso()
                n += 1
        _write_cards_file(rows)
        con = _connect()
        try:
            con.execute(
                "UPDATE cards SET archived=TRUE, updated_at=? WHERE col='done' AND archived=FALSE",
                [now_iso()],
            )
        finally:
            con.close()
    return n


def query_weekly_summary() -> list[dict]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT week, lane, done_count, avg_cycle_seconds FROM weekly_summary"
        ).fetchall()
    finally:
        con.close()
    return [
        {"week": r[0], "lane": r[1], "done_count": r[2], "avg_cycle_seconds": r[3]}
        for r in rows
    ]


def card_history(card_id: str) -> list[dict]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT event_id, from_col, to_col, lane, ts FROM events WHERE card_id=? ORDER BY event_id",
            [card_id],
        ).fetchall()
    finally:
        con.close()
    return [
        {"event_id": r[0], "from_col": r[1], "to_col": r[2], "lane": r[3], "ts": r[4]}
        for r in rows
    ]
