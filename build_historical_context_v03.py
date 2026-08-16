#!/usr/bin/env python3

import io
import json
import os
import re
import unicodedata
from collections import defaultdict, deque
from pathlib import Path
from urllib import parse, request

import pandas as pd
import requests


SCRIPT_VERSION = "v03-football-data-1"
print(f"Historical context builder: {SCRIPT_VERSION}")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

CLUB = "Lech Poznań"
CLUB_KEY = "lech"

# Deliberately stop at 2025/26.
# 2026/27 is still in progress and should not be used for this backtest.
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

ART = Path("model_artifacts_v03")
ART.mkdir(exist_ok=True)


# ---------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------

def simplify(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower().replace("ł", "l")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_key(name):
    n = simplify(name)

    rules = [
        (["lech poznan"], "lech"),
        (["legia warsaw", "legia warszawa", "legia"], "legia"),
        (["rakow"], "rakow"),
        (["jagiellonia"], "jagiellonia"),
        (["pogon"], "pogon"),
        (["gornik zabrze", "gornik z"], "gornik_zabrze"),
        (["lechia"], "lechia"),
        (["slask"], "slask"),
        (["motor"], "motor"),
        (["radomiak"], "radomiak"),
        (["gks katowice", "katowice"], "gks_katowice"),
        (["widzew"], "widzew"),
        (["zaglebie lubin", "zaglebie"], "zaglebie_lubin"),
        (["stal mielec", "stal m"], "stal_mielec"),
        (["korona"], "korona"),
        (["cracovia"], "cracovia"),
        (["puszcza", "niepolomice"], "puszcza"),
        (["piast"], "piast"),
        (["arka"], "arka"),
        (["wisla plock", "wisla p"], "wisla_plock"),
        (["termalica", "nieciecza"], "termalica"),
        (["ruch"], "ruch"),
        (["lks"], "lks"),
        (["wisla krakow", "wisla k"], "wisla_krakow"),
        (["podbeskidzie"], "podbeskidzie"),
        (["warta"], "warta"),
        (["miedz"], "miedz"),
        (["sandecja"], "sandecja"),
    ]

    for needles, key in rules:
        if any(x in n for x in needles):
            return key

    return n.replace(" ", "_")


def normalize_season(value, match_date):
    if value is not None and not pd.isna(value):
        s = str(value).strip()

        m = re.match(r"^(\d{4})[/-](\d{2,4})$", s)
        if m:
            start = int(m.group(1))
            end_raw = m.group(2)
            end = int(end_raw) if len(end_raw) == 4 else 2000 + int(end_raw)
            return f"{start:04d}/{end:04d}"

        if re.match(r"^\d{4}$", s):
            start = int(s)
            return f"{start:04d}/{start + 1:04d}"

    year = match_date.year
    if match_date.month >= 7:
        return f"{year:04d}/{year + 1:04d}"
    return f"{year - 1:04d}/{year:04d}"


def find_col(columns, *candidates):
    lookup = {simplify(c): c for c in columns}

    for candidate in candidates:
        key = simplify(candidate)
        if key in lookup:
            return lookup[key]

    return None


# ---------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------

def api_headers(json_body=False):
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


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


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return

    url = (
        f"{SUPABASE_URL}/rest/v1/{table}"
        f"?on_conflict={parse.quote(on_conflict)}"
    )

    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        payload = json.dumps(batch, ensure_ascii=False).encode("utf-8")

        headers = api_headers(json_body=True)
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

        req = request.Request(
            url,
            data=payload,
            headers=headers,
            method="POST",
        )

        with request.urlopen(req, timeout=60) as response:
            response.read()


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
        raise RuntimeError("No historical_matches rows returned from Supabase.")

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

    return df[df["season"].isin(TARGET_SEASONS)].copy()


# ---------------------------------------------------------------------
# Football-Data CSV
# ---------------------------------------------------------------------

def download_source():
    print(f"Downloading {SOURCE_URL}")

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

    if len(response.content) < 500:
        raise RuntimeError(
            f"Football-Data response is unexpectedly small: "
            f"{len(response.content)} bytes"
        )

    raw_path = ART / "POL_source.csv"
    raw_path.write_bytes(response.content)

    print(f"Downloaded {len(response.content):,} bytes")

    return response.content


def parse_source(content):
    # Football-Data files have historically used Windows encodings in
    # some datasets. Try UTF-8 first, then cp1252.
    last_error = None

    for encoding in ("utf-8-sig", "cp1252", "latin1"):
        try:
            text = content.decode(encoding)
            df = pd.read_csv(io.StringIO(text))
            break
        except Exception as exc:
            last_error = exc
    else:
        raise RuntimeError(f"Could not read POL.csv: {last_error}")

    df.columns = [str(c).strip() for c in df.columns]

    print(f"Source rows: {len(df)}")
    print(f"Source columns: {df.columns.tolist()}")

    date_col = find_col(df.columns, "Date")
    time_col = find_col(df.columns, "Time")
    season_col = find_col(df.columns, "Season")

    home_col = find_col(
        df.columns,
        "Home",
        "HomeTeam",
        "Team 1",
        "Team1",
    )

    away_col = find_col(
        df.columns,
        "Away",
        "AwayTeam",
        "Team 2",
        "Team2",
    )

    hg_col = find_col(
        df.columns,
        "HG",
        "FTHG",
        "HomeGoals",
    )

    ag_col = find_col(
        df.columns,
        "AG",
        "FTAG",
        "AwayGoals",
    )

    ft_col = find_col(
        df.columns,
        "FT",
        "Score",
        "Result",
    )

    required = {
        "date": date_col,
        "home": home_col,
        "away": away_col,
    }

    missing = [
        name
        for name, col in required.items()
        if col is None
    ]

    if missing:
        raise RuntimeError(
            f"Unsupported POL.csv schema. Missing {missing}. "
            f"Columns were: {df.columns.tolist()}"
        )

    if (hg_col is None or ag_col is None) and ft_col is None:
        raise RuntimeError(
            "Unsupported POL.csv schema: no goal columns and no FT score column. "
            f"Columns were: {df.columns.tolist()}"
        )

    matches = []

    for _, row in df.iterrows():
        raw_date = row.get(date_col)

        if pd.isna(raw_date):
            continue

        # Handles DD/MM/YYYY as well as textual dates.
        date = pd.to_datetime(
            raw_date,
            dayfirst=True,
            errors="coerce",
        )

        if pd.isna(date):
            continue

        date = date.to_pydatetime()

        season = normalize_season(
            row.get(season_col) if season_col else None,
            date,
        )

        if season not in TARGET_SEASONS:
            continue

        home = str(row.get(home_col, "")).strip()
        away = str(row.get(away_col, "")).strip()

        if not home or not away or home == "nan" or away == "nan":
            continue

        if hg_col is not None and ag_col is not None:
            hg = pd.to_numeric(row.get(hg_col), errors="coerce")
            ag = pd.to_numeric(row.get(ag_col), errors="coerce")

            if pd.isna(hg) or pd.isna(ag):
                continue

            hg = int(hg)
            ag = int(ag)

        else:
            score = str(row.get(ft_col, "")).strip()
            m = re.search(r"(\d+)\s*[-:]\s*(\d+)", score)

            if not m:
                continue

            hg = int(m.group(1))
            ag = int(m.group(2))

        hour = 12
        minute = 0
        has_time = False

        if time_col is not None:
            tm = re.search(
                r"\b(\d{1,2}):(\d{2})\b",
                str(row.get(time_col, "")),
            )

            if tm:
                hour = int(tm.group(1))
                minute = int(tm.group(2))
                has_time = True

        kickoff = pd.Timestamp(
            year=date.year,
            month=date.month,
            day=date.day,
            hour=hour,
            minute=minute,
            tz="Europe/Warsaw",
        )

        matches.append(
            {
                "season": season,
                "date": date.date(),
                "kickoff": kickoff,
                "has_time": has_time,
                "home": home,
                "away": away,
                "home_key": team_key(home),
                "away_key": team_key(away),
                "home_goals": hg,
                "away_goals": ag,
            }
        )

    if not matches:
        raise RuntimeError(
            "POL.csv was read, but no target Ekstraklasa matches were parsed."
        )

    parsed = pd.DataFrame(matches)

    print("")
    print("PARSED SOURCE BY SEASON")
    print(
        parsed.groupby("season")
        .size()
        .to_string()
    )

    missing_seasons = [
        season
        for season in TARGET_SEASONS
        if season not in set(parsed["season"])
    ]

    if missing_seasons:
        raise RuntimeError(
            f"POL.csv does not contain required seasons: {missing_seasons}. "
            f"Found: {sorted(parsed['season'].unique())}"
        )

    return matches


# ---------------------------------------------------------------------
# Reconstruct table before every Lech HOME match
# ---------------------------------------------------------------------

def base_positions(stats):
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
            -x[1],   # points
            -x[2],   # goal difference
            -x[3],   # goals scored
            -x[4],   # wins
            x[0],
        )
    )

    return {
        key: index
        for index, (key, *_rest) in enumerate(rows, start=1)
    }


