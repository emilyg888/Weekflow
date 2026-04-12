"""Weekflow — Personal Kanban. Streamlit entry point.

Run:
    .venv/bin/streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

import datetime as _dt
import io
import contextlib

from kanban import storage
from kanban.board import (
    render_board,
    render_card_manager,
    render_staging_view,
)


def _run_capture(fn) -> tuple[int, str]:
    """Run a CLI main() while capturing stdout/stderr. Returns (exit_code, log)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = fn() or 0
        except SystemExit as e:
            code = int(e.code or 0)
        except Exception as e:  # noqa: BLE001 — surface to UI
            print(f"ERROR: {e}", file=buf)
            code = 1
    return code, buf.getvalue()


def render_operations() -> None:
    with st.container(border=True):
        action_cols = st.columns([1, 1, 2])
        if action_cols[0].button(
            "🌅 Run Sunday reset",
            use_container_width=True,
            type="primary",
        ):
            from scripts.sunday_reset import main as sunday_main
            with st.spinner("Archiving Done · parsing backlog · writing reflect report…"):
                code, log = _run_capture(sunday_main)
            (st.success if code == 0 else st.error)(f"sunday_reset exited {code}")
            st.code(log or "(no output)", language="text")

        if action_cols[1].button(
            "⏰ Check stale WIP",
            use_container_width=True,
            type="primary",
        ):
            from scripts.stale_wip_check import main as stale_main
            with st.spinner("Scanning WIP cards…"):
                code, log = _run_capture(stale_main)
            (st.success if code == 0 else st.error)(f"stale_wip_check exited {code}")
            st.code(log or "(no output)", language="text")

        action_cols[2].markdown(
            (
                '<div style="text-align:right;line-height:1.1;">'
                '<div style="font-size:0.8rem;color:#8B8A80;text-transform:uppercase;letter-spacing:0.08em;">'
                "Today"
                "</div>"
                f'<div style="font-size:1.7rem;font-weight:700;color:#F2F1ED;">{_dt.date.today().isoformat()}</div>'
                "</div>"
            ),
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(
        page_title="Weekflow · Personal Kanban",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    storage.init_storage()
    st.title("Weekflow")
    st.caption("Capture → shape → execute → reflect → improve.")
    tab_board, tab_cards, tab_staging, tab_operations = st.tabs(
        ["🗂 Board", "✏️ Cards", "🤖 AI Staging", "⚙️ Operations"]
    )
    with tab_board:
        render_board()
    with tab_cards:
        st.subheader("Card detail & edit")
        render_card_manager()
    with tab_staging:
        render_staging_view()
    with tab_operations:
        render_operations()


if __name__ == "__main__":
    main()
