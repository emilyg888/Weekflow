"""Streamlit board rendering: 4 lanes × 4 columns with drag-and-drop + WIP enforcement."""
from __future__ import annotations

from collections import Counter
from typing import Optional

import streamlit as st
from streamlit_sortables import sort_items

from . import ai_parser, calendar_sync, discord, storage
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
# trailing marker so we can recover identity after a drag.
_SEP = "  ⟨"
_END = "⟩"


def _encode(card: Card) -> str:
    short = card.id[:8]
    prefix = "★ " if card.ai_generated else ""
    suffix = " 📅" if card.scheduled_at else ""
    title = card.title or "(untitled)"
    if card.effort:
        title += f"  · {card.effort}"
    return f"{prefix}{title}{suffix}{_SEP}{short}{_END}"


def _decode_id(encoded: str, id_lookup: dict[str, str]) -> Optional[str]:
    if _SEP not in encoded:
        return None
    short = encoded.rsplit(_SEP, 1)[1].rstrip(_END)
    return id_lookup.get(short)


# --- WIP helpers -----------------------------------------------------------

def _col_totals(cards: list[Card]) -> Counter:
    return Counter(c.col for c in cards)


def _wip_pill(col: str, count: int) -> str:
    limit = COL_LIMITS[col]
    if limit is None:
        return f"{count}"
    # Color hint is emoji since Streamlit markdown is limited in containers.
    if count >= limit + 1:
        dot = "🔴"
    elif count >= limit:
        dot = "🔴"
    elif count >= limit - 1 and limit >= 2:
        dot = "🟠"
    else:
        dot = "🟢"
    return f"{dot} {count}/{limit}"


def _validate_move(to_col: str, current_totals: Counter) -> tuple[bool, str]:
    """Spec §2.2: hard block at limit+1, amber at limit-1, red at limit."""
    limit = COL_LIMITS[to_col]
    if limit is None:
        return True, ""
    projected = current_totals[to_col] + 1
    if projected > limit:
        return False, f"{COL_LABELS[to_col]} is full ({projected}/{limit}). WIP limit enforced."
    return True, ""


# --- Rendering -------------------------------------------------------------

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
                # Pre-check Ready cap before inserting.
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

        # Build list[{'header': col_label, 'items': [item_strings]}].
        buckets_map: dict[str, list[str]] = {COL_LABELS[cid]: [] for cid in COL_IDS}
        for c in lane_cards:
            buckets_map[COL_LABELS[c.col]].append(_encode(c))
        buckets = [
            {"header": COL_LABELS[cid], "items": buckets_map[COL_LABELS[cid]]}
            for cid in COL_IDS
        ]

        key = f"sortable_{lane_id}"
        result = sort_items(
            buckets,
            multi_containers=True,
            direction="horizontal",
            custom_style=CUSTOM_CSS,
            key=key,
        )

        # Detect moves within this lane and persist. `result` has same shape.
        label_to_col = {COL_LABELS[cid]: cid for cid in COL_IDS}
        moves: list[tuple[str, str]] = []  # (card_id, new_col)
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
            # Apply moves while tracking totals for WIP enforcement.
            live_totals = _col_totals(storage.load_cards())
            blocked: list[str] = []
            for cid, new_col in moves:
                card = storage.get_card(cid)
                if card is None:
                    continue
                # Temporarily decrement old column
                live_totals[card.col] -= 1
                ok, msg = _validate_move(new_col, live_totals)
                if not ok:
                    live_totals[card.col] += 1
                    blocked.append(f"'{card.title}': {msg}")
                    continue
                prev_col = card.col
                storage.move_card(cid, new_col, lane_id)
                live_totals[new_col] += 1
                # Discord #done-log on Ready→Done (spec §5).
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

    tab_edit, tab_detail = st.tabs(["Edit", "Detail"])

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
            save_col, delete_col = st.columns([1, 1])
            saved = save_col.form_submit_button("Save", use_container_width=True)
            deleted = delete_col.form_submit_button("🗑 Delete", use_container_width=True)
            if saved:
                from .models import now_iso
                card.title = new_title.strip() or card.title
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

    with tab_detail:
        st.markdown(f"**{card.title}**")
        st.caption(f"`{card.id}`")
        meta_cols = st.columns(4)
        meta_cols[0].metric("Lane", LANE_LABELS[card.lane])
        meta_cols[1].metric("Column", COL_LABELS[card.col])
        meta_cols[2].metric("Tag", card.tag)
        meta_cols[3].metric("Effort", card.effort or "—")
        st.caption(f"Created {card.created_at} · Updated {card.updated_at}")
        if card.scheduled_at:
            ev = f" · event `{card.calendar_event_id}`" if card.calendar_event_id else ""
            st.caption(f"📅 Scheduled: {card.scheduled_at}{ev}")

        # --- Schedule form (spec §6) ---
        with st.expander("📅 Schedule", expanded=False):
            import datetime as _dt
            from .models import now_iso
            default_date = _dt.date.today() + _dt.timedelta(days=1)
            sc1, sc2, sc3 = st.columns([1, 1, 1])
            sched_date = sc1.date_input("Date", value=default_date, key=f"sd_{card.id}")
            sched_time = sc2.time_input("Time", value=_dt.time(9, 0), key=f"st_{card.id}")
            sched_mins = sc3.number_input(
                "Duration (min)",
                min_value=15, max_value=240, step=15,
                value=calendar_sync.effort_minutes(card.effort),
                key=f"sm_{card.id}",
            )
            bcols = st.columns([1, 1, 1])
            if bcols[0].button("📥 Export .ics", key=f"ics_{card.id}", use_container_width=True):
                start = _dt.datetime.combine(sched_date, sched_time)
                uid, path = calendar_sync.schedule_ics(card, start, int(sched_mins))
                card.scheduled_at = start.isoformat(timespec="minutes")
                card.calendar_event_id = uid
                card.updated_at = now_iso()
                storage.update_card(card)
                st.success(f"Wrote `{path.relative_to(storage.ROOT)}`.")
                with open(path, "rb") as fh:
                    st.download_button(
                        "⬇︎ Download .ics",
                        fh.read(),
                        file_name=path.name,
                        mime="text/calendar",
                        key=f"dl_{card.id}",
                    )
                st.rerun()
            if bcols[1].button("📆 Google Calendar", key=f"gcal_{card.id}", use_container_width=True):
                start = _dt.datetime.combine(sched_date, sched_time)
                try:
                    ev_id = calendar_sync.schedule_google(card, start, int(sched_mins))
                except RuntimeError as e:
                    st.error(str(e))
                else:
                    card.scheduled_at = start.isoformat(timespec="minutes")
                    card.calendar_event_id = ev_id
                    card.updated_at = now_iso()
                    storage.update_card(card)
                    st.success("Pushed to Google Calendar.")
                    st.rerun()
            if card.scheduled_at and bcols[2].button(
                "✖ Clear schedule", key=f"clr_{card.id}", use_container_width=True
            ):
                card.scheduled_at = None
                card.calendar_event_id = None
                card.updated_at = now_iso()
                storage.update_card(card)
                st.rerun()

        st.markdown("**Transition history**")
        history = storage.card_history(card.id)
        if not history:
            st.caption("No events logged yet.")
        else:
            import pandas as pd
            rows = [
                {
                    "ts": h["ts"],
                    "lane": LANE_LABELS.get(h["lane"], h["lane"]),
                    "from": COL_LABELS.get(h["from_col"], h["from_col"] or "—"),
                    "to": COL_LABELS.get(h["to_col"], h["to_col"]),
                }
                for h in history
            ]
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


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
