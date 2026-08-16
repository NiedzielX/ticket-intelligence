#!/usr/bin/env python3

import json
import math
import os
from pathlib import Path
from urllib import parse, request

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


SCRIPT_VERSION = "v03-football-data-1"
print(f"Attendance trainer: {SCRIPT_VERSION}")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
CLUB = "Lech Poznań"

ART = Path("model_artifacts_v03")
ART.mkdir(exist_ok=True)


def get_rows(table, params):
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
        return json.loads(response.read().decode("utf-8"))


def load_data():
    attendance = pd.DataFrame(
        get_rows(
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
    )

    context = pd.DataFrame(
        get_rows(
            "historical_match_context",
            {
                "select": "*",
                "source": "eq.football-data.co.uk",
                "order": "match_date.asc",
                "limit": "1000",
            },
        )
    )

    if attendance.empty or context.empty:
        raise RuntimeError(
            "Missing historical attendance or Football-Data context."
        )

    attendance["match_date"] = pd.to_datetime(
        attendance["match_date"],
        utc=True,
    )

    context["match_date"] = pd.to_datetime(
        context["match_date"],
        utc=True,
    )

    df = attendance.merge(
        context,
        left_on="id",
        right_on="historical_match_id",
        how="inner",
        suffixes=("", "_ctx"),
    )

    return df.sort_values("match_date").reset_index(drop=True)


CATEGORICAL = ["opponent"]

NUMERIC = [
    "month",
    "weekday",
    "kickoff_minutes",

    "rolling_3",
    "rolling_5",
    "rolling_10",
    "opponent_draw_ratio",

    "round_no",
    "matches_remaining",
    "season_progress",

    "lech_position_before",
    "opponent_position_before",
    "position_gap",

    "lech_points_before",
    "opponent_points_before",
    "points_gap",
    "points_to_leader",

    "lech_ppg_before",
    "opponent_ppg_before",
    "ppg_gap",

    "lech_last5_points",
    "opponent_last5_points",
    "sport_form_gap",

    "lech_last5_goal_diff",
    "opponent_last5_goal_diff",
    "sport_gd_form_gap",
]

FEATURES = CATEGORICAL + NUMERIC


def add_features(df):
    df = df.copy()

    local = df["match_date"].dt.tz_convert("Europe/Warsaw")

    df["month"] = local.dt.month
    df["weekday"] = local.dt.weekday
    df["kickoff_minutes"] = (
        local.dt.hour * 60
        + local.dt.minute
    )

    df["attendance"] = pd.to_numeric(
        df["attendance"],
        errors="coerce",
    )

    prior = df["attendance"].shift(1)

    df["rolling_3"] = (
        prior.rolling(3, min_periods=1).mean()
    )

    df["rolling_5"] = (
        prior.rolling(5, min_periods=1).mean()
    )

    df["rolling_10"] = (
        prior.rolling(10, min_periods=1).mean()
    )

    df["global_prev_mean"] = prior.expanding().mean()

    df["opponent_prev_mean"] = (
        df.groupby("opponent")["attendance"]
        .transform(
            lambda s:
                s.shift(1)
                .expanding()
                .mean()
        )
        .fillna(df["global_prev_mean"])
    )

    df["opponent_draw_ratio"] = (
        df["opponent_prev_mean"]
        / df["global_prev_mean"].replace(0, np.nan)
    ).clip(0.50, 1.80)

    df["opponent_scaled"] = (
        df["rolling_5"]
        * df["opponent_draw_ratio"]
    )

    # PostgreSQL numeric fields can arrive through REST as strings.
    source_numeric = [
        "round_no",
        "matches_remaining",
        "season_progress",
        "lech_position_before",
        "opponent_position_before",
        "position_gap",
        "lech_points_before",
        "opponent_points_before",
        "points_gap",
        "points_to_leader",
        "lech_ppg_before",
        "opponent_ppg_before",
        "lech_last5_points",
        "opponent_last5_points",
        "lech_last5_goal_diff",
        "opponent_last5_goal_diff",
    ]

    for col in source_numeric:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce",
        )

    df["ppg_gap"] = (
        df["lech_ppg_before"]
        - df["opponent_ppg_before"]
    )

    df["sport_form_gap"] = (
        df["lech_last5_points"]
        - df["opponent_last5_points"]
    )

    df["sport_gd_form_gap"] = (
        df["lech_last5_goal_diff"]
        - df["opponent_last5_goal_diff"]
    )

    df["target_residual"] = (
        df["attendance"]
        - df["rolling_5"]
    )

    # First home game of each season has no meaningful pre-season position.
    return df.dropna(
        subset=[
            "attendance",
            "rolling_5",
            "opponent_draw_ratio",
            "lech_position_before",
            "opponent_position_before",
        ]
    ).reset_index(drop=True)