def split_positions(stats, split_groups):
    # 2017/18–2019/20 used championship/relegation groups after
    # the regular phase. Once groups are frozen, rank within each group.
    result = {}

    for group_name, offset in (("top", 0), ("bottom", 8)):
        keys = [
            key
            for key, group in split_groups.items()
            if group == group_name
        ]

        keys.sort(
            key=lambda key: (
                -stats[key]["pts"],
                -(stats[key]["gf"] - stats[key]["ga"]),
                -stats[key]["gf"],
                -stats[key]["wins"],
                key,
            )
        )

        for i, key in enumerate(keys, start=1):
            result[key] = offset + i

    return result


def recent_metrics(history, key):
    games = list(history[key])[-5:]

    return (
        sum(g["points"] for g in games),
        sum(g["gd"] for g in games),
    )


def build_context(matches):
    all_context = []

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
                f"Lech not found in Football-Data season {season}. "
                f"Teams: {teams}"
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

        total_lech_matches = sum(
            1
            for m in season_matches
            if CLUB_KEY in (m["home_key"], m["away_key"])
        )

        if total_lech_matches < 29:
            raise RuntimeError(
                f"Season {season} appears incomplete: "
                f"only {total_lech_matches} Lech league matches."
            )

        # Group by calendar date deliberately. That is conservative:
        # no result from an earlier kickoff on the same day can leak into
        # the pre-match table of a later kickoff.
        groups = defaultdict(list)

        for m in season_matches:
            groups[m["date"]].append(m)

        split_groups = None

        for group_key in sorted(groups):
            group = groups[group_key]

            # Freeze 2017/18-2019/20 top/bottom groups once regular phase
            # (30 matches per club) is complete.
            if (
                season in {"2017/2018", "2018/2019", "2019/2020"}
                and split_groups is None
                and min(s["played"] for s in stats.values()) >= 30
            ):
                regular_positions = base_positions(stats)
                split_groups = {
                    team: ("top" if regular_positions[team] <= 8 else "bottom")
                    for team in stats
                }

            positions = (
                split_positions(stats, split_groups)
                if split_groups
                else base_positions(stats)
            )

            leader_points = max(
                (s["pts"] for s in stats.values()),
                default=0,
            )

            for m in group:
                # Attendance dataset contains Lech HOME games.
                if m["home_key"] != CLUB_KEY:
                    continue

                opponent = m["away_key"]
                lech = stats[CLUB_KEY]
                opp = stats[opponent]

                # Before the club has played any match, "position" is not
                # meaningful. Null it rather than inventing a ranking.
                lech_position = (
                    positions[CLUB_KEY]
                    if lech["played"] > 0
                    else None
                )

                opponent_position = (
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

                all_context.append(
                    {
                        "season": season,
                        "date": m["date"],
                        "opponent_key": opponent,

                        "round_no": lech["played"] + 1,
                        "total_rounds": total_lech_matches,
                        "matches_remaining": max(
                            total_lech_matches - lech["played"],
                            0,
                        ),
                        "season_progress": round(
                            lech["played"] / total_lech_matches,
                            4,
                        ),

                        "lech_position_before": lech_position,
                        "opponent_position_before": opponent_position,
                        "position_gap": (
                            opponent_position - lech_position
                            if (
                                lech_position is not None
                                and opponent_position is not None
                            )
                            else None
                        ),

                        "lech_points_before": lech["pts"],
                        "opponent_points_before": opp["pts"],
                        "points_gap": lech["pts"] - opp["pts"],
                        "points_to_leader": leader_points - lech["pts"],

                        "lech_matches_played_before": lech["played"],
                        "opponent_matches_played_before": opp["played"],

                        "lech_ppg_before": (
                            round(lech["pts"] / lech["played"], 3)
                            if lech["played"]
                            else 0.0
                        ),

                        "opponent_ppg_before": (
                            round(opp["pts"] / opp["played"], 3)
                            if opp["played"]
                            else 0.0
                        ),

                        "lech_last5_points": lech_l5_pts,
                        "opponent_last5_points": opp_l5_pts,
                        "lech_last5_goal_diff": lech_l5_gd,
                        "opponent_last5_goal_diff": opp_l5_gd,
                    }
                )

            # Apply results only after all pre-match contexts for this group.
            for m in group:
                hg = m["home_goals"]
                ag = m["away_goals"]

                home_points = 3 if hg > ag else 1 if hg == ag else 0
                away_points = 3 if ag > hg else 1 if hg == ag else 0

                hs = stats[m["home_key"]]
                aws = stats[m["away_key"]]

                hs["played"] += 1
                hs["pts"] += home_points
                hs["gf"] += hg
                hs["ga"] += ag
                hs["wins"] += int(hg > ag)

                aws["played"] += 1
                aws["pts"] += away_points
                aws["gf"] += ag
                aws["ga"] += hg
                aws["wins"] += int(ag > hg)

                history[m["home_key"]].append(
                    {
                        "points": home_points,
                        "gd": hg - ag,
                    }
                )

                history[m["away_key"]].append(
                    {
                        "points": away_points,
                        "gd": ag - hg,
                    }
                )

        print(
            f"{season}: {len(season_matches)} league matches, "
            f"{sum(1 for c in all_context if c['season'] == season)} "
            f"Lech home contexts"
        )

    return all_context


# ---------------------------------------------------------------------
# Match sporting context to attendance rows
# ---------------------------------------------------------------------

def join_to_attendance(attendance, contexts):
    matched = []
    unmatched = []

    for _, row in attendance.iterrows():
        candidates = [
            c
            for c in contexts
            if c["season"] == row["season"]
            and c["opponent_key"] == row["opponent_key"]
            and abs(
                (
                    pd.Timestamp(c["date"])
                    - pd.Timestamp(row["local_date"])
                ).days
            ) <= 1
        ]

        if len(candidates) != 1:
            unmatched.append(
                {
                    "historical_match_id": int(row["id"]),
                    "season": row["season"],
                    "match_date": row["match_date"].isoformat(),
                    "opponent": row["opponent"],
                    "opponent_key": row["opponent_key"],
                    "candidate_count": len(candidates),
                }
            )
            continue

        c = candidates[0]

        matched.append(
            {
                "historical_match_id": int(row["id"]),
                "season": row["season"],
                "match_date": row["match_date"].isoformat(),
                "opponent": row["opponent"],

                "round_no": c["round_no"],
                "total_rounds": c["total_rounds"],
                "matches_remaining": c["matches_remaining"],
                "season_progress": c["season_progress"],

                "lech_position_before": c["lech_position_before"],
                "opponent_position_before": c["opponent_position_before"],
                "position_gap": c["position_gap"],

                "lech_points_before": c["lech_points_before"],
                "opponent_points_before": c["opponent_points_before"],
                "points_gap": c["points_gap"],
                "points_to_leader": c["points_to_leader"],

                "lech_matches_played_before": c["lech_matches_played_before"],
                "opponent_matches_played_before": c[
                    "opponent_matches_played_before"
                ],

                "lech_ppg_before": c["lech_ppg_before"],
                "opponent_ppg_before": c["opponent_ppg_before"],

                "lech_last5_points": c["lech_last5_points"],
                "opponent_last5_points": c["opponent_last5_points"],
                "lech_last5_goal_diff": c["lech_last5_goal_diff"],
                "opponent_last5_goal_diff": c["opponent_last5_goal_diff"],

                "source": "football-data.co.uk",
                "source_url": SOURCE_URL,
            }
        )

    return matched, unmatched


def main():
    attendance = load_attendance()

    content = download_source()
    matches = parse_source(content)
    contexts = build_context(matches)

    matched, unmatched = join_to_attendance(
        attendance,
        contexts,
    )

    pd.DataFrame(matched).to_csv(
        ART / "historical_context_matched.csv",
        index=False,
    )

    pd.DataFrame(unmatched).to_csv(
        ART / "historical_context_unmatched.csv",
        index=False,
    )

    print("")
    print("=" * 72)
    print("HISTORICAL CONTEXT BUILD")
    print(f"Target attendance rows: {len(attendance)}")
    print(f"Matched:                {len(matched)}")
    print(f"Unmatched:              {len(unmatched)}")
    print("=" * 72)

    # We require high coverage. A few exceptional date/name mismatches are fine,
    # but do not train if source matching is materially incomplete.
    coverage = len(matched) / max(len(attendance), 1)

    if coverage < 0.90:
        raise RuntimeError(
            f"Context coverage only {coverage:.1%}. "
            "See historical_context_unmatched.csv."
        )

    supabase_upsert(
        "historical_match_context",
        matched,
        "historical_match_id",
    )

    print("SUCCESS")


if __name__ == "__main__":
    main()
