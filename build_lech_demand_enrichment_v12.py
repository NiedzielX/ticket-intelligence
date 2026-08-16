#!/usr/bin/env python3

import json
import math
import time
from pathlib import Path
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests


SCRIPT_VERSION = "lech-demand-enrichment-v1.2-fix1"
print(f"Lech Demand Enrichment: {SCRIPT_VERSION}")

ART = Path("lech_demand_artifacts_v12")
ART.mkdir(exist_ok=True)

BASE_DATASET = Path("lech_demand_artifacts_v1/lech_demand_dataset_v1.csv")
EUROPE_FILE = Path("lech_europe_matches_2017_2026.csv")
OUTPUT = ART / "lech_demand_enriched_v12.csv"

STADIUM_LAT = 52.39722
STADIUM_LON = 16.85806

WEATHER_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "weather_code",
    "cloud_cover",
    "wind_speed_10m",
    "wind_gusts_10m",
    "is_day",
]

DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_sum",
    "rain_sum",
    "wind_speed_10m_max",
    "wind_gusts_10m_max",
    "sunshine_duration",
]


def add_calendar_features(df):
    out = df.copy()

    local = (
        pd.to_datetime(out["match_date"], utc=True)
        .dt.tz_convert("Europe/Warsaw")
    )

    out["local_match_date"] = local.dt.date.astype(str)
    out["day_of_year"] = local.dt.dayofyear
    out["month"] = local.dt.month
    out["weekday"] = local.dt.weekday
    out["kickoff_minutes"] = local.dt.hour * 60 + local.dt.minute

    out["month_sin"] = np.sin(2 * np.pi * out["month"] / 12.0)
    out["month_cos"] = np.cos(2 * np.pi * out["month"] / 12.0)

    out["doy_sin"] = np.sin(
        2 * np.pi * out["day_of_year"] / 365.25
    )
    out["doy_cos"] = np.cos(
        2 * np.pi * out["day_of_year"] / 365.25
    )

    out["kickoff_sin"] = np.sin(
        2 * np.pi * out["kickoff_minutes"] / 1440.0
    )
    out["kickoff_cos"] = np.cos(
        2 * np.pi * out["kickoff_minutes"] / 1440.0
    )

    out["is_summer_month"] = out["month"].isin([6, 7, 8]).astype(int)
    out["is_winter_month"] = out["month"].isin([12, 1, 2]).astype(int)

    years = sorted(set(local.dt.year.tolist()))

    def easter_sunday(year):
        # Gregorian computus.
        a = year % 19
        b = year // 100
        c = year % 100
        d = b // 4
        e = b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i = c // 4
        k = c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return date(year, month, day)

    def polish_public_holidays(year):
        easter = easter_sunday(year)

        result = {
            date(year, 1, 1),
            date(year, 1, 6),
            easter,
            easter + timedelta(days=1),
            date(year, 5, 1),
            date(year, 5, 3),
            easter + timedelta(days=49),  # Pentecost Sunday
            easter + timedelta(days=60),  # Corpus Christi
            date(year, 8, 15),
            date(year, 11, 1),
            date(year, 11, 11),
            date(year, 12, 25),
            date(year, 12, 26),
        }

        # Christmas Eve became a statutory day off from 2025.
        if year >= 2025:
            result.add(date(year, 12, 24))

        return result

    holiday_dates = sorted(
        {
            h
            for year in years
            for h in polish_public_holidays(year)
        }
    )
    holiday_set = set(holiday_dates)

    def holiday_distance(d):
        if not holiday_dates:
            return 999
        return min(abs((d - h).days) for h in holiday_dates)

    dates = local.dt.date

    out["is_public_holiday"] = [
        int(d in holiday_set) for d in dates
    ]
    out["days_to_nearest_public_holiday"] = [
        holiday_distance(d) for d in dates
    ]
    out["holiday_within_1d"] = (
        out["days_to_nearest_public_holiday"] <= 1
    ).astype(int)
    out["holiday_within_3d"] = (
        out["days_to_nearest_public_holiday"] <= 3
    ).astype(int)

    return out


def add_europe_context(df):
    out = df.copy()

    euro = pd.read_csv(EUROPE_FILE)
    euro["date"] = pd.to_datetime(euro["date"]).dt.date

    by_season = {
        season: sorted(group["date"].tolist())
        for season, group in euro.groupby("season")
    }

    local_dates = pd.to_datetime(
        out["local_match_date"]
    ).dt.date

    rows = []

    for season, match_day in zip(out["season"], local_dates):
        dates = by_season.get(season, [])

        previous = [
            (match_day - d).days
            for d in dates
            if d < match_day
        ]
        following = [
            (d - match_day).days
            for d in dates
            if d > match_day
        ]

        days_since = min(previous) if previous else 99
        days_until = min(following) if following else 99

        prev10 = sum(
            1 for d in dates
            if 0 < (match_day - d).days <= 10
        )
        next10 = sum(
            1 for d in dates
            if 0 < (d - match_day).days <= 10
        )

        rows.append(
            {
                "days_since_europe": min(days_since, 30),
                "days_until_europe": min(days_until, 30),
                "europe_within_3d_before": int(days_since <= 3),
                "europe_within_4d_after": int(days_until <= 4),
                "europe_matches_prev_10d": prev10,
                "europe_matches_next_10d": next10,
                "europe_busy_window": int(
                    days_since <= 4 or days_until <= 4
                ),
                "europe_season_active": int(bool(dates)),
            }
        )

    return pd.concat(
        [out.reset_index(drop=True), pd.DataFrame(rows)],
        axis=1,
    )


