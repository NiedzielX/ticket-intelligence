#!/usr/bin/env python3
import io, json, os, re
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse

import numpy as np
import pandas as pd

import build_lech_demand_dataset_v11 as data_v11
import build_lech_demand_enrichment_v12_fix2 as enrich_v12
import build_live_ticket_features_v01 as live_v01
import run_lech_demand_study_v13 as model_v13

VERSION = "beyond-forecast-v0.1"
EVENT_ID = int(os.environ["EVENT_ID"])
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
TOTAL_MATCHES = int(os.getenv("TOTAL_LEAGUE_MATCHES_PER_TEAM", "34"))
VALIDATE = os.getenv("VALIDATE_BASELINE", "false").lower() == "true"
EXPECTED_MAE = float(os.getenv("EXPECTED_HOLDOUT_MAE", "5321.8"))
MAE_TOLERANCE = float(os.getenv("HOLDOUT_MAE_TOLERANCE", "1.0"))
BASE = Path("lech_demand_artifacts_v1/lech_demand_dataset_v1.csv")
SOURCE = Path("lech_demand_artifacts_v1/POL_source.csv")
OUT = Path(os.getenv("OUTPUT_DIR", "beyond_forecast_artifacts_v01"))
CATEGORICAL = model_v13.CATEGORICAL
NUMERIC = model_v13.CANDIDATES["early_seasonality"]["numeric"]
FEATURES = CATEGORICAL + NUMERIC


def training_data():
    raw = pd.read_csv(BASE)
    df = enrich_v12.add_calendar_features(raw)
    for col, value in {
        "weather_apparent_c": 15.0,
        "weather_humidity_pct": 50.0,
        "weather_rain_mm_hour": 0.0,
        "weather_wind_kmh": 0.0,
        "weather_cloud_pct": 0.0,
    }.items():
        if col not in df:
            df[col] = value
    return model_v13.add_features(df)


def season_for(kickoff):
    local = pd.Timestamp(kickoff).tz_convert("Europe/Warsaw")
    return f"{local.year}/{local.year + 1}" if local.month >= 7 else f"{local.year - 1}/{local.year}"


def current_results(kickoff):
    if not SOURCE.exists():
        raise RuntimeError(f"Missing sporting source: {SOURCE}")
    source = None
    for enc in ("utf-8-sig", "cp1252", "latin1"):
        try:
            source = pd.read_csv(io.StringIO(SOURCE.read_text(encoding=enc)))
            break
        except Exception:
            pass
    if source is None:
        raise RuntimeError("Cannot parse sporting source.")

    season = season_for(kickoff)
    target = pd.Timestamp(kickoff).tz_convert("Europe/Warsaw")
    cutoff = min(target, pd.Timestamp.now(tz="Europe/Warsaw"))
    rows = []
    for _, r in source.iterrows():
        date = pd.to_datetime(r.get("Date"), dayfirst=True, errors="coerce")
        if pd.isna(date) or data_v11.normalize_season(r.get("Season"), date.to_pydatetime()) != season:
            continue
        hg, ag = pd.to_numeric(r.get("HG"), errors="coerce"), pd.to_numeric(r.get("AG"), errors="coerce")
        if pd.isna(hg) or pd.isna(ag):
            continue
        hour, minute = 12, 0
        m = re.search(r"\b(\d{1,2}):(\d{2})\b", str(r.get("Time", "")))
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
        dt = pd.Timestamp(date.year, date.month, date.day, hour, minute, tz="Europe/Warsaw")
        if dt >= cutoff:
            continue
        home, away = str(r.get("Home", "")).strip(), str(r.get("Away", "")).strip()
        if home and away:
            rows.append({
                "date": dt.date(), "kickoff": dt,
                "home": data_v11.team_key(home), "away": data_v11.team_key(away),
                "hg": int(hg), "ag": int(ag),
            })
    if not rows:
        raise RuntimeError(f"No completed {season} league results found.")
    return sorted(rows, key=lambda x: x["kickoff"]), season


