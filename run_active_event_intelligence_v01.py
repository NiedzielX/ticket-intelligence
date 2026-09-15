#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request


ACTIVE_EVENT_MATRIX_PATH = Path(
    os.getenv("ACTIVE_EVENT_MATRIX_PATH", "active_ticket_event_matrix.json")
)
HISTORICAL_DATASET_PATH = Path(
    "lech_demand_artifacts_v1/lech_demand_dataset_v1.csv"
)
HISTORICAL_SOURCE_PATH = Path("lech_demand_artifacts_v1/POL_source.csv")
LIVE_FEATURE_DIR = Path(os.getenv("LIVE_FEATURE_DIR", "live_ticket_artifacts_v01"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
FORECAST_MIN_INTERVAL_MINUTES = float(
    os.getenv("FORECAST_MIN_INTERVAL_MINUTES", "50")
)
MAX_LIVE_SNAPSHOT_AGE_MINUTES = float(
    os.getenv("MAX_LIVE_SNAPSHOT_AGE_MINUTES", "20")
)
FORECAST_MODEL_VERSION = os.getenv("FORECAST_MODEL_VERSION", "beyond-forecast-v0.1")
FORCE_INTELLIGENCE = os.getenv("FORCE_INTELLIGENCE", "false").lower() == "true"


def run_step(label, script, env=None):
    print(f"--- {label} ---", flush=True)
    completed = subprocess.run(
        [sys.executable, script],
        env=env or os.environ.copy(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")


def api_get(path):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase credentials are required for forecast cadence checks.")
    req = request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "[]")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc


def load_active_events(path=ACTIVE_EVENT_MATRIX_PATH):
    if not path.exists():
        raise RuntimeError(f"Missing active event matrix: {path}")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise RuntimeError("Active event matrix must be a JSON array.")
    return rows


def needs_historical_dataset(events):
    return any(event.get("competition") == "Ekstraklasa" for event in events)


def historical_dataset_ready():
    return HISTORICAL_DATASET_PATH.exists() and HISTORICAL_SOURCE_PATH.exists()


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def forecast_due(last_generated_at, now=None, min_interval_minutes=FORECAST_MIN_INTERVAL_MINUTES):
    if last_generated_at is None:
        return True
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise RuntimeError("Forecast cadence reference time must be timezone-aware.")
    generated_at = parse_timestamp(last_generated_at)
    if generated_at is None or generated_at.tzinfo is None:
        raise RuntimeError("Last forecast timestamp must be timezone-aware.")
    age_minutes = (
        reference.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)
    ).total_seconds() / 60.0
    return age_minutes >= min_interval_minutes


def canonical_ticket_event_id(external_event_id):
    query = parse.urlencode(
        {
            "provider": "eq.roboticket",
            "external_event_id": f"eq.{external_event_id}",
            "select": "id",
            "limit": "2",
        }
    )
    rows = api_get(f"ticket_events?{query}")
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected one canonical ticket event for roboticket:{external_event_id}; "
            f"found {len(rows)}."
        )
    return int(rows[0]["id"])


def latest_forecast_generated_at(external_event_id):
    ticket_event_id = canonical_ticket_event_id(external_event_id)
    query = parse.urlencode(
        {
            "ticket_event_id": f"eq.{ticket_event_id}",
            "model_version": f"eq.{FORECAST_MODEL_VERSION}",
            "select": "forecast_generated_at",
            "order": "forecast_generated_at.desc",
            "limit": "1",
        }
    )
    rows = api_get(f"forecast_observations?{query}")
    if not rows:
        return None
    return rows[0].get("forecast_generated_at")


def event_forecast_due(event):
    if FORCE_INTELLIGENCE:
        return True, "forced"
    last_generated_at = latest_forecast_generated_at(event["id"])
    due = forecast_due(last_generated_at)
    return due, last_generated_at or "no_previous_forecast"


def live_feature_path(event_id, directory=LIVE_FEATURE_DIR):
    return directory / f"event_{event_id}_latest_live_ticket_features_v01.json"


def live_snapshot_age_minutes(payload, now=None):
    latest = payload.get("latest") or {}
    captured_at = parse_timestamp(latest.get("captured_at"))
    if captured_at is None or captured_at.tzinfo is None:
        raise RuntimeError("Live feature payload must contain timezone-aware latest captured_at.")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        raise RuntimeError("Freshness reference time must be timezone-aware.")
    return (
        reference.astimezone(timezone.utc) - captured_at.astimezone(timezone.utc)
    ).total_seconds() / 60.0


def assert_fresh_live_features(
    event_id,
    path=None,
    now=None,
    max_age_minutes=MAX_LIVE_SNAPSHOT_AGE_MINUTES,
):
    target = path or live_feature_path(event_id)
    if not target.exists():
        raise RuntimeError(f"Missing live feature payload: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    age_minutes = live_snapshot_age_minutes(payload, now=now)
    if age_minutes < -1:
        raise RuntimeError(
            f"Live snapshot for event {event_id} is {abs(age_minutes):.1f} minutes in the future."
        )
    if age_minutes > max_age_minutes:
        raise RuntimeError(
            f"Live snapshot for event {event_id} is stale: {age_minutes:.1f} minutes old "
            f"(limit {max_age_minutes:.1f})."
        )
    latest = payload.get("latest") or {}
    return {
        "snapshot_id": latest.get("snapshot_id"),
        "captured_at": latest.get("captured_at"),
        "age_minutes": age_minutes,
        "signal_readiness": latest.get("signal_readiness"),
        "data_gap_detected": latest.get("data_gap_detected"),
    }


def event_environment(event, base_env=None):
    env = dict(base_env or os.environ)
    env.update(
        {
            "EVENT_ID": str(event["id"]),
            "EVENT_PROVIDER": "roboticket",
            "EVENT_KICKOFF_AT": str(event.get("kickoff_at") or ""),
            "TOTAL_LEAGUE_MATCHES_PER_TEAM": "34",
            "STADIUM_CAPACITY": "43269",
            "VALIDATE_BASELINE": "false",
            "PERSIST_FORECAST": "true",
            "EXPECTED_HOLDOUT_MAE": "5321.8",
            "HOLDOUT_MAE_TOLERANCE": "50.0",
            "TRANSIENT_JUMP_THRESHOLD": "500",
            "TRANSIENT_RETURN_TOLERANCE": "100",
            "SNAPSHOT_GAP_THRESHOLD_HOURS": "2.5",
            "WINDOW_MAX_OVERSHOOT_HOURS": "2.5",
        }
    )
    return env


def main():
    run_step(
        "Build active canonical event matrix",
        "build_active_ticket_event_matrix_v01.py",
    )
    events = load_active_events()

    print(f"Active events for intelligence cycle: {len(events)}")
    if not events:
        print("No active events. Nothing to forecast.")
        print("SUCCESS")
        return

    due_events = []
    skipped = 0
    for event in events:
        try:
            due, reason = event_forecast_due(event)
        except Exception as exc:
            print(
                f"WARNING cadence check failed for event {event['id']}: {exc}; "
                "running forecast defensively.",
                flush=True,
            )
            due, reason = True, "cadence_check_failed"
        if due:
            due_events.append(event)
            print(f"Forecast due for event {event['id']}: {reason}", flush=True)
        else:
            skipped += 1
            print(f"Forecast not due for event {event['id']}; last={reason}", flush=True)

    if not due_events:
        print(f"Forecasts skipped by cadence guard: {skipped}")
        print("SUCCESS")
        return

    historical_ready = historical_dataset_ready()
    if needs_historical_dataset(due_events) and not historical_ready:
        try:
            run_step(
                "Build leakage-safe historical dataset",
                "build_lech_demand_dataset_v11.py",
            )
            historical_ready = historical_dataset_ready()
        except Exception as exc:
            historical_ready = False
            print(f"WARNING historical dataset unavailable: {exc}", flush=True)

    succeeded = 0
    failed = 0

    for event in due_events:
        event_id = str(event["id"])
        opponent = event.get("away_team") or "unknown opponent"
        competition = event.get("competition") or ""
        env = event_environment(event)

        print(
            f"=== Intelligence event {event_id}: Lech Poznań vs {opponent} ===",
            flush=True,
        )

        try:
            run_step(
                f"Build live ticket features for {event_id}",
                "build_live_ticket_features_v01.py",
                env,
            )
            freshness = assert_fresh_live_features(event_id)
            print(
                "Fresh live source: "
                f"snapshot={freshness['snapshot_id']} "
                f"age_minutes={freshness['age_minutes']:.1f} "
                f"readiness={freshness['signal_readiness']} "
                f"data_gap={freshness['data_gap_detected']}",
                flush=True,
            )

            if competition == "Ekstraklasa" and not historical_ready:
                raise RuntimeError(
                    "Historical dataset unavailable for Ekstraklasa forecast."
                )

            run_step(
                f"Build Beyond Forecast for {event_id}",
                "build_beyond_forecast_v01.py",
                env,
            )
            run_step(
                f"Persist live quality for {event_id}",
                "enrich_forecast_live_quality_v01.py",
                env,
            )
            run_step(
                f"Persist forecast observation for {event_id}",
                "persist_forecast_observation_v01.py",
                env,
            )
            succeeded += 1
        except Exception as exc:
            failed += 1
            print(f"ERROR event {event_id}: {exc}", flush=True)

    print(f"Forecasts skipped by cadence guard: {skipped}")
    print(f"Successful intelligence events: {succeeded}")
    print(f"Failed intelligence events: {failed}")

    if failed:
        raise RuntimeError(f"{failed} active event intelligence cycle(s) failed.")
    if succeeded == 0:
        raise RuntimeError("No due active event intelligence cycle succeeded.")

    print("SUCCESS")


if __name__ == "__main__":
    main()
