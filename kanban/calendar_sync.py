"""Calendar integration (spec §6).

Two paths:
  1. Apple Calendar / universal fallback — generates an .ics file.
     The file can be opened with `open path.ics` on macOS to prompt import.
  2. Google Calendar — uses google-api-python-client if installed and
     credentials are configured. Otherwise raises RuntimeError.

Both paths return a stable event identifier that gets stored on the Card
(`calendar_event_id`). For .ics we generate our own UID since there is no
server-side event to reference.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Optional

from .models import Card
from .storage import ROOT

EXPORT_DIR = ROOT / "exports" / "calendar"

# Map effort string to minutes; blank/unknown = 30.
_EFFORT_MINUTES: dict[str, int] = {
    "15m": 15, "30m": 30, "45m": 45, "1h": 60, "2h": 120,
}


def effort_minutes(effort: Optional[str]) -> int:
    return _EFFORT_MINUTES.get((effort or "").strip(), 30)


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace(";", "\\;")
            .replace(",", "\\,")
            .replace("\n", "\\n")
    )


def _fmt_utc(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(card: Card, start: dt.datetime, minutes: int) -> tuple[str, str]:
    """Return (uid, ics_text). `start` may be naive (treated as local)."""
    if start.tzinfo is None:
        start = start.astimezone()
    end = start + dt.timedelta(minutes=minutes)
    uid = f"weekflow-{card.id}-{uuid.uuid4().hex[:8]}@weekflow.local"
    dtstamp = _fmt_utc(dt.datetime.now(dt.timezone.utc))
    body = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Weekflow//Personal Kanban//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_fmt_utc(start)}",
        f"DTEND:{_fmt_utc(end)}",
        f"SUMMARY:{_ics_escape(card.title)}",
        f"DESCRIPTION:{_ics_escape(f'Weekflow card · {card.lane} · {card.tag}')}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return uid, body


def schedule_ics(card: Card, start: dt.datetime, minutes: Optional[int] = None) -> tuple[str, Path]:
    """Write an .ics file for `card`. Returns (event_id, file_path)."""
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    uid, text = build_ics(card, start, minutes or effort_minutes(card.effort))
    path = EXPORT_DIR / f"{card.id}-{uid.split('-')[-1].split('@')[0]}.ics"
    path.write_text(text)
    return uid, path


def schedule_google(card: Card, start: dt.datetime, minutes: Optional[int] = None) -> str:
    """Create a Google Calendar event. Returns the Google event id.

    Requires:
      - pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib
      - A service-account JSON at $WEEKFLOW_GCAL_CREDENTIALS (or user OAuth
        token at $WEEKFLOW_GCAL_TOKEN), and optionally $WEEKFLOW_GCAL_CALENDAR
        (defaults to 'primary').
    """
    try:
        from google.oauth2 import service_account  # type: ignore
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Google Calendar support requires `pip install google-api-python-client "
            "google-auth`. Falling back to .ics is recommended."
        ) from e

    creds_path = os.environ.get("WEEKFLOW_GCAL_CREDENTIALS")
    if not creds_path:
        raise RuntimeError("Set WEEKFLOW_GCAL_CREDENTIALS to a service-account JSON path.")

    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/calendar.events"]
    )
    service = build("calendar", "v3", credentials=creds)
    calendar_id = os.environ.get("WEEKFLOW_GCAL_CALENDAR", "primary")

    if start.tzinfo is None:
        start = start.astimezone()
    end = start + dt.timedelta(minutes=minutes or effort_minutes(card.effort))
    event = {
        "summary": card.title,
        "description": f"Weekflow card · {card.lane} · {card.tag}",
        "start": {"dateTime": start.isoformat()},
        "end":   {"dateTime": end.isoformat()},
    }
    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    return created["id"]
