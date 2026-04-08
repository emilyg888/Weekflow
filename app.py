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
    render_add_card_form,
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


def render_sidebar() -> None:
    st.sidebar.header("Operations")
    st.sidebar.caption("Manual triggers for the weekly + safety nets.")

    if st.sidebar.button("🌅 Run Sunday reset", use_container_width=True):
        from scripts.sunday_reset import main as sunday_main
        with st.spinner("Archiving Done · parsing backlog · writing reflect report…"):
            code, log = _run_capture(sunday_main)
        (st.sidebar.success if code == 0 else st.sidebar.error)(
            f"sunday_reset exited {code}"
        )
        st.sidebar.code(log or "(no output)", language="text")

    if st.sidebar.button("⏰ Check stale WIP", use_container_width=True):
        from scripts.stale_wip_check import main as stale_main
        with st.spinner("Scanning WIP cards…"):
            code, log = _run_capture(stale_main)
        (st.sidebar.success if code == 0 else st.sidebar.error)(
            f"stale_wip_check exited {code}"
        )
        st.sidebar.code(log or "(no output)", language="text")

    st.sidebar.divider()
    st.sidebar.caption(f"Today · {_dt.date.today().isoformat()}")


def main() -> None:
    st.set_page_config(page_title="Weekflow · Personal Kanban", layout="wide")
    storage.init_storage()
    st.title("Weekflow")
    st.caption("Capture → shape → execute → reflect → improve.")
    render_sidebar()

    tab_board, tab_cards, tab_staging = st.tabs(
        ["🗂 Board", "✏️ Cards", "🤖 AI Staging"]
    )
    with tab_board:
        render_add_card_form()
        render_board()
    with tab_cards:
        render_card_manager()
    with tab_staging:
        render_staging_view()


if __name__ == "__main__":
    main()
