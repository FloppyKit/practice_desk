from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .config import SA_PATH

SCOPES = ["https://www.googleapis.com/auth/calendar"]


@lru_cache(maxsize=1)
def calendar_service():
    creds = service_account.Credentials.from_service_account_file(
        str(SA_PATH), scopes=SCOPES
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def freebusy(
    calendar_ids: list[str], time_min: datetime, time_max: datetime
) -> list[dict[str, datetime]]:
    svc = calendar_service()
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "items": [{"id": cid} for cid in calendar_ids],
    }
    result = svc.freebusy().query(body=body).execute()
    busy: list[tuple[datetime, datetime]] = []
    for cal in result.get("calendars", {}).values():
        if cal.get("errors"):
            continue
        for b in cal.get("busy", []):
            start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
            busy.append((start, end))
    busy.sort(key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return [{"start": s, "end": e} for s, e in merged]


def _wall(dt: datetime) -> str:
    """Local wall time without offset for Google timeZone field."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def create_event(
    calendar_id: str,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
    timezone: str,
    attendee_email: str | None = None,
    private_props: dict[str, str] | None = None,
    uid: str | None = None,
) -> dict[str, Any]:
    svc = calendar_service()
    body: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": _wall(start), "timeZone": timezone},
        "end": {"dateTime": _wall(end), "timeZone": timezone},
    }
    if uid:
        body["id"] = uid
    if attendee_email:
        body["attendees"] = [{"email": attendee_email}]
    if private_props:
        body["extendedProperties"] = {"private": {str(k): str(v) for k, v in private_props.items()}}
    return (
        svc.events()
        .insert(
            calendarId=calendar_id,
            body=body,
            sendUpdates="none",
        )
        .execute()
    )


def patch_event(
    calendar_id: str,
    event_id: str,
    *,
    description: str | None = None,
    private_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    svc = calendar_service()
    body: dict[str, Any] = {}
    if description is not None:
        body["description"] = description
    if private_props is not None:
        body["extendedProperties"] = {
            "private": {str(k): str(v) for k, v in private_props.items()}
        }
    return (
        svc.events()
        .patch(calendarId=calendar_id, eventId=event_id, body=body)
        .execute()
    )


def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    svc = calendar_service()
    return svc.events().get(calendarId=calendar_id, eventId=event_id).execute()


def list_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    """All events on the calendar in the window (desk week view)."""
    svc = calendar_service()
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = svc.events().list(**kwargs).execute()
        items.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def list_booker_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    """List events created by booker (private prop booker=1) in window."""
    svc = calendar_service()
    items: list[dict[str, Any]] = []
    page_token = None
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
            "privateExtendedProperty": "booker=1",
            "maxResults": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = svc.events().list(**kwargs).execute()
        items.extend(result.get("items") or [])
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return items


def delete_event(calendar_id: str, event_id: str) -> None:
    svc = calendar_service()
    svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