def build_model():
    preprocessing = ColumnTransformer(
        [
            (
                "cat",
                OneHotEncoder(
                    handle_unknown="ignore",
                    sparse_output=False,
                ),
                CATEGORICAL,
            ),
            (
                "num",
                StandardScaler(),
                NUMERIC,
            ),
        ],
        remainder="drop",
    )

    regressor = GradientBoostingRegressor(
        random_state=42,
        n_estimators=160,
        learning_rate=0.025,
        max_depth=2,
        min_samples_leaf=7,
        loss="huber",
    )

    return Pipeline(
        [
            ("features", preprocessing),
            ("model", regressor),
        ]
    )


def metrics(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)

    error = y_pred - y_true

    return {
        "mae": round(
            float(mean_absolute_error(y_true, y_pred)),
            1,
        ),
        "rmse": round(
            float(
                math.sqrt(
                    mean_squared_error(y_true, y_pred)
                )
            ),
            1,
        ),
        "mape_pct": round(
            float(
                np.mean(
                    np.abs(error / y_true)
                ) * 100
            ),
            2,
        ),
        "bias": round(float(np.mean(error)), 1),
    }


def backtest(df):
    test_seasons = [
        season
        for season in ["2024/2025", "2025/2026"]
        if season in set(df["season"])
    ]

    rows = []

    for season in test_seasons:
        test = df[df["season"] == season].copy()

        if test.empty:
            continue

        train = df[
            df["match_date"] < test["match_date"].min()
        ].copy()

        if len(train) < 70:
            raise RuntimeError(
                f"Only {len(train)} training rows before {season}. "
                "Need at least 70."
            )

        model = build_model()

        model.fit(
            train[FEATURES],
            train["target_residual"],
        )

        residual_pred = model.predict(
            test[FEATURES]
        )

        model_pred = (
            test["rolling_5"].to_numpy()
            + residual_pred
        )

        for pos, (_, row) in enumerate(test.iterrows()):
            rows.append(
                {
                    "season": season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(row["attendance"]),
                    "rolling_5": float(row["rolling_5"]),
                    "opponent_scaled": float(row["opponent_scaled"]),
                    "model_v03": float(model_pred[pos]),

                    "lech_position_before": int(
                        row["lech_position_before"]
                    ),
                    "opponent_position_before": int(
                        row["opponent_position_before"]
                    ),
                    "points_to_leader": int(
                        row["points_to_leader"]
                    ),
                    "matches_remaining": int(
                        row["matches_remaining"]
                    ),
                }
            )

    pred = pd.DataFrame(rows)

    if pred.empty:
        raise RuntimeError(
            "No v0.3 backtest predictions."
        )

    report = {
        col: metrics(
            pred["actual"],
            pred[col],
        )
        for col in [
            "rolling_5",
            "opponent_scaled",
            "model_v03",
        ]
    }

    per_season = {}

    for season, group in pred.groupby("season"):
        per_season[season] = {
            col: metrics(
                group["actual"],
                group[col],
            )
            for col in [
                "rolling_5",
                "opponent_scaled",
                "model_v03",
            ]
        }

    pred["model_error"] = (
        pred["model_v03"]
        - pred["actual"]
    )

    pred["model_abs_error"] = (
        pred["model_error"].abs()
    )

    top_errors = (
        pred.sort_values(
            "model_abs_error",
            ascending=False,
        )
        .head(10)
        .copy()
    )

    return pred, report, per_season, top_errors


def main():
    raw = load_data()
    data = add_features(raw)

    print(f"Context-enriched rows: {len(raw)}")
    print(f"Rows usable for modelling: {len(data)}")

    pred, report, per_season, top_errors = backtest(
        data
    )

    print("")
    print("MODEL A v0.3 — SPORTING CONTEXT BACKTEST")
    print("=" * 90)

    for name, result in report.items():
        print(
            f"{name:20}"
            f" MAE={result['mae']:8.1f}"
            f" RMSE={result['rmse']:8.1f}"
            f" MAPE={result['mape_pct']:6.2f}%"
            f" BIAS={result['bias']:8.1f}"
        )

    print("=" * 90)

    print("")
    print("PER-SEASON MAE")

    for season, values in per_season.items():
        print(
            f"{season}: "
            f"rolling5={values['rolling_5']['mae']:.1f}, "
            f"opponent_scaled={values['opponent_scaled']['mae']:.1f}, "
            f"model_v03={values['model_v03']['mae']:.1f}"
        )

    print("")
    print("TOP 10 ERRORS")
    print(top_errors.to_string(index=False))

    pred.to_csv(
        ART / "backtest_predictions_v03.csv",
        index=False,
    )

    top_errors.to_csv(
        ART / "top_10_errors_v03.csv",
        index=False,
    )

    report_payload = {
        "club": CLUB,
        "context_rows": int(len(raw)),
        "model_rows": int(len(data)),
        "backtest": report,
        "per_season": per_season,
        "source": "football-data.co.uk",
    }

    (
        ART / "model_report_v03.json"
    ).write_text(
        json.dumps(
            report_payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    final_model = build_model()

    final_model.fit(
        data[FEATURES],
        data["target_residual"],
    )

    joblib.dump(
        final_model,
        ART / "attendance_model_v03.joblib",
    )

    print("")
    print("SUCCESS")


if __name__ == "__main__":
    main()
