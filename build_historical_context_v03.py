#!/usr/bin/env python3

import io
import json
import os
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from urllib import parse, request

import pandas as pd
import requests

SCRIPT_VERSION = "v03-fbref-fix2"
print(f"Historical context builder: {SCRIPT_VERSION}")


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
CLUB = "Lech Poznań"

SEASONS = [
    "2017/2018",
    "2018/2019",
    "2019/2020",
    "2020/2021",
    "2021/2022",
    "2022/2023",
    "2023/2024",
    "2024/2025",
    "2025/2026",
    "2026/2027",
]

ART = Path("model_artifacts_v03")
ART.mkdir(exist_ok=True)


# ---------------------------------------------------------
# Team normalization
# ---------------------------------------------------------

def simplify(value):
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.lower()
    value = value.replace("ł", "l")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_key(name):
    n = simplify(name)

    rules = [
        (["lech poznan", "lech"], "lech"),
        (["legia"], "legia"),
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
        (["zaglebie"], "zaglebie_lubin"),
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
        (["sandecja", "nowy sacz"], "sandecja"),
    ]

    for needles, key in rules:
        if any(x in n for x in needles):
            return key

    return n.replace(" ", "_")


# ---------------------------------------------------------
# Supabase
# ---------------------------------------------------------

def api_headers(json_body=False):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def supabase_get(table, params):
    query = parse.urlencode(params, doseq=True, safe=",.*()")
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = request.Request(url, headers=api_headers(), method="GET")

    with request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def supabase_upsert(table, rows, on_conflict):
    if not rows:
        return

    url = (
        f"{SUPABASE_URL}/rest/v1/{table}"
        f"?on_conflict={parse.quote(on_conflict)}"
    )

    for i in range(0, len(rows), 100):
        batch = rows[i:i + 100]
        body = json.dumps(batch, ensure_ascii=False).encode("utf-8")

        headers = api_headers(json_body=True)
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"

        req = request.Request(
            url,
            data=body,
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

    df = pd.DataFrame(rows)
    df["match_date"] = pd.to_datetime(df["match_date"], utc=True)
    df["local_date"] = (
        df["match_date"]
        .dt.tz_convert("Europe/Warsaw")
        .dt.date
    )
    df["opponent_key"] = df["opponent"].map(team_key)

    return df


# ---------------------------------------------------------
# FBref
# ---------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
)


def fbref_url(season):
    season_slug = season.replace("/", "-")
    return (
        "https://fbref.com/en/comps/36/"
        f"{season_slug}/schedule/"
        f"{season_slug}-Ekstraklasa-Scores-and-Fixtures"
    )


def download_fbref(season):
    url = fbref_url(season)

    # Sports Reference rate-limits aggressive traffic.
    # We perform only one request per season and wait between seasons.
    for attempt in range(3):
        response = SESSION.get(url, timeout=60)

        if response.status_code == 200:
            return response.text, url

        if response.status_code in (429, 500, 502, 503, 504):
            wait = 20 * (attempt + 1)
            print(
                f"FBref {season}: HTTP {response.status_code}; "
                f"retrying in {wait}s"
            )
            time.sleep(wait)
            continue

        print(
            f"FBref direct request failed for {season}: "
            f"HTTP {response.status_code}"
        )
        break

    # Fallback through Jina Reader. It fetches the public page and returns
    # readable Markdown, which avoids depending on WorldFootball.
    reader_url = (
        "https://r.jina.ai/http://fbref.com/en/comps/36/"
        f"{season.replace('/', '-')}/schedule/"
        f"{season.replace('/', '-')}-Ekstraklasa-Scores-and-Fixtures"
    )

    response = requests.get(
        reader_url,
        timeout=90,
        headers={
            "User-Agent": "ticket-intelligence/0.3",
        },
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"FBref failed ({url}) and Jina fallback failed "
            f"with HTTP {response.status_code}"
        )

    return response.text, reader_url


def normalize_columns(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            " ".join(
                str(x)
                for x in col
                if str(x) != "nan"
            ).strip()
            for col in df.columns
        ]

    df.columns = [
        re.sub(r"\s+", " ", str(c)).strip()
        for c in df.columns
    ]

    return df


def choose_column(columns, exact):
    for c in columns:
        if c.lower() == exact.lower():
            return c

    for c in columns:
        if exact.lower() in c.lower():
            return c

    return None


def parse_score(score):
    if score is None or pd.isna(score):
        return None

    text = str(score).strip()

    # FBref normally uses an en dash: 2–1
    m = re.search(r"(\d+)\s*[–—-]\s*(\d+)", text)

    if not m:
        return None

    return int(m.group(1)), int(m.group(2))


def parse_html_schedule(content, season, source_url):
    try:
        tables = pd.read_html(io.StringIO(content))
    except ValueError:
        return None

    schedule = None

    for table in tables:
        table = normalize_columns(table)

        cols = list(table.columns)
        date_col = choose_column(cols, "Date")
        home_col = choose_column(cols, "Home")
        away_col = choose_column(cols, "Away")
        score_col = choose_column(cols, "Score")

        if date_col and home_col and away_col and score_col:
            schedule = table
            break

    if schedule is None:
        return None

    return dataframe_to_matches(schedule, season, source_url)


