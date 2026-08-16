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

PREDICT_OPPONENT = os.getenv(
    "PREDICT_OPPONENT",
    "Raków Częstochowa"
)

PREDICT_MATCH_DATE = os.getenv(
    "PREDICT_MATCH_DATE",
    "2026-09-06T17:30:00+02:00"
)

ART = Path("model_artifacts")
ART.mkdir(exist_ok=True)


# ---------------------------------------------------------
# Supabase
# ---------------------------------------------------------

def supabase_get(table, params):

    query = parse.urlencode(
        params,
        doseq=True,
        safe=",.*()"
    )

    url = (
        f"{SUPABASE_URL}/rest/v1/{table}"
        f"?{query}"
    )

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
                "id,club,opponent,attendance,"
                "season,competition,match_date,"
                "restricted_capacity"
            ),
            "club": f"eq.{CLUB}",
            "restricted_capacity": "eq.false",
            "order": "match_date.asc",
            "limit": "1000",
        }
    )

    if not rows:
        raise RuntimeError(
            "No historical matches returned from Supabase."
        )

    df = pd.DataFrame(rows)

    df["match_date"] = pd.to_datetime(
        df["match_date"],
        utc=True
    )

    df["attendance"] = pd.to_numeric(
        df["attendance"],
        errors="raise"
    )

    df = df.sort_values(
        "match_date"
    ).reset_index(drop=True)

    return df


# ---------------------------------------------------------
# Feature engineering
# Every attendance-based feature uses ONLY earlier matches.
# ---------------------------------------------------------

def add_features(df):

    df = df.copy()

    local_dt = df["match_date"].dt.tz_convert(
        "Europe/Warsaw"
    )

    df["year"] = local_dt.dt.year
    df["month"] = local_dt.dt.month
    df["weekday"] = local_dt.dt.weekday

    df["kickoff_minutes"] = (
        local_dt.dt.hour * 60
        + local_dt.dt.minute
    )

    df["days_since_prev_home"] = (
        df["match_date"]
        .diff()
        .dt.total_seconds()
        .div(86400)
    )

    df["prev_attendance"] = (
        df["attendance"]
        .shift(1)
    )

    df["rolling_3"] = (
        df["attendance"]
        .shift(1)
        .rolling(3, min_periods=1)
        .mean()
    )

    df["rolling_5"] = (
        df["attendance"]
        .shift(1)
        .rolling(5, min_periods=1)
        .mean()
    )

    df["rolling_10"] = (
        df["attendance"]
        .shift(1)
        .rolling(10, min_periods=1)
        .mean()
    )

    df["global_prev_mean"] = (
        df["attendance"]
        .shift(1)
        .expanding()
        .mean()
    )

    # Historical attendance against the same opponent,
    # using only previous matches.
    df["opponent_prev_mean"] = (
        df.groupby("opponent")["attendance"]
        .transform(
            lambda s:
                s.shift(1)
                .expanding()
                .mean()
        )
    )

    # Fallback for first ever observed match vs opponent.
    df["opponent_prev_mean"] = (
        df["opponent_prev_mean"]
        .fillna(df["global_prev_mean"])
    )

    # First row cannot have prior-match features.
    df = df.dropna(
        subset=[
            "prev_attendance",
            "rolling_5",
            "global_prev_mean",
        ]
    ).reset_index(drop=True)

    return df


# ---------------------------------------------------------
# Model
# ---------------------------------------------------------

CATEGORICAL = [
    "opponent",
]

NUMERIC = [
    "year",
    "month",
    "weekday",
    "kickoff_minutes",
    "days_since_prev_home",
    "prev_attendance",
    "rolling_3",
    "rolling_5",
    "rolling_10",
    "global_prev_mean",
    "opponent_prev_mean",
]

FEATURES = CATEGORICAL + NUMERIC


def build_model():

    preprocessing = ColumnTransformer(
        transformers=[
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
                "passthrough",
                NUMERIC,
            ),
        ],
        remainder="drop",
    )

    regressor = GradientBoostingRegressor(
        random_state=42,
        n_estimators=250,
        learning_rate=0.03,
        max_depth=2,
        loss="huber",
    )

    return Pipeline(
        steps=[
            ("features", preprocessing),
            ("model", regressor),
        ]
    )


# ---------------------------------------------------------
# Metrics
# ---------------------------------------------------------

def metrics(y_true, y_pred):

    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)

    mae = mean_absolute_error(
        y_true,
        y_pred
    )

    rmse = math.sqrt(
        mean_squared_error(
            y_true,
            y_pred
        )
    )

    mape = np.mean(
        np.abs(
            (y_true - y_pred)
            / y_true
        )
    ) * 100

    return {
        "mae": round(float(mae), 1),
        "rmse": round(float(rmse), 1),
        "mape_pct": round(float(mape), 2),
    }


# ---------------------------------------------------------
# Chronological season backtest
# ---------------------------------------------------------

