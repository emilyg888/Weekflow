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


def _pill_state(col: str, count: int) -> str:
    """Return 'ok' | 'warn' | 'over' | 'none' based on count vs cap (UI-5)."""
    limit = COL_LIMITS[col]
    if limit is None:
        return "none"
    if count > limit:
        return "over"
    if count == limit:
        return "warn"
    return "ok"


def _wip_pill_html(col: str, count: int) -> str:
    """Render the WIP pill for a column header (UI-5)."""
    limit = COL_LIMITS[col]
    if limit is None:
        return f'<span class="wf-pill ok">{count}</span>'
    state = _pill_state(col, count)
    return f'<span class="wf-pill {state}">{count}/{limit}</span>'


def _validate_move(to_col: str, current_totals: Counter) -> tuple[bool, str]:
    """Spec §2.2: hard block at limit+1, amber at limit-1, red at limit."""
    limit = COL_LIMITS[to_col]
    if limit is None:
        return True, ""
    projected = current_totals[to_col] + 1
    if projected > limit:
        return False, f"{COL_LABELS[to_col]} is full ({projected}/{limit}). WIP limit enforced."
    return True, ""


# --- Rendering (v2 — light theme, horizontal grid per UI-1 … UI-7) -------

# Palette — warm dark theme
_PAGE_BG = "#1C1B19"
_BOARD_BG = "#1C1B19"
_COL_BG = "#201E1C"
_CARD_BG = "#26241F"
_HEADER_BG = "#26241F"
_BORDER = "#38362F"
_BORDER_HOVER = "#6B6557"
_TEXT_PRIMARY = "#F2F1ED"
_TEXT_MUTED = "#8B8A80"

# Lane accent colours — brighter variants for dark bg
_LANE_COLORS: dict[str, str] = {
    "deep":   "#7B6FE0",
    "growth": "#4FC18E",
    "health": "#4A9BE0",
    "admin":  "#E67E47",
}

# WIP pill colours — dark mode
_PILL_CSS = {
    "ok":   ("#1F3220", "#8FC97B"),
    "warn": ("#3A2D10", "#E8B85C"),
    "over": ("#3A1515", "#E77766"),
}

# Global page CSS (UI-2, UI-7). Injected once per render via st.markdown.
_PAGE_CSS = f"""<style>
:root {{
  --bg-page: {_PAGE_BG};
  --bg-board: {_BOARD_BG};
  --bg-col: {_COL_BG};
  --bg-card: {_CARD_BG};
  --bg-header: {_HEADER_BG};
  --border: {_BORDER};
  --border-hover: {_BORDER_HOVER};
  --text-primary: {_TEXT_PRIMARY};
  --text-muted: {_TEXT_MUTED};
}}
.stApp, [data-testid="stAppViewContainer"], section[data-testid="stMain"],
[data-testid="stHeader"], [data-testid="stSidebar"] {{
  background: var(--bg-page) !important;
  color: var(--text-primary) !important;
}}
[data-testid="stSidebar"] {{
  border-right: 1px solid var(--border);
}}
/* Buttons, inputs and text adopt the dark palette */
.stButton > button:not([disabled]) {{
  background: #26241F !important;
  color: var(--text-primary) !important;
  border: 1px solid var(--border) !important;
  cursor: pointer !important;
  opacity: 1 !important;
}}
.stButton > button:not([disabled]):hover {{
  border-color: {_BORDER_HOVER} !important;
  background: #2C2923 !important;
}}
.stButton > button:not([disabled]):focus {{
  box-shadow: 0 0 0 1px {_BORDER_HOVER} !important;
}}
.stButton > button[disabled] {{
  background: #26241F !important;
  color: var(--text-muted) !important;
  border: 1px solid var(--border) !important;
  opacity: 0.45 !important;
  cursor: not-allowed !important;
}}
.stTextInput > div > div > input,
.stSelectbox [data-baseweb="select"] > div,
.stNumberInput input,
.stDateInput input,
.stTimeInput input {{
  background: #26241F !important;
  color: var(--text-primary) !important;
  border-color: var(--border) !important;
}}
p, span, li, label, .stMarkdown, .stMarkdown p {{
  color: var(--text-primary);
}}
.main .block-container {{
  padding-top: 1rem;
  max-width: 100% !important;
}}
/* Typography (UI-7, scaled up per user preference) */
h1 {{
  font-size: 28px !important;
  font-weight: 600 !important;
  color: var(--text-primary) !important;
  margin: 0 0 4px 0 !important;
  padding: 0 !important;
}}
h2, h3 {{
  color: var(--text-primary) !important;
}}
[data-testid="stCaptionContainer"] {{
  color: var(--text-muted) !important;
  font-size: 12px !important;
}}
/* Row-gap tightening so lanes don't feel airy */
[data-testid="stHorizontalBlock"] {{
  gap: 6px !important;
  align-items: stretch !important;
}}
[data-testid="stVerticalBlock"] > div {{
  gap: 0.25rem;
}}

/* Column header bar (UI-1 sticky + UI-5 pills) */
.wf-col-headers {{
  position: sticky;
  top: 2.8rem;
  z-index: 50;
  background: var(--bg-header);
  border: 1.5px solid var(--border);
  border-radius: 6px;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0;
  margin: 2px 0 8px 0;
  box-shadow: 0 1px 3px rgba(0,0,0,0.4);
  overflow: hidden;
}}
.wf-col-head {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  min-width: 0;
  border-right: 1px solid rgba(255,255,255,0.06);
}}
.wf-col-head:last-child {{ border-right: none; }}
.wf-col-head.warn {{ box-shadow: inset 0 0 0 1.5px #8A6420; }}
.wf-col-head.over {{ box-shadow: inset 0 0 0 1.5px #A84040; }}
.wf-col-title {{
  display: flex; flex-direction: column;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--text-primary);
  line-height: 1.2;
}}
.wf-col-sub {{
  font-size: 11px;
  text-transform: none;
  letter-spacing: 0;
  color: var(--text-muted);
  font-weight: 400;
}}
.wf-pill {{
  font-size: 12px;
  font-weight: 600;
  padding: 3px 9px;
  border-radius: 10px;
  border: 1px solid rgba(255,255,255,0.06);
}}
.wf-pill.ok   {{ background: {_PILL_CSS['ok'][0]};   color: {_PILL_CSS['ok'][1]}; }}
.wf-pill.warn {{ background: {_PILL_CSS['warn'][0]}; color: {_PILL_CSS['warn'][1]}; }}
.wf-pill.over {{ background: {_PILL_CSS['over'][0]}; color: {_PILL_CSS['over'][1]}; }}

/* Lane label cell (UI-1 right label column, UI-3 dot) */
.wf-lane-label {{
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-direction: row-reverse;
  gap: 8px;
  padding: 8px 10px 8px 4px;
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  min-height: 84px;
  text-align: right;
  border-right: 4px solid transparent;
}}
.wf-lane-dot {{
  width: 12px;
  height: 12px;
  border-radius: 50%;
  flex-shrink: 0;
}}

/* Week badge (UI-7) */
.wf-week-badge {{
  display: inline-block;
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
  background: var(--bg-col);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 2px 10px;
  margin-left: 8px;
}}
</style>
"""


