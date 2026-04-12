"""Backlog Bucket — Streamlit component.

Scans a filesystem folder for notes/audio/images/docs, lets the user click
to stage them as kanban cards with inferred lane/tag, edit metadata, then
promote the batch into the Ready column.

Exposed entry point: `render_backlog_bucket()` — called from app.py as the
second tab of the main dashboard. Persistence delegates to
`kanban.storage` so cards flow through the same JSON + DuckDB + events
pipeline as the rest of the board.

Configuration: set WEEKFLOW_BACKLOG_PATH to override the default folder.
"""
from __future__ import annotations

import os
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import streamlit as st

from kanban import storage
from kanban.models import COL_LIMITS, EFFORTS, LANE_IDS, LANE_LABELS, TAGS, Card

# ── Config ────────────────────────────────────────────────────────────────
BACKLOG_PATH = Path(
    os.environ.get("WEEKFLOW_BACKLOG_PATH", "/Users/emilygao/Documents/Weekflow_Backlogs")
).expanduser()

_LABEL_TO_ID = {label: lid for lid, label in LANE_LABELS.items()}
_LANE_LABELS_ORDERED = [LANE_LABELS[lid] for lid in LANE_IDS]

# Icon badge palette per extension (kept bright enough to read on dark bg).
EXT_GROUPS: dict[str, tuple[str, str, str]] = {
    "md":   ("Notes",  "#1E2E3D", "#7AB2EA"),
    "txt":  ("Notes",  "#1E2E3D", "#7AB2EA"),
    "mp3":  ("Audio",  "#1F3220", "#8FC97B"),
    "wav":  ("Audio",  "#1F3220", "#8FC97B"),
    "png":  ("Images", "#3A2D10", "#E8B85C"),
    "jpg":  ("Images", "#3A2D10", "#E8B85C"),
    "jpeg": ("Images", "#3A2D10", "#E8B85C"),
    "pdf":  ("Docs",   "#3A1515", "#E77766"),
    "json": ("Data",   "#2A2540", "#A89BE8"),
}

TAG_COLORS: dict[str, tuple[str, str]] = {
    "Architecture": ("#3A2D10", "#E8B85C"),
    "Learning":     ("#1E2E3D", "#7AB2EA"),
    "Experiment":   ("#1F3220", "#8FC97B"),
    "Admin":        ("#2A2926", "#B4B2A9"),
    "Health":       ("#1E2E3D", "#7AB2EA"),
    "Dev":          ("#2A2540", "#A89BE8"),
    "Done":         ("#2A2926", "#8B8A80"),
}

# ── Helpers ───────────────────────────────────────────────────────────────

_TEXT_EXTS = {"md", "txt", "json"}


def _read_path_text(path_str: str, ext: str) -> str:
    if ext not in _TEXT_EXTS:
        return ""
    try:
        text = Path(path_str).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return text.strip()


def _read_uploaded_text(uploaded, ext: str) -> str:
    if ext not in _TEXT_EXTS:
        return ""
    try:
        return uploaded.getvalue().decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""

def _infer_tag(name: str, ext: str) -> str:
    n = name.lower()
    if ext in ("mp3", "wav"):
        return "Learning"
    if ext in ("png", "jpg", "jpeg"):
        return "Architecture"
    if ext == "pdf":
        return "Admin"
    if ext == "json":
        return "Dev"
    if any(k in n for k in ("aws", "study", "learn", "course")):
        return "Learning"
    if any(k in n for k in ("retro", "billing", "admin")):
        return "Admin"
    if any(k in n for k in ("row", "erg", "health", "gym")):
        return "Health"
    if any(k in n for k in ("arch", "pipeline", "diagram", "design")):
        return "Architecture"
    if any(k in n for k in ("exp", "test", "poc")):
        return "Experiment"
    return "Dev"


def _infer_lane_id(tag: str) -> str:
    """Return a lane *id* (deep/growth/health/admin)."""
    return {
        "Architecture": "deep",
        "Dev":          "deep",
        "Learning":     "growth",
        "Experiment":   "growth",
        "Health":       "health",
        "Admin":        "admin",
    }.get(tag, "admin")


