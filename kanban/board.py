"""Streamlit board rendering: 4 lanes × 4 columns with drag-and-drop + WIP enforcement."""
from __future__ import annotations

from collections import Counter
import hashlib
import os
import re
from pathlib import Path
from typing import Optional

import streamlit as st
from streamlit_sortables import sort_items

from . import ai_parser, discord, storage
from .models import (
    COL_IDS,
    COL_LABELS,
    COL_LIMITS,
    EFFORTS,
    LANE_IDS,
    LANE_LABELS,
    TAGS,
    Card,
)

# --- Item serialization ----------------------------------------------------
# streamlit-sortables operates on plain strings. We encode card IDs as a
# trailing invisible marker so we can recover identity after a drag without
# showing the short card id on the tile.
_SEP = "\u2063"
_END = "\u2064"
_BIT_ZERO = "\u200b"
_BIT_ONE = "\u200c"
_DEFAULT_BACKLOG_PATH = "/Users/emilygao/Documents/Weekflow_Backlogs"


def _hide_short_id(short: str) -> str:
    bits = "".join(f"{int(ch, 16):04b}" for ch in short)
    return "".join(_BIT_ONE if bit == "1" else _BIT_ZERO for bit in bits)


def _reveal_short_id(hidden: str) -> Optional[str]:
    bits = "".join("1" if ch == _BIT_ONE else "0" for ch in hidden if ch in {_BIT_ZERO, _BIT_ONE})
    if len(bits) != 32:
        return None
    try:
        return "".join(f"{int(bits[i:i + 4], 2):x}" for i in range(0, 32, 4))
    except ValueError:
        return None


def _encode(card: Card) -> str:
    short = card.id[:8]
    title = card.title or "(untitled)"
    label = title
    if card.effort:
        label = f"{title}\n{card.effort}"
    return f"{label}{_SEP}{_hide_short_id(short)}{_END}"


def _decode_id(encoded: str, id_lookup: dict[str, str]) -> Optional[str]:
    if _SEP not in encoded:
        return None
    hidden = encoded.rsplit(_SEP, 1)[1].rstrip(_END)
    short = _reveal_short_id(hidden)
    if short is None:
        return None
    return id_lookup.get(short)


def _lane_widget_key(lane_id: str, lane_cards: list[Card]) -> str:
    """Force the sortable widget to refresh when cards change outside DnD.

    The sortable component keeps internal state by `key`. When a card is
    reassigned via the editor, a static per-lane key can leave the row showing
    stale cards until a full page reload. Include a digest of the lane's card
    state so external edits invalidate the cached widget state.
    """
    parts = [
        "|".join(
            [
                c.id,
                c.title,
                c.lane,
                c.col,
                c.tag,
                c.effort,
                c.updated_at,
                "1" if c.ai_generated else "0",
            ]
        )
        for c in lane_cards
    ]
    digest = hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:12]
    return f"wf_sortable_{lane_id}_{digest}"