def sporting_context(results, opponent, target_kickoff):
    teams = {data_v11.CLUB_KEY, opponent}
    for r in results:
        teams.update((r["home"], r["away"]))
    stats = {t: {"played": 0, "pts": 0, "gf": 0, "ga": 0, "wins": 0} for t in teams}
    recent = defaultdict(lambda: deque(maxlen=5))
    dates = defaultdict(list)
    for r in results:
        hp = 3 if r["hg"] > r["ag"] else 1 if r["hg"] == r["ag"] else 0
        ap = 3 if r["ag"] > r["hg"] else 1 if r["hg"] == r["ag"] else 0
        for key, gf, ga, pts, win in (
            (r["home"], r["hg"], r["ag"], hp, r["hg"] > r["ag"]),
            (r["away"], r["ag"], r["hg"], ap, r["ag"] > r["hg"]),
        ):
            s = stats[key]
            s["played"] += 1
            s["pts"] += pts
            s["gf"] += gf
            s["ga"] += ga
            s["wins"] += int(win)
            recent[key].append({"points": pts, "gd": gf - ga})
            dates[key].append(r["date"])

    lech, opp = stats[data_v11.CLUB_KEY], stats[opponent]
    if not lech["played"] or not opp["played"]:
        raise RuntimeError(f"Insufficient current context: Lech={lech['played']}, opponent={opp['played']}")
    positions = data_v11.table_positions(stats)
    leader = max(x["pts"] for x in stats.values())
    ordered_pts = sorted((x["pts"] for x in stats.values()), reverse=True)
    second = ordered_pts[1] if len(ordered_pts) > 1 else 0
    context_date = min(
        pd.Timestamp(target_kickoff).tz_convert("Europe/Warsaw").date(),
        pd.Timestamp.now(tz="Europe/Warsaw").date(),
    )

    def last5(key):
        games = list(recent[key])
        return sum(x["points"] for x in games), sum(x["gd"] for x in games)

    def days_since(key):
        return 14.0 if not dates[key] else float((pd.Timestamp(context_date) - pd.Timestamp(dates[key][-1])).days)

    def last14(key):
        return sum(1 for d in dates[key] if 0 < (pd.Timestamp(context_date) - pd.Timestamp(d)).days <= 14)

    lp, lg = last5(data_v11.CLUB_KEY)
    op, og = last5(opponent)
    lpos, opos = positions[data_v11.CLUB_KEY], positions[opponent]
    return {
        "round_no": lech["played"] + 1,
        "matches_remaining_after": max(TOTAL_MATCHES - lech["played"] - 1, 0),
        "season_progress": lech["played"] / TOTAL_MATCHES,
        "lech_position_before": lpos,
        "opponent_position_before": opos,
        "position_gap": opos - lpos,
        "lech_points_before": lech["pts"],
        "opponent_points_before": opp["pts"],
        "points_gap": lech["pts"] - opp["pts"],
        "points_to_leader": leader - lech["pts"],
        "opponent_points_to_leader": leader - opp["pts"],
        "leader_margin_if_lech_first": lech["pts"] - second if lpos == 1 else 0,
        "lech_ppg_before": lech["pts"] / lech["played"],
        "opponent_ppg_before": opp["pts"] / opp["played"],
        "lech_last5_points": lp,
        "opponent_last5_points": op,
        "lech_last5_goal_diff": lg,
        "opponent_last5_goal_diff": og,
        "days_since_lech_prev_league_match": min(max(days_since(data_v11.CLUB_KEY), 0), 60),
        "days_since_opponent_prev_league_match": min(max(days_since(opponent), 0), 60),
        "lech_matches_last_14d": last14(data_v11.CLUB_KEY),
        "opponent_matches_last_14d": last14(opponent),
        "current_completed_lech_matches": lech["played"],
        "current_completed_opponent_matches": opp["played"],
        "sporting_context_date": str(context_date),
        "sporting_context_mode": "as_of_run_completed_results",
    }


def attendance_history(target_kickoff):
    cutoff = min(pd.Timestamp(target_kickoff).tz_convert("UTC"), pd.Timestamp.now(tz="UTC"))
    q = parse.urlencode({
        "select": "id,club,opponent,attendance,season,match_date,restricted_capacity",
        "club": f"eq.{data_v11.CLUB}",
        "restricted_capacity": "eq.false",
        "match_date": f"lt.{cutoff.isoformat()}",
        "order": "match_date.asc",
        "limit": "1000",
    })
    rows = live_v01.api_get_all(f"historical_matches?{q}")
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("No attendance history returned.")
    df["match_date"] = pd.to_datetime(df["match_date"], utc=True, errors="raise")
    df["attendance"] = pd.to_numeric(df["attendance"], errors="coerce")
    df = df.dropna(subset=["attendance"]).sort_values("match_date").reset_index(drop=True)
    df["opponent_key"] = df["opponent"].map(data_v11.team_key)
    return df


