#!/usr/bin/env python3

import io
import json
import math
import os
import re
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from urllib import parse, request

import numpy as np
import pandas as pd
import requests


SCRIPT_VERSION = "lech-demand-dataset-v1.1"
print(f"Lech demand dataset builder: {SCRIPT_VERSION}")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

CLUB = "Lech Poznań"
CLUB_KEY = "lech"

TARGET_SEASONS = [
    "2017/2018",
    "2018/2019",
    "2019/2020",
    "2020/2021",
    "2021/2022",
    "2022/2023",
    "2023/2024",
    "2024/2025",
    "2025/2026",
]

SOURCE_URL = "https://www.football-data.co.uk/new/POL.csv"

ART = Path("lech_demand_artifacts_v1")
ART.mkdir(exist_ok=True)


# ---------------------------------------------------------------------
# Team normalisation
# ---------------------------------------------------------------------

def simplify(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("ł", "l")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_key(name):
    n = simplify(name)

    # Important: exact/most-specific cases first.
    if n in {"wisla", "wisla k", "wisla krakow"}:
        return "wisla_krakow"
    if n in {"wisla p", "wisla pl", "wisla plock"}:
        return "wisla_plock"

    if n in {"miedz", "legnica", "miedz legnica"}:
        return "miedz"

    if n in {"gks k", "gks katowice", "katowice"}:
        return "gks_katowice"

    if n in {"gornik", "leczna", "gornik leczna"}:
        return "gornik_leczna"
    if n in {"gornik z", "gornik zabrze"}:
        return "gornik_zabrze"

    rules = [
        (["lech poznan"], "lech"),
        (["legia warsaw", "legia warszawa", "legia"], "legia"),
        (["rakow"], "rakow"),
        (["jagiellonia"], "jagiellonia"),
        (["pogon"], "pogon"),
        (["lechia"], "lechia"),
        (["slask"], "slask"),
        (["motor"], "motor"),
        (["radomiak"], "radomiak"),
        (["widzew"], "widzew"),
        (["zaglebie sosnowiec"], "zaglebie_sosnowiec"),
        (["zaglebie lubin", "zaglebie"], "zaglebie_lubin"),
        (["stal mielec", "stal m"], "stal_mielec"),
        (["korona"], "korona"),
        (["cracovia"], "cracovia"),
        (["puszcza", "niepolomice"], "puszcza"),
        (["piast"], "piast"),
        (["arka"], "arka"),
        (["termalica", "nieciecza"], "termalica"),
        (["ruch"], "ruch"),
        (["lks"], "lks"),
        (["podbeskidzie"], "podbeskidzie"),
        (["warta"], "warta"),
        (["sandecja", "nowy sacz"], "sandecja"),
    ]

    for needles, key in rules:
        if any(x in n for x in needles):
            return key

    return n.replace(" ", "_")


# ---------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------

def api_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }


def supabase_get(table, params):
    query = parse.urlencode(params, doseq=True, safe=",.*()")
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"

    req = request.Request(
        url,
        headers=api_headers(),
        method="GET",
    )

    with request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_attendance():
    rows = supabase_get(
        "historical_matches",
        {
            "select": (
                "id,club,opponent,attendance,season,"
                "match_date,restricted_capacity"
            ),
            "club": f"eq.{CLUB}",
            "restricted_capacity": "eq.false",
            "order": "match_date.asc",
            "limit": "1000",
        },
    )

    if not rows:
        raise RuntimeError("No historical_matches returned from Supabase.")

    df = pd.DataFrame(rows)

    df["match_date"] = pd.to_datetime(
        df["match_date"],
        utc=True,
        errors="raise",
    )

    df["local_date"] = (
        df["match_date"]
        .dt.tz_convert("Europe/Warsaw")
        .dt.date
    )

    df["opponent_key"] = df["opponent"].map(team_key)

    return (
        df[df["season"].isin(TARGET_SEASONS)]
        .sort_values("match_date")
        .reset_index(drop=True)
    )


# ---------------------------------------------------------------------
# Football-Data
# ---------------------------------------------------------------------