def _sortable_css(lane_color: str) -> str:
    """CSS passed into the streamlit-sortables iframe. Scoped per lane so each
    lane's cards get the correct accent stripe (UI-3).
    """
    return f"""
* {{ box-sizing: border-box; }}
body {{ background: {_BOARD_BG}; margin: 0; padding: 0; }}
.sortable-component {{
  display: grid !important;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  padding: 0;
  align-items: stretch;
}}
.sortable-container {{
  background: {_COL_BG};
  border: 1.5px solid {_BORDER};
  border-radius: 6px;
  padding: 6px;
  min-height: 132px;
  display: flex;
}}
/* Header is rendered outside the iframe, hide the duplicate inside */
.sortable-container-header {{ display: none; }}
.sortable-container-body {{
  padding: 0;
  min-height: 100%;
  width: 100%;
  display: flex;
  align-items: center;
  flex: 1 1 auto;
}}
ul {{
  list-style: none;
  padding: 0;
  margin: 0;
  width: 100%;
  min-height: 100%;
  display: flex;
  flex-direction: column;
  justify-content: center;
  align-items: center;
  flex: 1 1 auto;
}}

.sortable-item {{
  background: {_CARD_BG};
  border: 1.5px solid {_BORDER};
  border-left: 4px solid {lane_color};
  border-radius: 8px;
  padding: 6px;
  width: 64px;
  min-height: 64px;
  aspect-ratio: 1 / 1;
  margin: 0 0 6px 0;
  font-size: 11px;
  font-weight: 600;
  color: {_TEXT_PRIMARY};
  line-height: 1.1;
  white-space: pre-line;
  word-break: break-word;
  overflow: hidden;
  cursor: grab;
  list-style: none;
  box-shadow: 0 1px 3px rgba(0,0,0,0.35);
  transition: border-color 120ms ease, box-shadow 120ms ease;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}}
.sortable-item:last-child {{ margin-bottom: 0; }}
.sortable-item:hover {{
  border-color: {_BORDER_HOVER};
  border-left-color: {lane_color};
  box-shadow: 0 2px 6px rgba(0,0,0,0.5);
}}
.sortable-item.dragging {{ opacity: 0.35; border-style: dashed; cursor: grabbing; }}

/* Done column (4th container) — dim per UI-4 */
.sortable-container:nth-child(4) .sortable-item {{
  opacity: 0.55;
}}
"""