def future_row(event, context, season):
    hist = attendance_history(event["kickoff_at"])
    values = hist["attendance"].astype(float)
    if len(values) < 10:
        raise RuntimeError(f"Insufficient attendance history: {len(values)}")
    r3 = float(values.tail(3).mean())
    r5 = float(values.tail(5).mean())
    r10 = float(values.tail(10).mean())
    mean = float(values.mean())
    season_values = hist.loc[hist["season"] == season, "attendance"].astype(float)
    season_avg = float(season_values.mean()) if len(season_values) else mean
    opponent = data_v11.team_key(event["away_team"])
    ov = hist.loc[hist["opponent_key"] == opponent, "attendance"].astype(float)
    om = float(ov.mean()) if len(ov) else mean
    orm = float(ov.tail(3).mean()) if len(ov) else om
    cal = enrich_v12.add_calendar_features(pd.DataFrame({"match_date": [event["kickoff_at"]]})).iloc[0]
    row = {
        "opponent_key": opponent,
        "month": int(cal["month"]),
        "weekday": int(cal["weekday"]),
        "kickoff_minutes": int(cal["kickoff_minutes"]),
        "is_weekend": int(int(cal["weekday"]) in (5, 6)),
        "rolling_3": r3,
        "rolling_5": r5,
        "rolling_10": r10,
        "season_avg_so_far": season_avg,
        "recent_attendance_trend": r3 / r10,
        "opponent_draw_ratio": float(np.clip(om / mean, 0.5, 1.8)),
        "opponent_recent_draw_ratio": float(np.clip(orm / mean, 0.5, 1.8)),
        **context,
        "month_sin": float(cal["month_sin"]),
        "month_cos": float(cal["month_cos"]),
        "doy_sin": float(cal["doy_sin"]),
        "doy_cos": float(cal["doy_cos"]),
        "kickoff_sin": float(cal["kickoff_sin"]),
        "kickoff_cos": float(cal["kickoff_cos"]),
        "is_summer_month": int(cal["is_summer_month"]),
        "is_winter_month": int(cal["is_winter_month"]),
        "days_to_nearest_public_holiday": int(cal["days_to_nearest_public_holiday"]),
    }
    row["ppg_gap"] = row["lech_ppg_before"] - row["opponent_ppg_before"]
    row["form_points_gap"] = row["lech_last5_points"] - row["opponent_last5_points"]
    row["form_gd_gap"] = row["lech_last5_goal_diff"] - row["opponent_last5_goal_diff"]
    row["late_season"] = float(np.clip(row["season_progress"], 0, 1) ** 2)
    remaining = max(3.0 * (row["matches_remaining_after"] + 1), 3.0)
    row["title_reachability"] = float(np.clip(1 - row["points_to_leader"] / remaining, 0, 1))
    row["title_pressure"] = row["late_season"] * row["title_reachability"]
    row["top4_match"] = int(row["lech_position_before"] <= 4 and row["opponent_position_before"] <= 4)
    row["late_top4_match"] = row["top4_match"] * row["late_season"]
    frame = pd.DataFrame([row])
    missing = [c for c in FEATURES if c not in frame or pd.isna(frame.iloc[0][c])]
    if missing:
        raise RuntimeError(f"Future feature row invalid: {missing}")
    return frame, hist


def historical_forecast(event):
    train = training_data()
    validation = None
    if VALIDATE:
        _, summary = model_v13.evaluate_candidate(
            train,
            "early_seasonality",
            model_v13.CANDIDATES["early_seasonality"],
        )
        delta = round(summary["holdout_mae"] - EXPECTED_MAE, 1)
        validation = {
            "holdout_mae": summary["holdout_mae"],
            "expected_holdout_mae": EXPECTED_MAE,
            "delta": delta,
        }
        if abs(delta) > MAE_TOLERANCE:
            raise RuntimeError(
                f"Baseline validation failed: MAE={summary['holdout_mae']} expected={EXPECTED_MAE}"
            )
    results, season = current_results(event["kickoff_at"])
    context = sporting_context(results, data_v11.team_key(event["away_team"]), event["kickoff_at"])
    future, hist = future_row(event, context, season)
    interval = model_v13.conformal_radius(train, NUMERIC, alpha=0.20)
    model = model_v13.build_model(NUMERIC)
    model.fit(
        train[FEATURES],
        train["target_residual"],
        model__sample_weight=model_v13.recency_weights(train),
    )
    residual = float(model.predict(future[FEATURES])[0])
    p50 = float(future.iloc[0]["rolling_5"] + residual)
    radius = float(interval["radius"])
    clean_context = {
        k: round(float(v), 4) if isinstance(v, (float, np.floating))
        else int(v) if isinstance(v, (int, np.integer))
        else v
        for k, v in context.items()
    }
    return {
        "status": "available",
        "model": "lech_v13_early_seasonality",
        "training_rows": len(train),
        "training_last_match": str(train["match_date"].max()),
        "context_season": season,
        "sporting_context_as_of": datetime.now(timezone.utc).isoformat(),
        "historical_attendance_rows_available": len(hist),
        "p10": round(max(0, p50 - radius)),
        "p50": round(p50),
        "p90": round(p50 + radius),
        "conformal_radius": round(radius, 1),
        "rolling_5_baseline": round(float(future.iloc[0]["rolling_5"]), 1),
        "residual_prediction": round(residual, 1),
        "validation": validation,
        "context": clean_context,
    }