def fetch_weather_period(start_date, end_date, label, session=None):
    """
    Fetch a small ERA5 window with retries.

    Full-season ERA5 requests are intentionally avoided because the archive
    endpoint may take too long to build large hourly responses.
    """
    params = {
        "latitude": STADIUM_LAT,
        "longitude": STADIUM_LON,
        "start_date": str(start_date),
        "end_date": str(end_date),
        "hourly": ",".join(HOURLY_VARS),
        "daily": ",".join(DAILY_VARS),
        "timezone": "Europe/Warsaw",
        "models": "era5",
    }

    client = session or requests.Session()

    print(
        f"Fetching ERA5 weather {label}: "
        f"{start_date} -> {end_date}"
    )

    last_error = None

    for attempt in range(1, 5):
        try:
            response = client.get(
                WEATHER_URL,
                params=params,
                timeout=(20, 90),
                headers={
                    "User-Agent": "ticket-intelligence/1.2-fix1"
                },
            )
            response.raise_for_status()
            payload = response.json()

            if (
                "hourly" not in payload
                or "daily" not in payload
            ):
                raise RuntimeError(
                    f"Unexpected Open-Meteo response for {label}: "
                    f"{payload.keys()}"
                )

            safe_label = re.sub(
                r"[^A-Za-z0-9_.-]+",
                "_",
                label,
            )

            (
                ART / f"weather_{safe_label}.json"
            ).write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            hourly = pd.DataFrame(payload["hourly"])
            daily = pd.DataFrame(payload["daily"])

            hourly["time"] = pd.to_datetime(
                hourly["time"]
            )
            daily["time"] = pd.to_datetime(
                daily["time"]
            ).dt.date

            return hourly, daily

        except (
            requests.Timeout,
            requests.ConnectionError,
            requests.HTTPError,
            ValueError,
        ) as exc:
            last_error = exc

            if attempt == 4:
                break

            wait_seconds = [2, 5, 10][attempt - 1]

            print(
                f"  attempt {attempt}/4 failed: {exc}. "
                f"Retrying in {wait_seconds}s..."
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"Open-Meteo failed for {label} "
        f"({start_date} -> {end_date}) after 4 attempts: "
        f"{last_error}"
    )


def build_weather_chunks(match_days, max_span_days=35):
    """
    Group only the dates we actually need.

    A season used to be requested as one ~10-month hourly response.
    Instead, nearby match dates are grouped into short windows capped
    at max_span_days. This dramatically reduces response size.
    """
    days = sorted(set(match_days))

    if not days:
        return []

    chunks = []
    chunk = [days[0]]

    for current in days[1:]:
        proposed_span = (
            pd.Timestamp(current)
            - pd.Timestamp(chunk[0])
        ).days

        # Keep nearby fixtures together, but never create a long ERA5 query.
        gap = (
            pd.Timestamp(current)
            - pd.Timestamp(chunk[-1])
        ).days

        if proposed_span <= max_span_days and gap <= 24:
            chunk.append(current)
        else:
            chunks.append(chunk)
            chunk = [current]

    chunks.append(chunk)

    return chunks


def fetch_weather_chunk_with_fallback(
    chunk,
    season,
    chunk_no,
    session,
):
    start = min(chunk)
    end = max(chunk)

    label = (
        f"{season.replace('/', '_')}_chunk_{chunk_no:02d}"
    )

    try:
        return fetch_weather_period(
            start,
            end,
            label,
            session=session,
        )
    except RuntimeError as exc:
        print(
            f"Chunk request failed permanently: {exc}"
        )
        print(
            "Falling back to exact match-day requests "
            "for this chunk."
        )

    hourly_parts = []
    daily_parts = []

    for day_no, day in enumerate(chunk, start=1):
        day_label = (
            f"{season.replace('/', '_')}_"
            f"chunk_{chunk_no:02d}_day_{day_no:02d}"
        )

        hourly, daily = fetch_weather_period(
            day,
            day,
            day_label,
            session=session,
        )

        hourly_parts.append(hourly)
        daily_parts.append(daily)

    hourly = (
        pd.concat(
            hourly_parts,
            ignore_index=True,
        )
        .drop_duplicates(subset=["time"])
        .sort_values("time")
        .reset_index(drop=True)
    )

    daily = (
        pd.concat(
            daily_parts,
            ignore_index=True,
        )
        .drop_duplicates(subset=["time"])
        .sort_values("time")
        .reset_index(drop=True)
    )

    return hourly, daily