def _render_column_headers(totals: Counter) -> None:
    """Sticky column-header bar with WIP pills (UI-1, UI-5). Sits above the
    lane rows and spans the same width as the sortable area.
    """
    cells: list[str] = []
    for col_id in COL_IDS:
        limit = COL_LIMITS[col_id]
        state = _pill_state(col_id, totals[col_id])
        head_class = "wf-col-head" + (" warn" if state == "warn" else "" if state != "over" else " over")
        if state == "over":
            head_class = "wf-col-head over"
        sub = (
            f"max {limit}" if limit is not None
            else "resets Sun"
        )
        cells.append(
            f'<div class="{head_class}">'
            f'<div class="wf-col-title">{COL_LABELS[col_id]}'
            f'<span class="wf-col-sub">{sub}</span></div>'
            f'{_wip_pill_html(col_id, totals[col_id])}'
            f'</div>'
        )
    st.markdown(
        '<div class="wf-col-headers">' + "".join(cells) + '</div>',
        unsafe_allow_html=True,
    )


def _render_lane_label(lane_id: str) -> None:
    color = _LANE_COLORS[lane_id]
    st.markdown(
        f'<div class="wf-lane-label" style="border-right-color:{color}">'
        f'<span class="wf-lane-dot" style="background:{color}"></span>'
        f'{LANE_LABELS[lane_id]}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_inline_add_form(lane_id: str) -> None:
    """UI-6 inline add-card form, scoped to a single lane's Ready cell.
    Stored open/closed in session state so the form only appears for the
    lane whose '+ add' was clicked.
    """
    state_key = f"wf_add_open_{lane_id}"
    if not st.session_state.get(state_key):
        if st.button(
            "Add Card",
            key=f"wf_add_btn_{lane_id}",
            help="Create a new card in this lane's Ready column",
            use_container_width=True,
        ):
            st.session_state[state_key] = True
            st.rerun()
        return

    with st.form(f"wf_add_form_{lane_id}", clear_on_submit=True, border=True):
        title = st.text_input(
            "Title",
            max_chars=80,
            placeholder=f"New {LANE_LABELS[lane_id]} card",
            label_visibility="collapsed",
            key=f"wf_add_title_{lane_id}",
        )
        fc1, fc2 = st.columns([1, 1])
        tag = fc1.selectbox(
            "Tag", TAGS, label_visibility="collapsed", key=f"wf_add_tag_{lane_id}"
        )
        effort = fc2.selectbox(
            "Effort", EFFORTS, label_visibility="collapsed", key=f"wf_add_effort_{lane_id}"
        )
        bc1, bc2 = st.columns([1, 1])
        submit = bc1.form_submit_button("Add card", use_container_width=True)
        cancel = bc2.form_submit_button("Cancel", use_container_width=True)
        if cancel:
            st.session_state[state_key] = False
            st.rerun()
        if submit:
            if not title.strip():
                st.warning("Title required.")
                return
            totals = _col_totals(storage.load_cards())
            ok, msg = _validate_move("ready", totals)
            if not ok:
                st.error(msg)
                return
            card = Card(
                title=title.strip(),
                lane=lane_id,  # pre-selected from the lane that was clicked
                col="ready",
                tag=tag,
                effort=effort,
            )
            storage.add_card(card)
            st.session_state[state_key] = False
            st.rerun()


def _open_card(card_id: str) -> None:
    st.session_state["card_manager_select"] = card_id


def render_board() -> None:
    """Horizontal grid board: sticky column headers + 4 swimlane rows.

    Layout is achieved with Streamlit's native columns (`[1, 10]`) for the
    lane-label × card-cells split, and a sticky HTML header bar above. The
    card cells themselves come from streamlit-sortables, styled with a CSS
    grid so the 4 columns align with the header bar.
    """
    # Always-present CSS (cheap; Streamlit dedupes)
    st.markdown(_PAGE_CSS, unsafe_allow_html=True)

    cards = storage.load_cards()
    totals = _col_totals(cards)

    # Header row: 4-col header strip on the left, empty label cell on the right
    head_cols, head_label = st.columns([10, 1])
    with head_cols:
        _render_column_headers(totals)
    with head_label:
        st.markdown("&nbsp;", unsafe_allow_html=True)

    # One row per lane
    for lane_id in LANE_IDS:
        board_col, label_col = st.columns([10, 1])
        with board_col:
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
                custom_style=_sortable_css(_LANE_COLORS[lane_id]),
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

        with label_col:
            _render_lane_label(lane_id)
            _render_inline_add_form(lane_id)


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