def parse_markdown_schedule(content, season, source_url):
    lines = content.splitlines()

    for i, line in enumerate(lines):
        if (
            "|" in line
            and "Date" in line
            and "Home" in line
            and "Score" in line
            and "Away" in line
        ):
            header = [x.strip() for x in line.strip().strip("|").split("|")]

            rows = []
            j = i + 1

            # Skip markdown separator.
            if j < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[j]):
                j += 1

            while j < len(lines):
                row_line = lines[j]

                if "|" not in row_line:
                    if rows:
                        break
                    j += 1
                    continue

                cells = [
                    x.strip()
                    for x in row_line.strip().strip("|").split("|")
                ]

                if len(cells) == len(header):
                    rows.append(cells)

                j += 1

            if rows:
                return dataframe_to_matches(
                    pd.DataFrame(rows, columns=header),
                    season,
                    source_url,
                )

    return None


def dataframe_to_matches(df, season, source_url):
    df = normalize_columns(df)
    cols = list(df.columns)

    date_col = choose_column(cols, "Date")
    time_col = choose_column(cols, "Time")
    home_col = choose_column(cols, "Home")
    away_col = choose_column(cols, "Away")
    score_col = choose_column(cols, "Score")
    wk_col = choose_column(cols, "Wk")

    if not all([date_col, home_col, away_col, score_col]):
        return None

    all_fixtures = []
    completed = []

    for _, row in df.iterrows():
        date_value = str(row.get(date_col, "")).strip()

        try:
            date = pd.to_datetime(
                date_value,
                errors="raise",
            ).date()
        except Exception:
            continue

        home = str(row.get(home_col, "")).strip()
        away = str(row.get(away_col, "")).strip()

        if not home or not away or home == "nan" or away == "nan":
            continue

        hour = 12
        minute = 0

        if time_col:
            tm = re.search(
                r"(\d{1,2}):(\d{2})",
                str(row.get(time_col, "")),
            )
            if tm:
                hour = int(tm.group(1))
                minute = int(tm.group(2))

        kickoff = pd.Timestamp(
            datetime(
                date.year,
                date.month,
                date.day,
                hour,
                minute,
            ),
            tz="Europe/Warsaw",
        )

        wk = None
        if wk_col:
            wk_match = re.search(
                r"\d+",
                str(row.get(wk_col, "")),
            )
            if wk_match:
                wk = int(wk_match.group(0))

        base = {
            "season": season,
            "round_no": wk,
            "kickoff": kickoff,
            "date": date,
            "home": home,
            "away": away,
            "home_key": team_key(home),
            "away_key": team_key(away),
            "source_url": source_url,
        }

        all_fixtures.append(base)

        score = parse_score(row.get(score_col))
        if score is None:
            continue

        completed.append(
            {
                **base,
                "home_goals": score[0],
                "away_goals": score[1],
            }
        )

    if not all_fixtures:
        return None

    return {
        "all_fixtures": all_fixtures,
        "completed": completed,
    }


def fetch_season(season):
    content, source_url = download_fbref(season)

    parsed = parse_html_schedule(
        content,
        season,
        source_url,
    )

    if parsed is None:
        parsed = parse_markdown_schedule(
            content,
            season,
            source_url,
        )

    if parsed is None:
        raise RuntimeError(
            f"Could not parse FBref schedule for {season}"
        )

    return parsed


# ---------------------------------------------------------
# Sporting context
# ---------------------------------------------------------

def position_map(stats):
    rows = []

    for key, s in stats.items():
        gd = s["gf"] - s["ga"]
        rows.append(
            (
                key,
                s["pts"],
                gd,
                s["gf"],
            )
        )

    rows.sort(
        key=lambda x: (-x[1], -x[2], -x[3], x[0])
    )

    positions = {}

    for i, (key, _, _, _) in enumerate(rows, 1):
        positions[key] = i

    return positions


def recent_metrics(history, key):
    games = list(history[key])[-5:]

    return (
        sum(g["points"] for g in games),
        sum(g["gd"] for g in games),
    )


