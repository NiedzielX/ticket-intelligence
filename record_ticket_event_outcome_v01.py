#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
EVENT_ID = os.environ["EVENT_ID"].strip()
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
ACTUAL_ATTENDANCE = int(os.environ["ACTUAL_ATTENDANCE"])
SOURCE_NAME = os.environ["OUTCOME_SOURCE_NAME"].strip()
SOURCE_URL = os.getenv("OUTCOME_SOURCE_URL", "").strip() or None
ATTENDANCE_DEFINITION = os.getenv(
    "ATTENDANCE_DEFINITION", "official_reported_attendance"
).strip()
MAX_ATTENDANCE = int(os.getenv("MAX_ATTENDANCE", "43269"))


def api(path, method="GET", body=None, prefer=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    req = request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc


def resolve_ticket_event():
    query = parse.urlencode(
        {
            "provider": f"eq.{EVENT_PROVIDER}",
            "external_event_id": f"eq.{EVENT_ID}",
            "select": "id,home_team,away_team,competition,kickoff_at",
        }
    )
    rows = api(f"ticket_events?{query}") or []
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected exactly one event for {EVENT_PROVIDER}:{EVENT_ID}; found {len(rows)}."
        )
    return rows[0]


def validate():
    if ACTUAL_ATTENDANCE < 0:
        raise RuntimeError("ACTUAL_ATTENDANCE cannot be negative.")
    if ACTUAL_ATTENDANCE > MAX_ATTENDANCE:
        raise RuntimeError(
            f"ACTUAL_ATTENDANCE={ACTUAL_ATTENDANCE} exceeds configured venue capacity "
            f"{MAX_ATTENDANCE}. Verify the source before recording the outcome."
        )
    if not SOURCE_NAME:
        raise RuntimeError("OUTCOME_SOURCE_NAME is required.")


def upsert_outcome(ticket_event_id):
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "ticket_event_id": int(ticket_event_id),
        "actual_attendance": ACTUAL_ATTENDANCE,
        "attendance_definition": ATTENDANCE_DEFINITION,
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "confirmed_at": now,
        "updated_at": now,
    }
    query = parse.urlencode({"on_conflict": "ticket_event_id"})
    rows = api(
        f"ticket_event_outcomes?{query}",
        "POST",
        payload,
        "resolution=merge-duplicates,return=representation",
    ) or []
    if len(rows) != 1:
        raise RuntimeError(f"Expected one persisted outcome, found {len(rows)}.")
    return rows[0]


def main():
    validate()
    event = resolve_ticket_event()
    persisted = upsert_outcome(event["id"])
    print(
        f"Recorded outcome for {event['home_team']} vs {event['away_team']}: "
        f"actual_attendance={persisted['actual_attendance']} "
        f"ticket_event_id={persisted['ticket_event_id']}"
    )
    print("SUCCESS")


if __name__ == "__main__":
    main()
