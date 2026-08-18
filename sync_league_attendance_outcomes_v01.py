#!/usr/bin/env python3

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
SOURCE_URL = os.getenv(
    "ATTENDANCE_SOURCE_URL",
    "https://www.frekwencyjny.pl/kluby/lech-poznan",
)
SOURCE_NAME = "frekwencyjny.pl"
PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
HOME_TEAM = os.getenv("EVENT_HOME_TEAM", "Lech Poznań")
COMPETITION = "Ekstraklasa"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
MIN_POST_KICKOFF_HOURS = float(os.getenv("MIN_POST_KICKOFF_HOURS", "2"))
STADIUM_CAPACITY = int(os.getenv("STADIUM_CAPACITY", "43269"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "attendance_outcome_artifacts_v01"))
WARSAW = ZoneInfo("Europe/Warsaw")


MATCH_RE = re.compile(
    r"\bLech\s+(?P<home_goals>\d+):(?P<away_goals>\d+)\s+"
    r"(?P<opponent>.{1,60}?)\s+"
    r"(?P<attendance>\d{1,3}(?:\s+\d{3})?)\s+"
    r"(?P<season>\d{4}/\d{4})\s*[•·]\s*Ekstraklasa\s*[•·]\s*"
    r"(?P<date>\d{2}\.\d{2}\.\d{4})\s+"
    r"(?P<time>\d{2}:\d{2})",
    re.IGNORECASE,
)