def _title_from_filename(name: str) -> str:
    return Path(name).stem.replace("-", " ").replace("_", " ").title()


def _resolve_icloud_placeholder(p: Path) -> tuple[str, bool]:
    """If `p` is a macOS iCloud placeholder (`.foo.md.icloud`), return the
    real display name (`foo.md`) and True. Otherwise return (p.name, False).
    """
    if p.name.startswith(".") and p.name.endswith(".icloud"):
        # Strip leading '.' and trailing '.icloud'.
        return p.name[1:-len(".icloud")], True
    return p.name, False


def _trigger_icloud_download(placeholder: Path, timeout: int = 60) -> bool:
    """Ask iCloud to materialize an offloaded file. Uses `brctl download`
    when available; returns True on success, False otherwise (best-effort).
    """
    try:
        r = subprocess.run(
            ["brctl", "download", str(placeholder.parent)],
            capture_output=True,
            timeout=timeout,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _scan_backlog() -> list[dict]:
    if not BACKLOG_PATH.exists():
        return []
    files: list[dict] = []
    for p in sorted(BACKLOG_PATH.iterdir()):
        if not p.is_file():
            continue
        display_name, offloaded = _resolve_icloud_placeholder(p)
        # Skip other dotfiles (but keep .icloud placeholders, which are now
        # represented by their real name above).
        if not offloaded and p.name.startswith("."):
            continue
        ext = Path(display_name).suffix.lstrip(".").lower()
        stat = p.stat()  # for offloaded files this is the placeholder's stat
        if offloaded:
            size_str = "☁︎ iCloud"
        else:
            size_kb = stat.st_size / 1024
            size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb/1024:.1f} MB"
        mtime = datetime.fromtimestamp(stat.st_mtime)
        group, bg, color = EXT_GROUPS.get(ext, ("Other", "#2A2926", "#8B8A80"))
        files.append({
            "name":        display_name,
            "path":        str(p),                         # placeholder path on disk
            "real_path":   str(p.parent / display_name),   # materialized name
            "ext":         ext,
            "group":       group,
            "size":        size_str,
            "date":        mtime.strftime("%-d %b"),
            "bg":          bg,
            "color":       color,
            "offloaded":   offloaded,
        })
    return files


def _ready_total() -> int:
    return Counter(c.col for c in storage.load_cards())["ready"]


def _stage_file(f: dict) -> None:
    """Push a scanned file into the session's staging queue."""
    tag = _infer_tag(f["name"], f["ext"])
    st.session_state.bb_staged.append({
        "source": f["name"],
        "title":  _title_from_filename(f["name"]),
        "notes":  _read_path_text(f["real_path"], f["ext"]),
        "tag":    tag,
        "lane":   _infer_lane_id(tag),
        "effort": "30m",
    })
    st.session_state.bb_promoted.add(f["name"])


# ── Component CSS (dark palette, scoped selectors) ────────────────────────
_CSS = """
<style>
.bb-section-header {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .06em;
    color: #8B8A80;
    margin: 14px 0 6px;
}
.bb-file-meta-name {
    font-size: 14px;
    font-weight: 600;
    color: #F2F1ED;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.bb-file-meta-sub {
    font-size: 11px;
    color: #8B8A80;
}
.bb-staged {
    background: #26241F;
    border: 1.5px solid #38362F;
    border-left: 4px solid #7B6FE0;
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 10px;
}
.bb-empty {
    text-align: center;
    padding: 36px 16px;
    color: #8B8A80;
    font-size: 13px;
    border: 1.5px dashed #38362F;
    border-radius: 9px;
    background: #201E1C;
}
.bb-source-row {
    font-size: 11px;
    color: #8B8A80;
    margin-top: 4px;
}
.bb-tag-pill {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 9px;
    border-radius: 10px;
    margin-right: 6px;
    border: 1px solid rgba(255,255,255,0.06);
}
</style>
"""


# ── Public entry ──────────────────────────────────────────────────────────