def _normalize_lookup(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _matching_backlog_note(card: Card) -> Optional[Path]:
    backlog_path = Path(os.environ.get("WEEKFLOW_BACKLOG_PATH", _DEFAULT_BACKLOG_PATH)).expanduser()
    if not backlog_path.exists():
        return None
    target = _normalize_lookup(card.title)
    for path in sorted(backlog_path.iterdir()):
        if not path.is_file() or path.suffix.lower() not in (".md", ".txt", ".json"):
            continue
        if _normalize_lookup(path.stem) == target:
            return path
    return None


def _card_notes(card: Card) -> tuple[str, Optional[str]]:
    if card.notes.strip():
        return card.notes.strip(), None
    source = _matching_backlog_note(card)
    if source is None:
        return "", None
    try:
        return source.read_text(encoding="utf-8").strip(), source.name
    except (OSError, UnicodeDecodeError):
        return "", source.name


# --- WIP helpers -----------------------------------------------------------

def _col_totals(cards: list[Card]) -> Counter:
    return Counter(c.col for c in cards)



def _validate_move(to_col: str, current_totals: Counter) -> tuple[bool, str]:
    """Spec §2.2: hard block at limit+1, amber at limit-1, red at limit."""
    limit = COL_LIMITS[to_col]
    if limit is None:
        return True, ""
    projected = current_totals[to_col] + 1
    if projected > limit:
        return False, f"{COL_LABELS[to_col]} is full ({projected}/{limit}). WIP limit enforced."
    return True, ""


# --- Rendering (horizontal flex grid per lane) ----------------------------

CUSTOM_CSS = """
.sortable-component {
    display: flex !important;
    flex-direction: row !important;
    gap: 10px;
    align-items: stretch;
}
.sortable-container {
    background: var(--secondary-background-color, #f6f7f9);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 8px;
    padding: 8px;
    min-height: 90px;
    flex: 1;
    display: flex;
    flex-direction: column;
}
.sortable-container-header {
    font-weight: 600;
    font-size: 0.82rem;
    padding: 4px 6px 6px;
    color: var(--text-color, #24292e);
    opacity: 0.7;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    flex-shrink: 0;
}
.sortable-container-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    min-height: 40px;
}
.sortable-item {
    background: var(--background-color, white);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 6px;
    padding: 8px 10px;
    margin: 3px 0;
    font-size: 0.85rem;
    cursor: grab;
    box-shadow: 0 1px 3px rgba(0,0,0,0.12);
    color: var(--text-color, #24292e);
}
.sortable-item:hover {
    border-color: var(--primary-color, #0969da);
    box-shadow: 0 2px 6px rgba(0,0,0,0.15);
}
"""


def _wip_pill(col: str, count: int) -> str:
    limit = COL_LIMITS[col]
    if limit is None:
        return f"{count}"
    if count >= limit:
        dot = "🔴"
    elif count >= limit - 1 and limit >= 2:
        dot = "🟠"
    else:
        dot = "🟢"
    return f"{dot} {count}/{limit}"


def render_add_card_form() -> None:
    with st.expander("➕ Add card", expanded=False):
        with st.form("add_card", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([3, 1.2, 1, 1])
            title = c1.text_input("Title", max_chars=80, label_visibility="collapsed", placeholder="Card title")
            lane = c2.selectbox(
                "Lane",
                LANE_IDS,
                format_func=lambda x: LANE_LABELS[x],
                label_visibility="collapsed",
            )
            tag = c3.selectbox("Tag", TAGS, label_visibility="collapsed")
            effort = c4.selectbox("Effort", EFFORTS, label_visibility="collapsed")
            submitted = st.form_submit_button("Add to Ready", use_container_width=True)
            if submitted:
                if not title.strip():
                    st.warning("Title required.")
                    return
                cards = storage.load_cards()
                totals = _col_totals(cards)
                ok, msg = _validate_move("ready", totals)
                if not ok:
                    st.error(msg)
                    return
                card = Card(title=title.strip(), lane=lane, col="ready", tag=tag, effort=effort)
                storage.add_card(card)
                st.success(f"Added '{card.title}'")
                st.rerun()


def render_board() -> None:
    cards = storage.load_cards()
    totals = _col_totals(cards)

    # Global WIP summary across all lanes.
    summary_cols = st.columns(len(COL_IDS))
    for i, col_id in enumerate(COL_IDS):
        summary_cols[i].metric(
            label=COL_LABELS[col_id],
            value=_wip_pill(col_id, totals[col_id]),
        )

    st.divider()

    # Render each lane as its own horizontal sortable row.
    for lane_id in LANE_IDS:
        st.subheader(LANE_LABELS[lane_id])
        lane_cards = [c for c in cards if c.lane == lane_id]
        id_lookup: dict[str, str] = {c.id[:8]: c.id for c in lane_cards}

        buckets_map: dict[str, list[str]] = {COL_LABELS[cid]: [] for cid in COL_IDS}
        for c in lane_cards:
            buckets_map[COL_LABELS[c.col]].append(_encode(c))
        buckets = [
            {"header": COL_LABELS[cid], "items": buckets_map[COL_LABELS[cid]]}
            for cid in COL_IDS
        ]

        result = sort_items(
            buckets,
            multi_containers=True,
            direction="horizontal",
            custom_style=CUSTOM_CSS,
            key=_lane_widget_key(lane_id, lane_cards),
        )

        # Detect moves within this lane and persist.
        label_to_col = {COL_LABELS[cid]: cid for cid in COL_IDS}
        moves: list[tuple[str, str]] = []
        for bucket in result:
            new_col = label_to_col[bucket["header"]]
            for encoded in bucket["items"]:
                cid = _decode_id(encoded, id_lookup)
                if cid is None:
                    continue
                card = next((c for c in lane_cards if c.id == cid), None)
                if card is None:
                    continue
                if card.col != new_col:
                    moves.append((cid, new_col))

        if moves:
            live_totals = _col_totals(storage.load_cards())
            blocked: list[str] = []
            for cid, new_col in moves:
                card = storage.get_card(cid)
                if card is None:
                    continue
                live_totals[card.col] -= 1
                ok, msg = _validate_move(new_col, live_totals)
                if not ok:
                    live_totals[card.col] += 1
                    blocked.append(f"'{card.title}': {msg}")
                    continue
                prev_col = card.col
                storage.move_card(cid, new_col, lane_id)
                live_totals[new_col] += 1
                if prev_col == "ready" and new_col == "done":
                    discord.post(
                        "done-log",
                        f"✅ **{card.title}** · {LANE_LABELS[lane_id]} · {card.effort or 'no effort'}",
                    )
            if blocked:
                for b in blocked:
                    st.toast(b, icon="🚫")
            st.rerun()


# --- Card management (edit / delete / detail) -----------------------------

def _fmt_card_option(card: Card) -> str:
    return f"[{COL_LABELS[card.col]}] {LANE_LABELS[card.lane]} · {card.title}"


def render_card_manager() -> None:
    """Edit / delete / detail panel — spec §2.2 P1."""
    cards = storage.load_cards()
    if not cards:
        st.info("No cards yet. Add one above to get started.")
        return

    options = {c.id: _fmt_card_option(c) for c in cards}
    selected_id = st.selectbox(
        "Card",
        list(options.keys()),
        format_func=lambda x: options[x],
        key="card_manager_select",
    )
    card = storage.get_card(selected_id)
    if card is None:
        return
    notes_text, source_name = _card_notes(card)

    tab_detail, tab_edit, tab_discord = st.tabs(["Detail", "Edit", "Send to Discord"])

    with tab_detail:
        st.markdown("**Content**")
        if source_name:
            st.caption(f"Loaded from `{source_name}`")
        if notes_text:
            st.text_area(
                "Content",
                value=notes_text,
                height=260,
                disabled=True,
                key=f"card_content_preview_{card.id}",
                label_visibility="collapsed",
            )
        else:
            st.caption("No note content stored for this card yet.")

    with tab_edit:
        with st.form(f"edit_{card.id}"):
            c1, c2, c3, c4 = st.columns([3, 1.2, 1, 1])
            new_title = c1.text_input("Title", value=card.title, max_chars=80)
            new_lane = c2.selectbox(
                "Lane",
                LANE_IDS,
                index=LANE_IDS.index(card.lane),
                format_func=lambda x: LANE_LABELS[x],
            )
            new_tag = c3.selectbox("Tag", TAGS, index=TAGS.index(card.tag) if card.tag in TAGS else 0)
            new_effort = c4.selectbox(
                "Effort", EFFORTS, index=EFFORTS.index(card.effort) if card.effort in EFFORTS else 0
            )
            new_notes = st.text_area(
                "Notes",
                value=card.notes or notes_text,
                height=180,
                placeholder="Card notes or source text",
            )
            save_col, delete_col = st.columns([1, 1])
            saved = save_col.form_submit_button("Save", use_container_width=True)
            deleted = delete_col.form_submit_button("🗑 Delete", use_container_width=True)
            if saved:
                from .models import now_iso
                card.title = new_title.strip() or card.title
                card.notes = new_notes.strip()
                card.lane = new_lane
                card.tag = new_tag
                card.effort = new_effort
                card.updated_at = now_iso()
                storage.update_card(card)
                st.success("Saved.")
                st.rerun()
            if deleted:
                storage.delete_card(card.id)
                st.success(f"Deleted '{card.title}'.")
                st.rerun()

    with tab_discord:
        st.markdown(f"**{card.title}**")
        info_cols = st.columns(4)
        info_cols[0].caption(f"Lane: {LANE_LABELS[card.lane]}")
        info_cols[1].caption(f"Column: {COL_LABELS[card.col]}")
        info_cols[2].caption(f"Tag: {card.tag}")
        info_cols[3].caption(f"Effort: {card.effort or '—'}")
        if st.button("Send to Discord", key=f"notify_{card.id}", use_container_width=False):
            sent, error = discord.post_card_result(card, notes_text, source_name)
            if sent:
                st.success("Card sent to Discord.")
            else:
                st.error(error or "Discord notification failed.")


# --- AI staging view -------------------------------------------------------

def render_staging_view() -> None:
    """Approve/discard AI-generated card candidates — spec §3.2 P1."""
    candidates = ai_parser.load_staging()
    total_raw = len(candidates)

    action_cols = st.columns([1, 1, 4])
    if action_cols[0].button("🔄 Re-parse backlog", use_container_width=True):
        with st.spinner("Parsing /backlog/raw/…"):
            new_cards = ai_parser.parse_all()
        if new_cards:
            discord.post("backlog-bucket", f"📥 {len(new_cards)} new cards ready for review.")
            st.success(f"Parsed {len(new_cards)} new candidate(s).")
        else:
            st.info("No candidates produced. Drop notes into `/backlog/raw/`.")
        st.rerun()

    if action_cols[1].button("🗑 Discard all", use_container_width=True, disabled=total_raw == 0):
        ai_parser.write_staging([])
        st.rerun()

    action_cols[2].caption(f"{total_raw} candidate(s) in staging queue")

    if not candidates:
        st.caption(
            "Staging is empty. Drop `.md` / `.txt` files into `/backlog/raw/` and hit re-parse, "
            "or run `python scripts/parse_backlog.py` from the CLI."
        )
        return

    for i, cand in enumerate(list(candidates)):
        with st.container(border=True):
            c_title, c_lane, c_tag, c_effort, c_approve, c_discard = st.columns(
                [3, 1.2, 1, 1, 1, 1]
            )
            c_title.markdown(
                f"**{cand.get('title', '(untitled)')}**  \n"
                f"<small>from `{cand.get('source_file', '?')}`</small>",
                unsafe_allow_html=True,
            )
            c_lane.caption(LANE_LABELS.get(cand.get("lane", "deep"), cand.get("lane", "")))
            c_tag.caption(cand.get("tag", ""))
            c_effort.caption(cand.get("effort") or "—")
            if c_approve.button("✅", key=f"approve_{i}", help="Promote to Ready"):
                totals = _col_totals(storage.load_cards())
                ok, msg = _validate_move("ready", totals)
                if not ok:
                    st.toast(msg, icon="🚫")
                else:
                    card = Card(
                        title=str(cand.get("title", ""))[:80],
                        lane=cand.get("lane", "deep") if cand.get("lane") in LANE_IDS else "deep",
                        col="ready",
                        tag=cand.get("tag", "Architecture") if cand.get("tag") in TAGS else "Architecture",
                        effort=cand.get("effort", "") if cand.get("effort", "") in EFFORTS else "",
                        ai_generated=True,
                    )
                    storage.add_card(card)
                    ai_parser.remove_from_staging(i)
                    st.rerun()
            if c_discard.button("❌", key=f"discard_{i}", help="Discard candidate"):
                ai_parser.remove_from_staging(i)
                st.rerun()
