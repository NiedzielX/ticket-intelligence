#!/usr/bin/env python3

import json
import math
import os
from pathlib import Path
from urllib import request, parse

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

CLUB = os.getenv("MODEL_CLUB", "Lech Poznań")
PREDICT_OPPONENT = os.getenv("PREDICT_OPPONENT", "Raków Częstochowa")
PREDICT_MATCH_DATE = os.getenv(
    "PREDICT_MATCH_DATE",
    "2026-09-06T17:30:00+02:00",
)

ART = Path("model_artifacts_v02")
ART.mkdir(exist_ok=True)


def supabase_get(table, params):
    query = parse.urlencode(params, doseq=True, safe=",.*()")
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    req = request.Request(
        url,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
        method="GET",
    )
    with request.urlopen(req, timeout=60) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw)


def load_history():
    rows = supabase_get(
        "historical_matches",
        {
            "select": (
                "id,club,opponent,attendance,season,competition,"
                "match_date,restricted_capacity"
            ),
            "club": f"eq.{CLUB}",
            "restricted_capacity": "eq.false",
            "order": "match_date.asc",
            "limit": "1000",
        },
    )

    if not rows:
        raise RuntimeError("No historical matches returned from Supabase.")

    df = pd.DataFrame(rows)
    df["match_date"] = pd.to_datetime(df["match_date"], utc=True)
    df["attendance"] = pd.to_numeric(df["attendance"], errors="raise")
    return df.sort_values("match_date").reset_index(drop=True)


def add_features(df):
    df = df.copy()

    local_dt = df["match_date"].dt.tz_convert("Europe/Warsaw")
    df["month"] = local_dt.dt.month
    df["weekday"] = local_dt.dt.weekday
    df["kickoff_minutes"] = local_dt.dt.hour * 60 + local_dt.dt.minute

    df["days_since_prev_home"] = (
        df["match_date"].diff().dt.total_seconds().div(86400)
    )

    # All attendance-derived predictors use prior matches only.
    prior = df["attendance"].shift(1)

    df["prev_attendance"] = prior
    df["rolling_3"] = prior.rolling(3, min_periods=1).mean()
    df["rolling_5"] = prior.rolling(5, min_periods=1).mean()
    df["rolling_10"] = prior.rolling(10, min_periods=1).mean()
    df["global_prev_mean"] = prior.expanding().mean()

    df["opponent_prev_mean"] = (
        df.groupby("opponent")["attendance"]
        .transform(lambda s: s.shift(1).expanding().mean())
    )
    df["opponent_prev_mean"] = df["opponent_prev_mean"].fillna(
        df["global_prev_mean"]
    )

    # A season-level trend should be handled relative to recent demand,
    # not by asking a tree model to extrapolate a calendar year.
    df["recent_trend"] = df["rolling_3"] - df["rolling_10"]

    df["opponent_draw_ratio"] = (
        df["opponent_prev_mean"]
        / df["global_prev_mean"].replace(0, np.nan)
    ).clip(0.45, 1.90)

    # Simple historical comparator: today's recent baseline adjusted by
    # the opponent's historical draw relative to Lech's historical level.
    df["opponent_scaled_baseline"] = (
        df["rolling_5"] * df["opponent_draw_ratio"]
    )

    # v0.2 learns an adjustment to the rolling-5 baseline rather than
    # trying to predict the full attendance level from scratch.
    df["target_residual"] = df["attendance"] - df["rolling_5"]

    return df.dropna(
        subset=[
            "prev_attendance",
            "rolling_5",
            "global_prev_mean",
            "days_since_prev_home",
            "target_residual",
        ]
    ).reset_index(drop=True)


CATEGORICAL = ["opponent"]

NUMERIC = [
    "month",
    "weekday",
    "kickoff_minutes",
    "days_since_prev_home",
    "prev_attendance",
    "rolling_3",
    "rolling_5",
    "rolling_10",
    "recent_trend",
    "opponent_prev_mean",
    "opponent_draw_ratio",
]

FEATURES = CATEGORICAL + NUMERIC


def build_model():
    preprocessing = ColumnTransformer(
        transformers=[
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CATEGORICAL,
            ),
            ("num", "passthrough", NUMERIC),
        ],
        remainder="drop",
    )

    regressor = GradientBoostingRegressor(
        random_state=42,
        n_estimators=180,
        learning_rate=0.025,
        max_depth=2,
        min_samples_leaf=6,
        loss="huber",
    )

    return Pipeline(
        steps=[
            ("features", preprocessing),
            ("model", regressor),
        ]
    )