def render_backlog_bucket() -> None:
    """Render the Backlog Bucket view. Called from app.py."""
    st.markdown(_CSS, unsafe_allow_html=True)

    if "bb_staged" not in st.session_state:
        st.session_state.bb_staged = []         # list of dicts being edited
    if "bb_promoted" not in st.session_state:
        st.session_state.bb_promoted = set()    # filenames already staged this session

    col_browser, col_staging = st.columns([3, 2], gap="medium")

    # ── LEFT: File browser ────────────────────────────────────────────────
    with col_browser:
        st.markdown("#### Backlog bucket")
        st.caption(f"`{BACKLOG_PATH}`")

        search = st.text_input(
            "search", placeholder="Search files…", label_visibility="collapsed",
            key="bb_search",
        )
        files = _scan_backlog()
        if not files:
            st.warning(f"Folder not found or empty: `{BACKLOG_PATH}`")
            st.caption(
                "Create the folder (or set `WEEKFLOW_BACKLOG_PATH`) and drop "
                "notes / audio / images / PDFs in — they'll appear here."
            )
        else:
            if search:
                files = [f for f in files if search.lower() in f["name"].lower()]
            st.caption(f"{len(files)} file{'s' if len(files) != 1 else ''} · click to stage")

            groups: dict[str, list[dict]] = {}
            for f in files:
                groups.setdefault(f["group"], []).append(f)

            for group_name, group_files in groups.items():
                st.markdown(
                    f'<div class="bb-section-header">{group_name}</div>',
                    unsafe_allow_html=True,
                )
                for f in group_files:
                    already = f["name"] in st.session_state.bb_promoted
                    dim = "opacity:0.4;" if already else ""
                    icon, info, btn = st.columns([1, 5, 2])
                    with icon:
                        ext_label = {
                            "md": "Md", "mp3": "♪", "png": "Img", "pdf": "PDF",
                            "txt": "Txt", "json": "{}", "jpg": "Img",
                        }.get(f["ext"], f["ext"].upper())
                        st.markdown(
                            f'<div style="width:38px;height:38px;border-radius:8px;'
                            f'background:{f["bg"]};color:{f["color"]};display:flex;'
                            f'align-items:center;justify-content:center;font-size:12px;'
                            f'font-weight:700;{dim}">{ext_label}</div>',
                            unsafe_allow_html=True,
                        )
                    with info:
                        st.markdown(
                            f'<div style="{dim}">'
                            f'<div class="bb-file-meta-name">{f["name"]}</div>'
                            f'<div class="bb-file-meta-sub">{f["size"]} · {f["date"]}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    with btn:
                        if already:
                            st.caption("Staged")
                        elif f["offloaded"]:
                            if st.button(
                                "↓ Stage",
                                key=f"bb_stage_{f['name']}",
                                help="Download from iCloud and stage",
                                use_container_width=True,
                            ):
                                with st.spinner("Downloading from iCloud…"):
                                    _trigger_icloud_download(Path(f["path"]))
                                _stage_file(f)
                                st.rerun()
                        else:
                            if st.button(
                                "+ Stage",
                                key=f"bb_stage_{f['name']}",
                                use_container_width=True,
                            ):
                                _stage_file(f)
                                st.rerun()

    # ── RIGHT: Staged cards ───────────────────────────────────────────────
    with col_staging:
        st.markdown("#### Staged cards")
        st.caption("Edit metadata then promote to Ready")

        with st.expander("Drop a file from Finder (fallback)", expanded=False):
            uploaded = st.file_uploader(
                "Drag a file from your filesystem",
                accept_multiple_files=False,
                label_visibility="collapsed",
                key="bb_uploader",
            )
            if uploaded is not None and uploaded.name not in st.session_state.bb_promoted:
                ext = Path(uploaded.name).suffix.lstrip(".").lower()
                tag = _infer_tag(uploaded.name, ext)
                st.session_state.bb_staged.append({
                    "source": uploaded.name,
                    "title":  _title_from_filename(uploaded.name),
                    "notes":  _read_uploaded_text(uploaded, ext),
                    "tag":    tag,
                    "lane":   _infer_lane_id(tag),
                    "effort": "30m",
                })
                st.session_state.bb_promoted.add(uploaded.name)
                st.rerun()

        st.markdown("---")

        if not st.session_state.bb_staged:
            st.markdown(
                '<div class="bb-empty">No cards staged yet<br>'
                '<span style="font-size:11px">Click files on the left to add them</span></div>',
                unsafe_allow_html=True,
            )
            return

        to_remove: list[int] = []
        for i, card in enumerate(st.session_state.bb_staged):
            with st.container():
                st.markdown('<div class="bb-staged">', unsafe_allow_html=True)

                st.session_state.bb_staged[i]["title"] = st.text_input(
                    "Title",
                    value=card["title"],
                    key=f"bb_title_{i}",
                    label_visibility="collapsed",
                )

                c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
                with c1:
                    new_tag = st.selectbox(
                        "Tag", TAGS,
                        index=TAGS.index(card["tag"]) if card["tag"] in TAGS else 0,
                        key=f"bb_tag_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state.bb_staged[i]["tag"] = new_tag
                with c2:
                    current_label = LANE_LABELS.get(card["lane"], _LANE_LABELS_ORDERED[0])
                    new_label = st.selectbox(
                        "Lane", _LANE_LABELS_ORDERED,
                        index=_LANE_LABELS_ORDERED.index(current_label),
                        key=f"bb_lane_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state.bb_staged[i]["lane"] = _LABEL_TO_ID[new_label]
                with c3:
                    eff_opts = [e for e in EFFORTS if e]  # drop the blank
                    cur_eff = card["effort"] if card["effort"] in eff_opts else "30m"
                    new_eff = st.selectbox(
                        "Effort", eff_opts,
                        index=eff_opts.index(cur_eff),
                        key=f"bb_effort_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state.bb_staged[i]["effort"] = new_eff
                with c4:
                    if st.button("✕", key=f"bb_rm_{i}"):
                        to_remove.append(i)

                tag_bg, tag_fg = TAG_COLORS.get(card["tag"], ("#2A2926", "#8B8A80"))
                st.markdown(
                    f'<div class="bb-source-row">'
                    f'<span class="bb-tag-pill" style="background:{tag_bg};color:{tag_fg}">{card["tag"]}</span>'
                    f'{card["source"]}'
                    f'</div>',
                    unsafe_allow_html=True,
                )
                st.markdown("</div>", unsafe_allow_html=True)

        for i in reversed(to_remove):
            removed = st.session_state.bb_staged.pop(i)
            st.session_state.bb_promoted.discard(removed["source"])
        if to_remove:
            st.rerun()

        st.markdown("---")

        # WIP-aware promote — enforce Ready cap across all lanes (§1.2).
        n = len(st.session_state.bb_staged)
        ready_now = _ready_total()
        ready_cap = COL_LIMITS["ready"] or 0
        remaining = max(0, ready_cap - ready_now)
        if remaining < n:
            st.caption(
                f"Ready has room for {remaining} more card(s). "
                f"Promoting will add up to {remaining}; the rest stays staged."
            )

        if st.button(
            f"Promote {n} card{'s' if n != 1 else ''} to Ready →",
            type="primary",
            use_container_width=True,
            disabled=remaining == 0,
        ):
            promoted = 0
            leftover: list[dict] = []
            for staged in st.session_state.bb_staged:
                if promoted >= remaining:
                    leftover.append(staged)
                    continue
                card = Card(
                    title=staged["title"][:80],
                    notes=staged.get("notes", ""),
                    lane=staged["lane"] if staged["lane"] in LANE_IDS else "deep",
                    col="ready",
                    tag=staged["tag"] if staged["tag"] in TAGS else "Architecture",
                    effort=staged["effort"] if staged["effort"] in EFFORTS else "",
                    ai_generated=False,
                )
                storage.add_card(card)
                promoted += 1
            st.session_state.bb_staged = leftover
            if not leftover:
                st.session_state.bb_promoted.clear()
            st.success(f"Promoted {promoted} card{'s' if promoted != 1 else ''} to Ready.")
            st.rerun()