def simplify(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.lower().replace("ł", "l")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_key(name):
    normalized = simplify(name)
    aliases = [
        (["jagiellonia"], "jagiellonia"),
        (["rakow"], "rakow"),
        (["piast"], "piast"),
        (["cracovia"], "cracovia"),
        (["wisla plock", "wisla pl"], "wisla_plock"),
        (["widzew"], "widzew"),
        (["gornik zabrze", "gornik z"], "gornik_zabrze"),
        (["radomiak"], "radomiak"),
        (["slask"], "slask"),
        (["korona"], "korona"),
        (["legia"], "legia"),
        (["wisla krakow", "wisla k"], "wisla_krakow"),
        (["zaglebie lubin", "zaglebie l"], "zaglebie_lubin"),
        (["gks katowice", "gks k"], "gks_katowice"),
        (["pogon"], "pogon"),
        (["motor"], "motor"),
        (["wieczysta"], "wieczysta"),
        (["arka"], "arka"),
        (["lechia"], "lechia"),
        (["termalica", "nieciecza"], "termalica"),
    ]
    for needles, key in aliases:
        if any(needle in normalized for needle in needles):
            return key
    return normalized.replace(" ", "_")


def season_for_date(local_date):
    year = local_date.year
    if local_date.month >= 7:
        return f"{year:04d}/{year + 1:04d}"
    return f"{year - 1:04d}/{year:04d}"


def api(path, method="GET", body=None, prefer=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Accept": "application/json",
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


def load_candidate_events(now):
    query = parse.urlencode(
        {
            "provider": f"eq.{PROVIDER}",
            "home_team": f"eq.{HOME_TEAM}",
            "competition": f"eq.{COMPETITION}",
            "select": "id,external_event_id,home_team,away_team,competition,match_date,kickoff_at",
            "order": "kickoff_at.asc",
            "limit": "1000",
        }
    )
    rows = api(f"ticket_events?{query}") or []

    outcome_query = parse.urlencode(
        {
            "select": "ticket_event_id,actual_attendance,source_name",
            "limit": "1000",
        }
    )
    outcomes = api(f"ticket_event_outcomes?{outcome_query}") or []
    resolved_ids = {int(row["ticket_event_id"]) for row in outcomes}

    candidates = []
    for row in rows:
        if int(row["id"]) in resolved_ids:
            continue
        kickoff = datetime.fromisoformat(row["kickoff_at"].replace("Z", "+00:00"))
        hours_after_kickoff = (now - kickoff.astimezone(timezone.utc)).total_seconds() / 3600
        if hours_after_kickoff < MIN_POST_KICKOFF_HOURS:
            continue
        candidates.append({**row, "hours_after_kickoff": hours_after_kickoff})
    return candidates


def fetch_source_matches():
    response = requests.get(
        SOURCE_URL,
        timeout=45,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True).replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    matches = {}
    for match in MATCH_RE.finditer(text):
        local_datetime = datetime.strptime(
            f"{match.group('date')} {match.group('time')}",
            "%d.%m.%Y %H:%M",
        ).replace(tzinfo=WARSAW)
        attendance = int(re.sub(r"\s+", "", match.group("attendance")))
        opponent = match.group("opponent").strip()
        record = {
            "opponent": opponent,
            "opponent_key": team_key(opponent),
            "attendance": attendance,
            "season": match.group("season"),
            "local_date": local_datetime.date().isoformat(),
            "local_kickoff": local_datetime.isoformat(),
            "score": f"{match.group('home_goals')}:{match.group('away_goals')}",
        }
        key = (
            record["opponent_key"],
            record["local_date"],
            record["attendance"],
            record["score"],
        )
        matches[key] = record

    return sorted(matches.values(), key=lambda row: row["local_kickoff"], reverse=True)


def match_event(event, source_matches):
    kickoff = datetime.fromisoformat(event["kickoff_at"].replace("Z", "+00:00"))
    local_date = kickoff.astimezone(WARSAW).date()
    expected_season = season_for_date(local_date)
    expected_opponent = team_key(event["away_team"])

    candidates = [
        row
        for row in source_matches
        if row["local_date"] == local_date.isoformat()
        and row["opponent_key"] == expected_opponent
        and row["season"] == expected_season
    ]

    unique = {
        (row["attendance"], row["score"]): row
        for row in candidates
    }
    if len(unique) != 1:
        return None, len(unique)
    return next(iter(unique.values())), 1


def persist_outcome(event, source_match, now):
    attendance = int(source_match["attendance"])
    if attendance <= 0 or attendance > STADIUM_CAPACITY:
        raise RuntimeError(
            f"Attendance {attendance} for event {event['external_event_id']} "
            f"is outside expected range 1..{STADIUM_CAPACITY}."
        )

    payload = {
        "ticket_event_id": int(event["id"]),
        "actual_attendance": attendance,
        "attendance_definition": "reported_match_attendance",
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "confirmed_at": now.isoformat(),
        "notes": (
            "Automatically matched by local match date and normalized opponent; "
            f"source score={source_match['score']}."
        ),
        "updated_at": now.isoformat(),
    }

    if DRY_RUN:
        return {**payload, "dry_run": True}

    query = parse.urlencode({"on_conflict": "ticket_event_id"})
    rows = api(
        f"ticket_event_outcomes?{query}",
        "POST",
        payload,
        "resolution=merge-duplicates,return=representation",
    ) or []
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected one persisted outcome for event {event['external_event_id']}; "
            f"found {len(rows)}."
        )
    return rows[0]


def main():
    now = datetime.now(timezone.utc)
    source_matches = fetch_source_matches()
    current_season_matches = [
        row for row in source_matches if row["season"] == season_for_date(now.astimezone(WARSAW).date())
    ]

    if not current_season_matches:
        raise RuntimeError(
            "Attendance source returned no completed Lech Ekstraklasa matches "
            "for the current season."
        )

    candidates = load_candidate_events(now)
    results = []
    new_outcomes = 0

    for event in candidates:
        source_match, match_count = match_event(event, source_matches)
        if source_match is None:
            results.append(
                {
                    "event_id": event["external_event_id"],
                    "away_team": event["away_team"],
                    "status": "waiting_for_source",
                    "source_match_count": match_count,
                }
            )
            continue

        persisted = persist_outcome(event, source_match, now)
        new_outcomes += 1
        results.append(
            {
                "event_id": event["external_event_id"],
                "away_team": event["away_team"],
                "status": "dry_run_match" if DRY_RUN else "persisted",
                "actual_attendance": source_match["attendance"],
                "source_score": source_match["score"],
                "ticket_event_id": int(event["id"]),
                "persisted_id": persisted.get("ticket_event_id"),
            }
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "generated_at": now.isoformat(),
        "dry_run": DRY_RUN,
        "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "source_match_count": len(source_matches),
        "current_season_source_match_count": len(current_season_matches),
        "pending_canonical_event_count": len(candidates),
        "new_outcome_count": new_outcomes,
        "results": results,
        "matching_rule": (
            "Exact Europe/Warsaw match date + normalized opponent + derived season; "
            "only completed Ekstraklasa source rows with a score and attendance are eligible."
        ),
        "safety": (
            "Existing ticket_event_outcomes are never overwritten by the automatic sync. "
            "Non-league events are excluded."
        ),
    }
    report_path = OUTPUT_DIR / "league_attendance_outcome_sync_v01.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Source completed matches parsed: {len(source_matches)}")
    print(f"Current-season source matches: {len(current_season_matches)}")
    print(f"Pending canonical league events: {len(candidates)}")
    print(f"New outcomes: {new_outcomes}")
    print(f"Dry run: {DRY_RUN}")
    for row in results:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    print(f"Report: {report_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
