#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path


ACTIVE_EVENT_MATRIX_PATH = Path(
    os.getenv("ACTIVE_EVENT_MATRIX_PATH", "active_ticket_event_matrix.json")
)
HISTORICAL_DATASET_PATH = Path(
    "lech_demand_artifacts_v1/lech_demand_dataset_v1.csv"
)
HISTORICAL_SOURCE_PATH = Path("lech_demand_artifacts_v1/POL_source.csv")


def run_step(label, script, env=None):
    print(f"--- {label} ---", flush=True)
    completed = subprocess.run(
        [sys.executable, script],
        env=env or os.environ.copy(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")


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

    historical_ready = historical_dataset_ready()
    if needs_historical_dataset(events) and not historical_ready:
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

    for event in events:
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

    print(f"Successful intelligence events: {succeeded}")
    print(f"Failed intelligence events: {failed}")

    if succeeded == 0:
        raise RuntimeError("No active event intelligence cycle succeeded.")

    print("SUCCESS")


if __name__ == "__main__":
    main()
