#!/usr/bin/env python3

import asyncio
import json
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from urllib import parse, request

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
CLUB = "Lech Poznań"

# We intentionally start with 2017/18.
# This avoids old league-format/points-halving complications and gives us
# a more comparable modern demand era.
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

ALIASES = {
    "Górnik Z.": "Górnik Zabrze",
    "Lechia": "Lechia Gdańsk",
    "Pogoń": "Pogoń Szczecin",
    "Jagiellonia": "Jagiellonia Białystok",
    "Śląsk": "Śląsk Wrocław",
    "Motor": "Motor Lublin",
    "Radomiak": "Radomiak Radom",
    "Legia": "Legia Warszawa",
    "GKS K.": "GKS Katowice",
    "Widzew": "Widzew Łódź",
    "Raków": "Raków Częstochowa",
    "Zagłębie L.": "Zagłębie Lubin",
    "Stal M.": "FKS Stal Mielec",
    "Korona": "Korona Kielce",
    "Cracovia": "KS Cracovia",
    "Puszcza": "Puszcza Niepołomice",
    "Piast": "Piast Gliwice",
    "Arka": "Arka Gdynia",
    "Wisła Pł.": "Wisła Płock",
    "Termalica": "Bruk-Bet Termalica Nieciecza",
    "Ruch": "Ruch Chorzów",
    "ŁKS": "ŁKS Łódź",
    "Wisła K.": "Wisła Kraków",
    "Podbeskidzie": "Podbeskidzie Bielsko-Biała",
}

TEAM_CANON = {
    "Cracovia": "KS Cracovia",
    "Stal Mielec": "FKS Stal Mielec",
    "Termalica Nieciecza": "Bruk-Bet Termalica Nieciecza",
    "Bruk-Bet Termalica": "Bruk-Bet Termalica Nieciecza",
}


def canon_team(name):
    name = re.sub(r"\s+\([A-Z]+\)$", "", str(name)).strip()
    name = re.sub(r"\s+", " ", name)
    return TEAM_CANON.get(name, name)


def season_slug(season):
    a, b = season.split("/")
    return f"{a}-{b}"


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
        batch = rows[i:i+100]
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
    df["canonical_opponent"] = df["opponent"].map(
        lambda x: ALIASES.get(x, x)
    )
    return df


async def fetch_html(page, url):
    print(f"Fetching {url}")
    response = await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=60000,
    )
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}: {url}")
    await page.wait_for_timeout(500)
    return await page.content()


def parse_schedule(html, season, url):
    soup = BeautifulSoup(html, "html.parser")
    matches = []
    current_round = None
    current_date = None

    for table in soup.select("table.standard_tabelle"):
        for tr in table.select("tr"):
            ths = [
                x.get_text(" ", strip=True)
                for x in tr.find_all("th")
            ]

            if ths:
                joined = " ".join(ths)
                m = re.search(
                    r"(\d+)\.\s*(?:Round|Spieltag)",
                    joined,
                    re.I,
                )
                if m:
                    current_round = int(m.group(1))
                continue

            cells = [
                td.get_text(" ", strip=True)
                for td in tr.find_all("td")
            ]
            if len(cells) < 5:
                continue

            # Find date if present in any early cell.
            row_date = None
            row_time = None

            for cell in cells[:3]:
                dm = re.search(
                    r"(\d{2})/(\d{2})/(\d{4})",
                    cell,
                )
                if dm:
                    row_date = datetime(
                        int(dm.group(3)),
                        int(dm.group(2)),
                        int(dm.group(1)),
                    ).date()

                tm = re.search(
                    r"\b(\d{1,2}):(\d{2})\b",
                    cell,
                )
                if tm:
                    row_time = (
                        int(tm.group(1)),
                        int(tm.group(2)),
                    )

            if row_date:
                current_date = row_date
            if not current_date:
                continue

            # Team cells are the two textual cells around the separator '-'.
            # WorldFootball schedule rows generally look like:
            # date | time | home | - | away | score
            dash_positions = [
                i for i, c in enumerate(cells)
                if c.strip() == "-"
            ]
            if not dash_positions:
                continue

            dash = dash_positions[0]
            if dash < 1 or dash + 1 >= len(cells):
                continue

            home = canon_team(cells[dash - 1])
            away = canon_team(cells[dash + 1])

            if not home or not away:
                continue

            # Score must be read only from cells AFTER the away team.
            # This prevents a kickoff such as 17:30 from being mistaken
            # for a 17-30 match result.
            score_match = None
            for cell in cells[dash + 2:]:
                sm = re.match(
                    r"^\s*(\d+):(\d+)(?:\s|\(|$)",
                    cell,
                )
                if sm:
                    score_match = sm
                    break

            if not score_match:
                continue

            hg = int(score_match.group(1))
            ag = int(score_match.group(2))

            hour, minute = row_time or (12, 0)

            # The site timezone can differ from Poland by an hour.
            # Relative ordering is what matters here.
            kickoff = pd.Timestamp(
                datetime.combine(
                    current_date,
                    datetime.min.time(),
                )
            ).tz_localize("Europe/Warsaw")
            kickoff += pd.Timedelta(
                hours=hour,
                minutes=minute,
            )

            matches.append(
                {
                    "season": season,
                    "round_no": current_round,
                    "kickoff": kickoff,
                    "date": current_date,
                    "home": home,
                    "away": away,
                    "home_goals": hg,
                    "away_goals": ag,
                    "source_url": url,
                }
            )

    # Deduplicate exact matches in case multiple tables were parsed.
    unique = {}
    for m in matches:
        key = (
            m["season"],
            m["date"],
            m["home"],
            m["away"],
        )
        unique[key] = m

    return list(unique.values())