def metrics(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    error = y_pred - y_true

    return {
        "mae": round(float(mean_absolute_error(y_true, y_pred)), 1),
        "rmse": round(float(math.sqrt(mean_squared_error(y_true, y_pred))), 1),
        "mape_pct": round(float(np.mean(np.abs(error / y_true)) * 100), 2),
        "bias": round(float(np.mean(error)), 1),
    }


def backtest(df):
    test_seasons = [
        s
        for s in ["2024/2025", "2025/2026", "2026/2027"]
        if s in set(df["season"])
    ]

    rows = []

    for test_season in test_seasons:
        test = df[df["season"] == test_season].copy()
        if test.empty:
            continue

        train = df[df["match_date"] < test["match_date"].min()].copy()
        if len(train) < 80:
            continue

        model = build_model()
        model.fit(train[FEATURES], train["target_residual"])

        residual_pred = model.predict(test[FEATURES])
        final_pred = test["rolling_5"].to_numpy() + residual_pred

        for pos, (_, row) in enumerate(test.iterrows()):
            rows.append(
                {
                    "season": test_season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(row["attendance"]),
                    "rolling_5": float(row["rolling_5"]),
                    "opponent_scaled": float(row["opponent_scaled_baseline"]),
                    "model_v02": float(final_pred[pos]),
                }
            )

    pred = pd.DataFrame(rows)
    if pred.empty:
        raise RuntimeError("Backtest produced no predictions.")

    report = {}
    for col in ["rolling_5", "opponent_scaled", "model_v02"]:
        report[col] = metrics(pred["actual"], pred[col])

    per_season = {}
    for season, group in pred.groupby("season"):
        per_season[season] = {
            col: metrics(group["actual"], group[col])
            for col in ["rolling_5", "opponent_scaled", "model_v02"]
        }

    pred["model_error"] = pred["model_v02"] - pred["actual"]
    pred["model_abs_error"] = pred["model_error"].abs()
    pred["rolling5_abs_error"] = (pred["rolling_5"] - pred["actual"]).abs()

    top_errors = (
        pred.sort_values("model_abs_error", ascending=False)
        .head(10)[
            [
                "season",
                "match_date",
                "opponent",
                "actual",
                "model_v02",
                "model_error",
                "rolling_5",
                "rolling5_abs_error",
            ]
        ]
        .copy()
    )

    return pred, report, per_season, top_errors


def future_feature_row(raw):
    target = pd.Timestamp(PREDICT_MATCH_DATE)
    if target.tzinfo is None:
        target = target.tz_localize("Europe/Warsaw")
    target_utc = target.tz_convert("UTC")
    local_target = target.tz_convert("Europe/Warsaw")

    prev = raw["attendance"]
    rolling3 = float(prev.tail(3).mean())
    rolling5 = float(prev.tail(5).mean())
    rolling10 = float(prev.tail(10).mean())
    global_mean = float(prev.mean())

    opp = raw[raw["opponent"] == PREDICT_OPPONENT]
    opponent_mean = float(opp["attendance"].mean()) if len(opp) else global_mean
    draw_ratio = float(np.clip(opponent_mean / global_mean, 0.45, 1.90))

    days_since = (
        target_utc - raw["match_date"].max()
    ).total_seconds() / 86400

    return pd.DataFrame(
        [
            {
                "opponent": PREDICT_OPPONENT,
                "month": local_target.month,
                "weekday": local_target.weekday(),
                "kickoff_minutes": local_target.hour * 60 + local_target.minute,
                "days_since_prev_home": days_since,
                "prev_attendance": float(prev.iloc[-1]),
                "rolling_3": rolling3,
                "rolling_5": rolling5,
                "rolling_10": rolling10,
                "recent_trend": rolling3 - rolling10,
                "opponent_prev_mean": opponent_mean,
                "opponent_draw_ratio": draw_ratio,
            }
        ]
    )


def main():
    print(f"Loading historical matches for {CLUB}...")
    raw = load_history()
    data = add_features(raw)

    print(f"Valid unrestricted matches: {len(raw)}")
    print(f"Rows usable for modelling: {len(data)}")
    print("")
    print("Running residual-model chronological backtest...")

    pred, report, per_season, top_errors = backtest(data)

    print("")
    print("MODEL A v0.2 — BACKTEST")
    print("=" * 86)
    for name, result in report.items():
        print(
            f"{name:20}"
            f" MAE={result['mae']:8.1f}"
            f" RMSE={result['rmse']:8.1f}"
            f" MAPE={result['mape_pct']:6.2f}%"
            f" BIAS={result['bias']:8.1f}"
        )
    print("=" * 86)

    print("")
    print("PER-SEASON MAE")
    for season, values in per_season.items():
        print(
            f"{season}: "
            f"rolling5={values['rolling_5']['mae']:.1f}, "
            f"scaled_opponent={values['opponent_scaled']['mae']:.1f}, "
            f"model_v02={values['model_v02']['mae']:.1f}"
        )

    print("")
    print("TOP 10 MODEL ERRORS")
    print(top_errors.to_string(index=False))

    # Train final residual model.
    final_model = build_model()
    final_model.fit(data[FEATURES], data["target_residual"])
    joblib.dump(final_model, ART / "attendance_model_v02.joblib")

    future = future_feature_row(raw)
    residual = float(final_model.predict(future[FEATURES])[0])
    rolling5 = float(future["rolling_5"].iloc[0])
    forecast = rolling5 + residual
    scaled_opponent = rolling5 * float(future["opponent_draw_ratio"].iloc[0])

    print("")
    print("NEXT MATCH FORECAST")
    print("=" * 68)
    print(f"Club:                    {CLUB}")
    print(f"Opponent:                {PREDICT_OPPONENT}")
    print(f"Kickoff:                 {PREDICT_MATCH_DATE}")
    print(f"Rolling-5 baseline:       {rolling5:,.0f}")
    print(f"Opponent-scaled baseline: {scaled_opponent:,.0f}")
    print(f"MODEL A v0.2:             {forecast:,.0f}")
    print("=" * 68)

    pred.to_csv(ART / "backtest_predictions_v02.csv", index=False)
    top_errors.to_csv(ART / "top_10_errors_v02.csv", index=False)

    output = {
        "club": CLUB,
        "history_rows": int(len(raw)),
        "modelling_rows": int(len(data)),
        "backtest": report,
        "per_season": per_season,
        "forecast": {
            "opponent": PREDICT_OPPONENT,
            "match_date": PREDICT_MATCH_DATE,
            "rolling_5_baseline": round(rolling5),
            "opponent_scaled_baseline": round(scaled_opponent),
            "model_a_v02": round(forecast),
        },
    }

    (ART / "model_report_v02.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("")
    print("SUCCESS")


if __name__ == "__main__":
    main()