def backtest(df):

    # Realistic holdout seasons.
    test_seasons = [
        s
        for s in [
            "2024/2025",
            "2025/2026",
            "2026/2027",
        ]
        if s in set(df["season"])
    ]

    predictions = []

    for test_season in test_seasons:

        test_rows = df[
            df["season"] == test_season
        ].copy()

        if test_rows.empty:
            continue

        first_test_date = (
            test_rows["match_date"].min()
        )

        train_rows = df[
            df["match_date"] < first_test_date
        ].copy()

        if len(train_rows) < 80:
            continue

        model = build_model()

        model.fit(
            train_rows[FEATURES],
            train_rows["attendance"],
        )

        model_pred = model.predict(
            test_rows[FEATURES]
        )

        for pos, (_, row) in enumerate(
            test_rows.iterrows()
        ):

            predictions.append(
                {
                    "season": test_season,
                    "match_date": row["match_date"],
                    "opponent": row["opponent"],
                    "actual": float(
                        row["attendance"]
                    ),
                    "historical_mean": float(
                        row["global_prev_mean"]
                    ),
                    "rolling_5": float(
                        row["rolling_5"]
                    ),
                    "opponent_mean": float(
                        row["opponent_prev_mean"]
                    ),
                    "model": float(
                        model_pred[pos]
                    ),
                }
            )

    if not predictions:
        raise RuntimeError(
            "Backtest produced no predictions."
        )

    pred = pd.DataFrame(predictions)

    report = {}

    for column in [
        "historical_mean",
        "rolling_5",
        "opponent_mean",
        "model",
    ]:
        report[column] = metrics(
            pred["actual"],
            pred[column],
        )

    return pred, report


# ---------------------------------------------------------
# Forecast future match
# ---------------------------------------------------------

def future_feature_row(df):

    target = pd.Timestamp(
        PREDICT_MATCH_DATE
    )

    if target.tzinfo is None:
        target = target.tz_localize(
            "Europe/Warsaw"
        )

    target_utc = target.tz_convert("UTC")

    latest = df.iloc[-1]

    prev_att = float(
        latest["attendance"]
    )

    rolling3 = float(
        df["attendance"].tail(3).mean()
    )

    rolling5 = float(
        df["attendance"].tail(5).mean()
    )

    rolling10 = float(
        df["attendance"].tail(10).mean()
    )

    global_mean = float(
        df["attendance"].mean()
    )

    opponent_rows = df[
        df["opponent"] == PREDICT_OPPONENT
    ]

    if len(opponent_rows):
        opponent_mean = float(
            opponent_rows["attendance"].mean()
        )
    else:
        opponent_mean = global_mean

    previous_date = df[
        "match_date"
    ].max()

    days_since = (
        target_utc - previous_date
    ).total_seconds() / 86400

    local_target = target.tz_convert(
        "Europe/Warsaw"
    )

    return pd.DataFrame(
        [
            {
                "opponent": PREDICT_OPPONENT,
                "year": local_target.year,
                "month": local_target.month,
                "weekday": local_target.weekday(),
                "kickoff_minutes": (
                    local_target.hour * 60
                    + local_target.minute
                ),
                "days_since_prev_home": days_since,
                "prev_attendance": prev_att,
                "rolling_3": rolling3,
                "rolling_5": rolling5,
                "rolling_10": rolling10,
                "global_prev_mean": global_mean,
                "opponent_prev_mean": opponent_mean,
            }
        ]
    )


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():

    print(
        f"Loading historical matches for {CLUB}..."
    )

    raw = load_history()

    print(
        f"Valid unrestricted matches: {len(raw)}"
    )

    data = add_features(raw)

    print(
        f"Rows usable for modelling: {len(data)}"
    )

    print("")
    print("Running chronological backtest...")

    predictions, report = backtest(
        data
    )

    print("")
    print("BACKTEST RESULTS")
    print("=" * 68)

    for name, result in report.items():

        print(
            f"{name:20}"
            f" MAE={result['mae']:8.1f}"
            f" RMSE={result['rmse']:8.1f}"
            f" MAPE={result['mape_pct']:6.2f}%"
        )

    print("=" * 68)

    # Save backtest rows.
    predictions.to_csv(
        ART / "backtest_predictions.csv",
        index=False,
    )

    # Train final model on all usable history.
    final_model = build_model()

    final_model.fit(
        data[FEATURES],
        data["attendance"],
    )

    joblib.dump(
        final_model,
        ART / "attendance_model.joblib",
    )

    # Forecast requested future fixture.
    future = future_feature_row(raw)

    forecast = float(
        final_model.predict(
            future[FEATURES]
        )[0]
    )

    rolling5_baseline = float(
        future["rolling_5"].iloc[0]
    )

    opponent_baseline = float(
        future[
            "opponent_prev_mean"
        ].iloc[0]
    )

    print("")
    print("NEXT MATCH FORECAST")
    print("=" * 68)

    print(
        f"Club:              {CLUB}"
    )

    print(
        f"Opponent:          {PREDICT_OPPONENT}"
    )

    print(
        f"Kickoff:           {PREDICT_MATCH_DATE}"
    )

    print(
        f"Rolling-5:         {rolling5_baseline:,.0f}"
    )

    print(
        f"Opponent baseline: {opponent_baseline:,.0f}"
    )

    print(
        f"MODEL A v0.1:      {forecast:,.0f}"
    )

    print("=" * 68)

    output = {
        "club": CLUB,
        "history_rows": int(len(raw)),
        "modelling_rows": int(len(data)),
        "backtest": report,
        "forecast": {
            "opponent": PREDICT_OPPONENT,
            "match_date": PREDICT_MATCH_DATE,
            "rolling_5_baseline": round(
                rolling5_baseline
            ),
            "opponent_baseline": round(
                opponent_baseline
            ),
            "model_a_v01": round(
                forecast
            ),
        },
    }

    (
        ART / "model_report.json"
    ).write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("")
    print("SUCCESS")


if __name__ == "__main__":
    main()