def download_source():
    print(f"Downloading sporting source: {SOURCE_URL}")

    response = requests.get(
        SOURCE_URL,
        timeout=90,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/126 Safari/537.36"
            ),
            "Accept": "text/csv,text/plain,*/*",
        },
    )

    response.raise_for_status()

    if len(response.content) < 1000:
        raise RuntimeError(
            f"Unexpectedly small Football-Data response: "
            f"{len(response.content)} bytes"
        )

    (ART / "POL_source.csv").write_bytes(response.content)

    return response.content


def normalize_season(value, match_date):
    if value is not None and not pd.isna(value):
        s = str(value).strip()
        m = re.match(r"^(\d{4})[/-](\d{2,4})$", s)

        if m:
            start = int(m.group(1))
            end_raw = m.group(2)
            end = (
                int(end_raw)
                if len(end_raw) == 4
                else 2000 + int(end_raw)
            )
            return f"{start:04d}/{end:04d}"

    year = match_date.year

    if match_date.month >= 7:
        return f"{year:04d}/{year + 1:04d}"

    return f"{year - 1:04d}/{year:04d}"


def normalized_market_probs(home_odds, draw_odds, away_odds):
    values = np.array(
        [home_odds, draw_odds, away_odds],
        dtype=float,
    )

    if np.isnan(values).any() or (values <= 1.0).any():
        return (None, None, None)

    raw = 1.0 / values
    probs = raw / raw.sum()

    return tuple(float(x) for x in probs)


def parse_source(content):
    last_error = None

    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            text = content.decode(encoding)
            df = pd.read_csv(io.StringIO(text))
            break
        except Exception as exc:
            last_error = exc
    else:
        raise RuntimeError(
            f"Could not read Football-Data CSV: {last_error}"
        )

    required = {
        "Season", "Date", "Home", "Away", "HG", "AG"
    }

    missing = required - set(df.columns)

    if missing:
        raise RuntimeError(
            f"Football-Data schema missing: {sorted(missing)}. "
            f"Columns: {df.columns.tolist()}"
        )

    matches = []

    for _, row in df.iterrows():
        raw_date = row.get("Date")
        date = pd.to_datetime(
            raw_date,
            dayfirst=True,
            errors="coerce",
        )

        if pd.isna(date):
            continue

        date = date.to_pydatetime()

        season = normalize_season(
            row.get("Season"),
            date,
        )

        if season not in TARGET_SEASONS:
            continue

        home = str(row.get("Home", "")).strip()
        away = str(row.get("Away", "")).strip()

        hg = pd.to_numeric(
            row.get("HG"),
            errors="coerce",
        )
        ag = pd.to_numeric(
            row.get("AG"),
            errors="coerce",
        )

        if (
            not home
            or not away
            or pd.isna(hg)
            or pd.isna(ag)
        ):
            continue

        hour = 12
        minute = 0

        tm = re.search(
            r"\b(\d{1,2}):(\d{2})\b",
            str(row.get("Time", "")),
        )

        if tm:
            hour = int(tm.group(1))
            minute = int(tm.group(2))

        kickoff = pd.Timestamp(
            year=date.year,
            month=date.month,
            day=date.day,
            hour=hour,
            minute=minute,
            tz="Europe/Warsaw",
        )

        odds = []

        for col in ("AvgCH", "AvgCD", "AvgCA"):
            value = pd.to_numeric(
                row.get(col),
                errors="coerce",
            )
            odds.append(
                None if pd.isna(value) else float(value)
            )

        market = normalized_market_probs(*odds)

        matches.append(
            {
                "season": season,
                "date": date.date(),
                "kickoff": kickoff,
                "home": home,
                "away": away,
                "home_key": team_key(home),
                "away_key": team_key(away),
                "home_goals": int(hg),
                "away_goals": int(ag),
                "avg_home_odds": odds[0],
                "avg_draw_odds": odds[1],
                "avg_away_odds": odds[2],
                "market_home_prob": market[0],
                "market_draw_prob": market[1],
                "market_away_prob": market[2],
            }
        )

    if not matches:
        raise RuntimeError("No target matches parsed from Football-Data.")

    parsed = pd.DataFrame(matches)

    print("")
    print("SPORTING SOURCE BY SEASON")
    print(parsed.groupby("season").size().to_string())

    return matches