def position_map(stats):
    rows = []
    for team, s in stats.items():
        gd = s["gf"] - s["ga"]
        rows.append(
            (
                team,
                s["pts"],
                gd,
                s["gf"],
            )
        )

    rows.sort(
        key=lambda x: (-x[1], -x[2], -x[3], x[0])
    )

    positions = {}
    last_key = None
    last_rank = 0

    for i, (team, pts, gd, gf) in enumerate(rows, 1):
        key = (pts, gd, gf)
        if key != last_key:
            last_rank = i
            last_key = key
        positions[team] = last_rank

    return positions


def recent_metrics(history, team):
    games = list(history[team])[-5:]
    points = sum(g["points"] for g in games)
    goal_diff = sum(g["gd"] for g in games)
    return points, goal_diff


def create_context_for_season(matches):
    if not matches:
        return []

    teams = sorted(
        set(m["home"] for m in matches)
        | set(m["away"] for m in matches)
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

    total_rounds = max(
        [m["round_no"] or 0 for m in matches]
        or [0]
    )

    # If round labels could not be parsed, use max scheduled games/team.
    scheduled_by_team = defaultdict(int)
    for m in matches:
        scheduled_by_team[m["home"]] += 1
        scheduled_by_team[m["away"]] += 1

    if not total_rounds:
        total_rounds = max(scheduled_by_team.values())

    results = []

    # Simultaneous matches must all see the same pre-kickoff table.
    groups = defaultdict(list)
    for m in matches:
        groups[m["kickoff"]].append(m)

    for kickoff in sorted(groups):
        group = groups[kickoff]
        positions = position_map(stats)
        leader_pts = max((x["pts"] for x in stats.values()), default=0)

        for m in group:
            if m["home"] != CLUB and m["away"] != CLUB:
                continue

            opponent = m["away"] if m["home"] == CLUB else m["home"]

            lech = stats[CLUB]
            opp = stats[opponent]

            lech_l5_pts, lech_l5_gd = recent_metrics(history, CLUB)
            opp_l5_pts, opp_l5_gd = recent_metrics(history, opponent)

            round_no = m["round_no"]
            if round_no is None:
                round_no = max(
                    lech["played"],
                    opp["played"],
                ) + 1

            results.append(
                {
                    "season": m["season"],
                    "date": m["date"],
                    "opponent": opponent,
                    "round_no": int(round_no),
                    "total_rounds": int(total_rounds),
                    "matches_remaining": max(
                        int(total_rounds - lech["played"]),
                        0,
                    ),
                    "season_progress": round(
                        lech["played"]
                        / max(total_rounds, 1),
                        4,
                    ),
                    "lech_position_before": positions.get(CLUB),
                    "opponent_position_before": positions.get(opponent),
                    "position_gap": (
                        positions.get(opponent, 0)
                        - positions.get(CLUB, 0)
                    ),
                    "lech_points_before": lech["pts"],
                    "opponent_points_before": opp["pts"],
                    "points_gap": lech["pts"] - opp["pts"],
                    "points_to_leader": leader_pts - lech["pts"],
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

        # Now update standings after all simultaneous fixtures in the group.
        for m in group:
            hg = m["home_goals"]
            ag = m["away_goals"]

            home_pts = 3 if hg > ag else 1 if hg == ag else 0
            away_pts = 3 if ag > hg else 1 if hg == ag else 0

            hs = stats[m["home"]]
            aws = stats[m["away"]]

            hs["played"] += 1
            hs["pts"] += home_pts
            hs["gf"] += hg
            hs["ga"] += ag

            aws["played"] += 1
            aws["pts"] += away_pts
            aws["gf"] += ag
            aws["ga"] += hg

            history[m["home"]].append(
                {
                    "points": home_pts,
                    "gd": hg - ag,
                }
            )
            history[m["away"]].append(
                {
                    "points": away_pts,
                    "gd": ag - hg,
                }
            )

    return results


def match_context_to_attendance(attendance, contexts):
    matched = []
    unmatched = []

    for _, row in attendance.iterrows():
        if row["season"] not in SEASONS:
            continue

        expected_opp = row["canonical_opponent"]

        candidates = [
            c
            for c in contexts
            if c["season"] == row["season"]
            and c["opponent"] == expected_opp
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
                    "canonical_opponent": expected_opp,
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
                "opponent_matches_played_before": c["opponent_matches_played_before"],
                "lech_ppg_before": c["lech_ppg_before"],
                "opponent_ppg_before": c["opponent_ppg_before"],
                "lech_last5_points": c["lech_last5_points"],
                "opponent_last5_points": c["opponent_last5_points"],
                "lech_last5_goal_diff": c["lech_last5_goal_diff"],
                "opponent_last5_goal_diff": c["opponent_last5_goal_diff"],
                "source": "worldfootball.net",
                "source_url": c["source_url"],
            }
        )

    return matched, unmatched


async def main():
    attendance = load_attendance()

    all_contexts = []
    scrape_summary = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        )
        page = await context.new_page()

        for season in SEASONS:
            slug = season_slug(season)
            url = (
                "https://www.worldfootball.net/all_matches/"
                f"pol-ekstraklasa-{slug}/"
            )

            try:
                html = await fetch_html(page, url)
                matches = parse_schedule(html, season, url)

                if not matches:
                    print(f"WARNING: no completed matches parsed for {season}")
                    scrape_summary.append(
                        {
                            "season": season,
                            "matches": 0,
                            "lech_contexts": 0,
                            "status": "no_matches",
                        }
                    )
                    continue

                contexts = create_context_for_season(matches)
                all_contexts.extend(contexts)

                print(
                    f"{season}: {len(matches)} league results, "
                    f"{len(contexts)} Lech contexts"
                )

                scrape_summary.append(
                    {
                        "season": season,
                        "matches": len(matches),
                        "lech_contexts": len(contexts),
                        "status": "ok",
                    }
                )

            except Exception as exc:
                print(f"WARNING {season}: {exc}")
                scrape_summary.append(
                    {
                        "season": season,
                        "matches": 0,
                        "lech_contexts": 0,
                        "status": f"error: {exc}",
                    }
                )

        await browser.close()

    matched, unmatched = match_context_to_attendance(
        attendance,
        all_contexts,
    )

    if not matched:
        raise RuntimeError(
            "No historical attendance rows matched sporting context."
        )

    supabase_upsert(
        "historical_match_context",
        matched,
        "historical_match_id",
    )

    pd.DataFrame(matched).to_csv(
        ART / "historical_context_matched.csv",
        index=False,
    )

    pd.DataFrame(unmatched).to_csv(
        ART / "historical_context_unmatched.csv",
        index=False,
    )

    pd.DataFrame(scrape_summary).to_csv(
        ART / "scrape_summary.csv",
        index=False,
    )

    print("")
    print("=" * 72)
    print("HISTORICAL CONTEXT BUILD")
    print(f"Matched:   {len(matched)}")
    print(f"Unmatched: {len(unmatched)}")
    print("=" * 72)

    # We allow a handful of unmatched rows due postponed/name edge cases,
    # but a large gap means we should not train.
    if len(matched) < 100:
        raise RuntimeError(
            f"Only {len(matched)} rows matched. "
            "Do not train v0.3 until source parsing is fixed."
        )

    print("SUCCESS")


if __name__ == "__main__":
    asyncio.run(main())