def create_context_for_season(parsed):
    completed = parsed["completed"]
    all_fixtures = parsed["all_fixtures"]

    if not completed:
        return []

    teams = sorted(
        set(m["home_key"] for m in all_fixtures)
        | set(m["away_key"] for m in all_fixtures)
    )

    stats = {
        t: {
            "played": 0,
            "pts": 0,
            "gf": 0,
            "ga": 0,
        }
        for t in teams
    }

    history = defaultdict(lambda: deque(maxlen=5))

    lech_total_matches = sum(
        1
        for m in all_fixtures
        if "lech" in (m["home_key"], m["away_key"])
    )

    groups = defaultdict(list)

    for m in completed:
        groups[m["kickoff"]].append(m)

    contexts = []

    for kickoff in sorted(groups):
        group = groups[kickoff]

        positions = position_map(stats)
        leader_points = max(
            (s["pts"] for s in stats.values()),
            default=0,
        )

        for m in group:
            if "lech" not in (m["home_key"], m["away_key"]):
                continue

            opponent_key = (
                m["away_key"]
                if m["home_key"] == "lech"
                else m["home_key"]
            )

            lech = stats["lech"]
            opp = stats[opponent_key]

            lech_l5_pts, lech_l5_gd = recent_metrics(
                history,
                "lech",
            )
            opp_l5_pts, opp_l5_gd = recent_metrics(
                history,
                opponent_key,
            )

            contexts.append(
                {
                    "season": m["season"],
                    "date": m["date"],
                    "opponent_key": opponent_key,
                    "round_no": lech["played"] + 1,
                    "total_rounds": lech_total_matches,
                    "matches_remaining": max(
                        lech_total_matches - lech["played"],
                        0,
                    ),
                    "season_progress": round(
                        lech["played"]
                        / max(lech_total_matches, 1),
                        4,
                    ),
                    "lech_position_before": positions["lech"],
                    "opponent_position_before": positions[opponent_key],
                    "position_gap": (
                        positions[opponent_key]
                        - positions["lech"]
                    ),
                    "lech_points_before": lech["pts"],
                    "opponent_points_before": opp["pts"],
                    "points_gap": lech["pts"] - opp["pts"],
                    "points_to_leader": (
                        leader_points - lech["pts"]
                    ),
                    "lech_matches_played_before": lech["played"],
                    "opponent_matches_played_before": opp["played"],
                    "lech_ppg_before": round(
                        lech["pts"] / max(lech["played"], 1),
                        3,
                    ) if lech["played"] else 0.0,
                    "opponent_ppg_before": round(
                        opp["pts"] / max(opp["played"], 1),
                        3,
                    ) if opp["played"] else 0.0,
                    "lech_last5_points": lech_l5_pts,
                    "opponent_last5_points": opp_l5_pts,
                    "lech_last5_goal_diff": lech_l5_gd,
                    "opponent_last5_goal_diff": opp_l5_gd,
                    "source_url": m["source_url"],
                }
            )

        # Update all matches only AFTER their pre-match context was captured.
        for m in group:
            hg = m["home_goals"]
            ag = m["away_goals"]

            home_pts = 3 if hg > ag else 1 if hg == ag else 0
            away_pts = 3 if ag > hg else 1 if hg == ag else 0

            hs = stats[m["home_key"]]
            aws = stats[m["away_key"]]

            hs["played"] += 1
            hs["pts"] += home_pts
            hs["gf"] += hg
            hs["ga"] += ag

            aws["played"] += 1
            aws["pts"] += away_pts
            aws["gf"] += ag
            aws["ga"] += hg

            history[m["home_key"]].append(
                {
                    "points": home_pts,
                    "gd": hg - ag,
                }
            )

            history[m["away_key"]].append(
                {
                    "points": away_pts,
                    "gd": ag - hg,
                }
            )

    return contexts


def match_context_to_attendance(attendance, contexts):
    matched = []
    unmatched = []

    for _, row in attendance.iterrows():
        if row["season"] not in SEASONS:
            continue

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
                "opponent_last5_goal_diff": c[
                    "opponent_last5_goal_diff"
                ],

                "source": "fbref.com",
                "source_url": c["source_url"],
            }
        )

    return matched, unmatched


def main():
    attendance = load_attendance()

    all_contexts = []
    summary = []

    for idx, season in enumerate(SEASONS):
        print(f"Fetching FBref Ekstraklasa {season}...")

        try:
            parsed = fetch_season(season)

            contexts = create_context_for_season(parsed)
            all_contexts.extend(contexts)

            print(
                f"{season}: "
                f"{len(parsed['all_fixtures'])} fixtures, "
                f"{len(parsed['completed'])} completed, "
                f"{len(contexts)} Lech contexts"
            )

            summary.append(
                {
                    "season": season,
                    "fixtures": len(parsed["all_fixtures"]),
                    "completed": len(parsed["completed"]),
                    "lech_contexts": len(contexts),
                    "status": "ok",
                }
            )

        except Exception as exc:
            print(f"WARNING {season}: {exc}")

            summary.append(
                {
                    "season": season,
                    "fixtures": 0,
                    "completed": 0,
                    "lech_contexts": 0,
                    "status": f"error: {exc}",
                }
            )

        # Stay comfortably below aggressive request rates.
        if idx < len(SEASONS) - 1:
            time.sleep(5)

    matched, unmatched = match_context_to_attendance(
        attendance,
        all_contexts,
    )

    pd.DataFrame(summary).to_csv(
        ART / "scrape_summary.csv",
        index=False,
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
    print(f"Matched:   {len(matched)}")
    print(f"Unmatched: {len(unmatched)}")
    print("=" * 72)

    if len(matched) < 100:
        raise RuntimeError(
            f"Only {len(matched)} rows matched. "
            "Artifact was uploaded for diagnosis; "
            "do not train v0.3 yet."
        )

    supabase_upsert(
        "historical_match_context",
        matched,
        "historical_match_id",
    )

    print("SUCCESS")


if __name__ == "__main__":
    main()