# ---------------------------------------------------------------------
# Sporting context reconstruction
# ---------------------------------------------------------------------

def table_positions(stats):
    rows = []

    for key, s in stats.items():
        rows.append(
            (
                key,
                s["pts"],
                s["gf"] - s["ga"],
                s["gf"],
                s["wins"],
            )
        )

    rows.sort(
        key=lambda x: (
            -x[1],
            -x[2],
            -x[3],
            -x[4],
            x[0],
        )
    )

    return {
        key: idx
        for idx, (key, *_rest)
        in enumerate(rows, start=1)
    }


def recent_metrics(history, key):
    games = list(history[key])[-5:]

    return (
        sum(g["points"] for g in games),
        sum(g["gd"] for g in games),
    )


def days_since(history_dates, key, current_date):
    if not history_dates[key]:
        return None

    return (
        pd.Timestamp(current_date)
        - pd.Timestamp(history_dates[key][-1])
    ).days


def matches_in_last_days(history_dates, key, current_date, days):
    current = pd.Timestamp(current_date)

    return sum(
        1
        for d in history_dates[key]
        if 0 < (current - pd.Timestamp(d)).days <= days
    )


def build_context(matches):
    contexts = []

    for season in TARGET_SEASONS:
        season_matches = [
            m for m in matches
            if m["season"] == season
        ]

        if not season_matches:
            continue

        teams = sorted(
            set(m["home_key"] for m in season_matches)
            | set(m["away_key"] for m in season_matches)
        )

        if CLUB_KEY not in teams:
            raise RuntimeError(
                f"Lech not found in sporting source for {season}."
            )

        stats = {
            key: {
                "played": 0,
                "pts": 0,
                "gf": 0,
                "ga": 0,
                "wins": 0,
            }
            for key in teams
        }

        history = defaultdict(lambda: deque(maxlen=5))
        match_dates = defaultdict(list)

        total_lech_matches = sum(
            1
            for m in season_matches
            if CLUB_KEY in (m["home_key"], m["away_key"])
        )

        groups = defaultdict(list)

        # Group by calendar date: no same-day result can leak
        # into another pre-match context.
        for m in season_matches:
            groups[m["date"]].append(m)

        for date in sorted(groups):
            group = groups[date]

            positions = table_positions(stats)
            leader_points = max(
                (s["pts"] for s in stats.values()),
                default=0,
            )

            sorted_points = sorted(
                (s["pts"] for s in stats.values()),
                reverse=True,
            )

            second_points = (
                sorted_points[1]
                if len(sorted_points) > 1
                else 0
            )

            for m in group:
                if m["home_key"] != CLUB_KEY:
                    continue

                opponent = m["away_key"]
                lech = stats[CLUB_KEY]
                opp = stats[opponent]

                lech_pos = (
                    positions[CLUB_KEY]
                    if lech["played"] > 0
                    else None
                )

                opp_pos = (
                    positions[opponent]
                    if opp["played"] > 0
                    else None
                )

                lech_l5_pts, lech_l5_gd = recent_metrics(
                    history,
                    CLUB_KEY,
                )

                opp_l5_pts, opp_l5_gd = recent_metrics(
                    history,
                    opponent,
                )

                games_left_including_current = max(
                    total_lech_matches - lech["played"],
                    1,
                )

                contexts.append(
                    {
                        "season": season,
                        "source_match_date": m["date"],
                        "source_kickoff": m["kickoff"],
                        "opponent_key": opponent,

                        "round_no": lech["played"] + 1,
                        "total_rounds": total_lech_matches,
                        "matches_remaining_after": max(
                            total_lech_matches
                            - lech["played"]
                            - 1,
                            0,
                        ),
                        "games_left_including_current": (
                            games_left_including_current
                        ),
                        "season_progress": round(
                            lech["played"]
                            / max(total_lech_matches, 1),
                            5,
                        ),

                        "lech_position_before": lech_pos,
                        "opponent_position_before": opp_pos,
                        "position_gap": (
                            opp_pos - lech_pos
                            if (
                                lech_pos is not None
                                and opp_pos is not None
                            )
                            else None
                        ),

                        "lech_points_before": lech["pts"],
                        "opponent_points_before": opp["pts"],
                        "points_gap": lech["pts"] - opp["pts"],
                        "points_to_leader": (
                            leader_points - lech["pts"]
                        ),
                        "opponent_points_to_leader": (
                            leader_points - opp["pts"]
                        ),
                        "leader_margin_if_lech_first": (
                            lech["pts"] - second_points
                            if lech_pos == 1
                            else 0
                        ),

                        "lech_matches_played_before": lech["played"],
                        "opponent_matches_played_before": opp["played"],
                        "lech_ppg_before": (
                            lech["pts"] / lech["played"]
                            if lech["played"]
                            else 0.0
                        ),
                        "opponent_ppg_before": (
                            opp["pts"] / opp["played"]
                            if opp["played"]
                            else 0.0
                        ),

                        "lech_last5_points": lech_l5_pts,
                        "opponent_last5_points": opp_l5_pts,
                        "lech_last5_goal_diff": lech_l5_gd,
                        "opponent_last5_goal_diff": opp_l5_gd,

                        "days_since_lech_prev_league_match": (
                            days_since(
                                match_dates,
                                CLUB_KEY,
                                m["date"],
                            )
                        ),
                        "days_since_opponent_prev_league_match": (
                            days_since(
                                match_dates,
                                opponent,
                                m["date"],
                            )
                        ),
                        "lech_matches_last_14d": (
                            matches_in_last_days(
                                match_dates,
                                CLUB_KEY,
                                m["date"],
                                14,
                            )
                        ),
                        "opponent_matches_last_14d": (
                            matches_in_last_days(
                                match_dates,
                                opponent,
                                m["date"],
                                14,
                            )
                        ),

                        "avg_home_odds": m["avg_home_odds"],
                        "avg_draw_odds": m["avg_draw_odds"],
                        "avg_away_odds": m["avg_away_odds"],
                        "market_home_prob": m["market_home_prob"],
                        "market_draw_prob": m["market_draw_prob"],
                        "market_away_prob": m["market_away_prob"],
                    }
                )

            # Apply results after all contexts on the date.
            for m in group:
                hg = m["home_goals"]
                ag = m["away_goals"]

                hp = (
                    3 if hg > ag
                    else 1 if hg == ag
                    else 0
                )
                ap = (
                    3 if ag > hg
                    else 1 if hg == ag
                    else 0
                )

                hs = stats[m["home_key"]]
                aws = stats[m["away_key"]]

                hs["played"] += 1
                hs["pts"] += hp
                hs["gf"] += hg
                hs["ga"] += ag
                hs["wins"] += int(hg > ag)

                aws["played"] += 1
                aws["pts"] += ap
                aws["gf"] += ag
                aws["ga"] += hg
                aws["wins"] += int(ag > hg)

                history[m["home_key"]].append(
                    {
                        "points": hp,
                        "gd": hg - ag,
                    }
                )
                history[m["away_key"]].append(
                    {
                        "points": ap,
                        "gd": ag - hg,
                    }
                )

                match_dates[m["home_key"]].append(m["date"])
                match_dates[m["away_key"]].append(m["date"])

    return contexts