def live_signal(event):
    context = live_v01.load_snapshot_context(event["id"])
    inventory = live_v01.load_inventory([r["snapshot_id"] for r in context])
    totals, counts = live_v01.aggregate_available(inventory)
    raw = live_v01.build_records(context, totals, counts)
    excluded, anomalies = live_v01.detect_transient_spikes(raw)
    clean = [r for r in raw if r["snapshot_id"] not in excluded]
    latest = live_v01.calculate_features(clean)[-1]
    keys = [
        "snapshot_id", "captured_at", "hours_to_kickoff", "days_to_match",
        "signal_readiness", "history_hours", "sector_count", "available_total",
        "first_available_total", "available_index", "net_removed_since_first",
        "net_removed_since_previous", "inventory_velocity_since_previous",
        "net_removed_6h", "inventory_velocity_6h", "net_removed_24h",
        "inventory_velocity_24h", "inventory_acceleration_6h_vs_24h",
    ]
    return {
        **{k: latest.get(k) for k in keys},
        "raw_snapshot_count": len(raw),
        "clean_snapshot_count": len(clean),
        "excluded_anomaly_count": len(anomalies),
        "inventory_interpretation": "demand_proxy_not_confirmed_sales",
    }


def horizon(hours):
    if hours is None:
        return None
    targets = [720, 336, 168, 72, 48, 24]
    nearest = min(targets, key=lambda x: abs(hours - x))
    return f"T-{nearest}h" if abs(hours - nearest) <= 1 else "continuous"


def main():
    event = live_v01.resolve_ticket_event()
    live = live_signal(event)
    if event.get("competition") == "Ekstraklasa":
        historical = historical_forecast(event)
    else:
        historical = {
            "status": "unavailable_for_competition",
            "reason": "Validated historical baseline is league-only; European baseline is not yet validated.",
        }
    p50 = historical.get("p50") if historical.get("status") == "available" else None
    correction_status = (
        "pending_empirical_calibration"
        if p50 is not None
        else "not_applicable_without_historical_baseline"
    )
    payload = {
        "script_version": VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "horizon": horizon(live.get("hours_to_kickoff")),
        "historical": historical,
        "live": live,
        "correction": {
            "status": correction_status,
            "live_adjustment_applied": 0,
            "production_p50": p50,
            "rule": (
                "Live inventory is recorded for calibration but does not shift P50 "
                "until a correction function is validated on multiple completed events."
            ),
        },
        "forecast": {
            "p10": historical.get("p10"),
            "p50": p50,
            "p90": historical.get("p90"),
            "live_adjustment": 0,
            "status": (
                "historical_baseline_with_live_observation"
                if p50 is not None
                else "live_observation_only"
            ),
        },
        "no_leakage": {
            "future_match_results_used": False,
            "inventory_after_forecast_timestamp_used": False,
            "note": (
                "Sporting context and attendance history use only information available at run time; "
                "future kickoff is used only for known calendar features."
            ),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"event_{EVENT_ID}_beyond_forecast_v01.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Event: {event['home_team']} vs {event['away_team']}")
    print(f"Hours to kickoff: {live.get('hours_to_kickoff')} | live readiness: {live.get('signal_readiness')}")
    print(f"Historical: {historical.get('status')} | P50: {p50} | adjustment: 0")
    print(f"Output: {path}\nSUCCESS")


if __name__ == "__main__":
    main()
