#!/usr/bin/env python
"""Parse /backlog/raw/ into staged AI candidates. Spec §3.2."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban import ai_parser, discord  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse backlog notes into staged cards.")
    ap.add_argument(
        "--no-llm",
        action="store_true",
        help="Force mock extractor (skip LM Studio/LLM).",
    )
    ap.add_argument("--notify", action="store_true", help="Post to #backlog-bucket on success.")
    args = ap.parse_args()

    use_llm = None if not args.no_llm else False
    cands = ai_parser.parse_all(use_llm=use_llm)
    print(f"Parsed {len(cands)} candidate(s) into staging.")
    if args.notify and cands:
        discord.post("backlog-bucket", f"📥 {len(cands)} new cards ready for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