# ---------------------------------------------------------------------
# Match context to attendance
# ---------------------------------------------------------------------

def choose_context(row, contexts):
    same = [
        c
        for c in contexts
        if c["season"] == row["season"]
        and c["opponent_key"] == row["opponent_key"]
    ]

    exact = [
        c
        for c in same
        if abs(
            (
                pd.Timestamp(c["source_match_date"])
                - pd.Timestamp(row["local_date"])
            ).days
        ) <= 1
    ]

    if len(exact) == 1:
        return exact[0], "exact_date"

    # Known historical source anomaly: some 2019/20 attendance rows carry
    # the correct day/month but the previous calendar year.
    same_month_day = [
        c
        for c in same
        if (
            c["source_match_date"].month
            == row["local_date"].month
            and c["source_match_date"].day
            == row["local_date"].day
        )
    ]

    if len(same_month_day) == 1:
        return same_month_day[0], "month_day_repair"

    # Safe final fallback only when the season/opponent fixture is unique.
    if len(same) == 1:
        return same[0], "unique_season_opponent"

    return None, "unmatched"


def join_dataset(attendance, contexts):
    matched = []
    unmatched = []

    for _, row in attendance.iterrows():
        c, match_method = choose_context(
            row,
            contexts,
        )

        if c is None:
            unmatched.append(
                {
                    "historical_match_id": int(row["id"]),
                    "season": row["season"],
                    "stored_match_date": row["match_date"].isoformat(),
                    "opponent": row["opponent"],
                    "opponent_key": row["opponent_key"],
                }
            )
            continue

        # Use sporting-source date when it repaired an obvious stored-date
        # anomaly; retain stored time where possible.
        stored_local = row["match_date"].tz_convert(
            "Europe/Warsaw"
        )

        corrected_local = pd.Timestamp(
            year=c["source_match_date"].year,
            month=c["source_match_date"].month,
            day=c["source_match_date"].day,
            hour=stored_local.hour,
            minute=stored_local.minute,
            tz="Europe/Warsaw",
        )

        out = {
            "historical_match_id": int(row["id"]),
            "season": row["season"],
            "match_date": corrected_local.isoformat(),
            "stored_match_date": row["match_date"].isoformat(),
            "date_match_method": match_method,
            "opponent": row["opponent"],
            "opponent_key": row["opponent_key"],
            "attendance": int(row["attendance"]),
        }

        for key, value in c.items():
            if key not in {
                "season",
                "source_match_date",
                "source_kickoff",
                "opponent_key",
            }:
                out[key] = value

        matched.append(out)

    return matched, unmatched