def add_weather(df):
    out = df.copy()

    local = (
        pd.to_datetime(out["match_date"], utc=True)
        .dt.tz_convert("Europe/Warsaw")
        .dt.tz_localize(None)
    )

    out["_local_ts"] = local
    out["_local_day"] = local.dt.date

    enriched = []
    session = requests.Session()

    for season, group in out.groupby("season", sort=True):
        match_days = sorted(
            set(group["_local_day"].tolist())
        )

        chunks = build_weather_chunks(
            match_days,
            max_span_days=35,
        )

        print(
            f"{season}: {len(match_days)} match days "
            f"-> {len(chunks)} weather chunks"
        )

        hourly_parts = []
        daily_parts = []

        for chunk_no, chunk in enumerate(
            chunks,
            start=1,
        ):
            hourly, daily = (
                fetch_weather_chunk_with_fallback(
                    chunk,
                    season,
                    chunk_no,
                    session,
                )
            )

            hourly_parts.append(hourly)
            daily_parts.append(daily)

            # Be polite to the public endpoint.
            time.sleep(0.5)

        hourly = (
            pd.concat(
                hourly_parts,
                ignore_index=True,
            )
            .drop_duplicates(subset=["time"])
            .sort_values("time")
            .reset_index(drop=True)
        )

        daily = (
            pd.concat(
                daily_parts,
                ignore_index=True,
            )
            .drop_duplicates(subset=["time"])
            .sort_values("time")
            .reset_index(drop=True)
        )

        daily_by_date = daily.set_index("time")

        for idx, row in group.iterrows():
            ts = row["_local_ts"]
            same_day = hourly[
                hourly["time"].dt.date == ts.date()
            ].copy()

            if same_day.empty:
                weather = {}
            else:
                nearest_idx = (
                    (same_day["time"] - ts).abs()
                    .idxmin()
                )
                h = same_day.loc[nearest_idx]

                if ts.date() not in daily_by_date.index:
                    weather = {}
                    enriched.append((idx, weather))
                    continue

                d = daily_by_date.loc[ts.date()]

                weather = {
                    "weather_temperature_c": h.get("temperature_2m"),
                    "weather_apparent_c": h.get("apparent_temperature"),
                    "weather_humidity_pct": h.get("relative_humidity_2m"),
                    "weather_precip_mm_hour": h.get("precipitation"),
                    "weather_rain_mm_hour": h.get("rain"),
                    "weather_code": h.get("weather_code"),
                    "weather_cloud_pct": h.get("cloud_cover"),
                    "weather_wind_kmh": h.get("wind_speed_10m"),
                    "weather_gust_kmh": h.get("wind_gusts_10m"),
                    "weather_is_day": h.get("is_day"),
                    "weather_daily_temp_max_c": d.get("temperature_2m_max"),
                    "weather_daily_temp_min_c": d.get("temperature_2m_min"),
                    "weather_daily_precip_mm": d.get("precipitation_sum"),
                    "weather_daily_rain_mm": d.get("rain_sum"),
                    "weather_daily_wind_max_kmh": d.get("wind_speed_10m_max"),
                    "weather_daily_gust_max_kmh": d.get("wind_gusts_10m_max"),
                    "weather_sunshine_hours": (
                        float(d.get("sunshine_duration")) / 3600.0
                        if pd.notna(d.get("sunshine_duration"))
                        else np.nan
                    ),
                }

            enriched.append((idx, weather))

    weather_df = pd.DataFrame(
        {idx: values for idx, values in enriched}
    ).T

    out = out.join(weather_df)
    out = out.drop(columns=["_local_ts", "_local_day"])

    coverage = float(
        out["weather_temperature_c"].notna().mean()
    )

    print(f"Weather coverage: {coverage:.1%}")

    if coverage < 0.95:
        raise RuntimeError(
            f"Weather coverage only {coverage:.1%}."
        )

    out["weather_daily_temp_range_c"] = (
        out["weather_daily_temp_max_c"]
        - out["weather_daily_temp_min_c"]
    )

    return out


def main():
    if not BASE_DATASET.exists():
        raise RuntimeError(
            f"Missing base dataset: {BASE_DATASET}"
        )

    df = pd.read_csv(BASE_DATASET)

    df = add_calendar_features(df)
    df = add_europe_context(df)
    df = add_weather(df)

    df.to_csv(OUTPUT, index=False)

    print("")
    print("=" * 78)
    print("LECH DEMAND ENRICHMENT v1.2")
    print(f"Rows: {len(df)}")
    print(f"Output: {OUTPUT}")
    print("=" * 78)
    print("SUCCESS")


if __name__ == "__main__":
    main()
