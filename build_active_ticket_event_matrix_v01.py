#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
HOME_TEAM = os.getenv("EVENT_HOME_TEAM", "Lech Poznań")
OUT = Path(os.getenv("ACTIVE_EVENT_MATRIX_PATH", "active_ticket_event_matrix.json"))


def api_get_all(path):
    rows = []
    offset = 0
    while True:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
            "Range": f"{offset}-{offset + 999}",
        }
        req = request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8") or "[]")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += 1000


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main():
    query = parse.urlencode(
        {
            "provider": f"eq.{PROVIDER}",
            "home_team": f"eq.{HOME_TEAM}",
            "select": "id,external_event_id,competition,kickoff_at,away_team",
            "order": "kickoff_at.asc",
            "limit": "1000",
        }
    )
    rows = api_get_all(f"ticket_events?{query}")
    now = datetime.now(timezone.utc)
    matrix = []

    for row in rows:
        kickoff = parse_timestamp(row.get("kickoff_at"))
        if kickoff is None or kickoff <= now:
            continue
        matrix.append(
            {
                "id": str(row["external_event_id"]),
                "competition": row.get("competition") or "",
                "away_team": row.get("away_team") or "",
                "kickoff_at": row.get("kickoff_at"),
            }
        )

    OUT.write_text(
        json.dumps(matrix, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    print(f"Active canonical events: {len(matrix)}")
    for event in matrix:
        print(
            f"  {event['id']} | {event['away_team']} | "
            f"{event['kickoff_at']} | {event['competition'] or 'competition unresolved'}"
        )
    print(f"MATRIX_JSON={OUT.read_text(encoding='utf-8')}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