def capacity_constrained_for_model(match_date):
    """
    Exclude attendance observations that were structurally capped by
    pandemic-era public-attendance rules.

    This is a modelling-quality exclusion, not a legal classification
    of each individual event.

    The window deliberately covers the Lech attendance observations
    between the partial reopening in June 2020 and the end of August 2021.
    """
    ts = pd.Timestamp(match_date)

    if ts.tzinfo is None:
        ts = ts.tz_localize("Europe/Warsaw")
    else:
        ts = ts.tz_convert("Europe/Warsaw")

    start = pd.Timestamp("2020-06-19", tz="Europe/Warsaw")
    end = pd.Timestamp("2021-08-31 23:59:59", tz="Europe/Warsaw")

    return bool(start <= ts <= end)


def main():
    attendance = load_attendance()
    content = download_source()
    matches = parse_source(content)
    contexts = build_context(matches)

    matched, unmatched = join_dataset(
        attendance,
        contexts,
    )

    dataset = pd.DataFrame(matched).sort_values(
        "match_date"
    )

    dataset["capacity_constrained_for_model"] = (
        dataset["match_date"]
        .map(capacity_constrained_for_model)
    )

    excluded_count = int(
        dataset["capacity_constrained_for_model"].sum()
    )

    dataset.to_csv(
        ART / "lech_demand_dataset_v1.csv",
        index=False,
    )

    pd.DataFrame(unmatched).to_csv(
        ART / "lech_demand_unmatched_v1.csv",
        index=False,
    )

    method_counts = (
        dataset["date_match_method"]
        .value_counts()
        .to_dict()
        if not dataset.empty
        else {}
    )

    print("")
    print("=" * 78)
    print("LECH DEMAND DATASET v1")
    print(f"Attendance rows in scope: {len(attendance)}")
    print(f"Matched:                  {len(matched)}")
    print(f"Unmatched:                {len(unmatched)}")
    print(f"Capacity-constrained:     {excluded_count}")
    print(f"Match methods:            {method_counts}")
    print("=" * 78)

    coverage = len(matched) / max(len(attendance), 1)

    if coverage < 0.95:
        raise RuntimeError(
            f"Dataset coverage only {coverage:.1%}. "
            "Inspect lech_demand_unmatched_v1.csv."
        )

    print("SUCCESS")


if __name__ == "__main__":
    main()
