#!/usr/bin/env python3

import json
import os
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
PROVIDER = "roboticket"
EXTERNAL_EVENT_ID = "10234"
MATCH_DATE = "2026-09-06"
KICKOFF_AT = "2026-09-06T17:30:00+02:00"


def api(path, method="GET", body=None, prefer=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
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


def main():
    event_query = parse.urlencode(
        {
            "provider": f"eq.{PROVIDER}",
            "external_event_id": f"eq.{EXTERNAL_EVENT_ID}",
            "select": "id,external_event_id,away_team,match_date,kickoff_at",
        }
    )
    events = api(f"ticket_events?{event_query}") or []
    if len(events) != 1:
        raise RuntimeError(
            f"Expected one canonical {PROVIDER}:{EXTERNAL_EVENT_ID} event; found {len(events)}."
        )
    event = events[0]
    ticket_event_id = int(event["id"])

    legacy_query = parse.urlencode(
        {
            "event_id": f"eq.{EXTERNAL_EVENT_ID}",
            "ticket_event_id": "is.null",
            "select": "id,captured_at,event_id,ticket_event_id",
            "order": "captured_at.asc",
            "limit": "1000",
        }
    )
    legacy = api(f"snapshots?{legacy_query}") or []
    snapshot_ids = [int(row["id"]) for row in legacy]

    if snapshot_ids:
        patch_query = parse.urlencode(
            {
                "event_id": f"eq.{EXTERNAL_EVENT_ID}",
                "ticket_event_id": "is.null",
            }
        )
        updated = api(
            f"snapshots?{patch_query}",
            "PATCH",
            {
                "event_id": None,
                "ticket_event_id": ticket_event_id,
                "event_match_date_at_capture": MATCH_DATE,
                "event_kickoff_at_capture": KICKOFF_AT,
            },
            "return=representation",
        ) or []
        if len(updated) != len(snapshot_ids):
            raise RuntimeError(
                f"Expected to relink {len(snapshot_ids)} snapshots; updated {len(updated)}."
            )

        ids_filter = f"in.({','.join(str(value) for value in snapshot_ids)})"
        sector_query = parse.urlencode({"snapshot_id": ids_filter})
        api(
            f"sector_inventory?{sector_query}",
            "PATCH",
            {"event_id": None},
            "return=minimal",
        )

    verify_query = parse.urlencode(
        {
            "ticket_event_id": f"eq.{ticket_event_id}",
            "select": "id,captured_at,event_id,ticket_event_id,event_match_date_at_capture,event_kickoff_at_capture",
            "order": "captured_at.asc",
            "limit": "1000",
        }
    )
    linked = api(f"snapshots?{verify_query}") or []

    print(f"Canonical ticket_event_id: {ticket_event_id}")
    print(f"Legacy snapshots relinked this run: {len(snapshot_ids)}")
    print(f"Canonical linked snapshots now: {len(linked)}")
    if linked:
        print(f"First linked snapshot: {linked[0]['id']} @ {linked[0]['captured_at']}")
        print(f"Last linked snapshot: {linked[-1]['id']} @ {linked[-1]['captured_at']}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
