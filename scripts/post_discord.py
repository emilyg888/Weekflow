#!/usr/bin/env python
"""Post a message to a Discord channel. Spec §5."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban import discord  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True, choices=list(discord.CHANNELS.keys()))
    ap.add_argument("--message", required=True)
    args = ap.parse_args()
    ok = discord.post(args.channel, args.message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
