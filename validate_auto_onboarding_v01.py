#!/usr/bin/env python3

import json
import os
import subprocess
from pathlib import Path


MATRIX_PATH = Path("roboticket_discovery_artifacts_v01/collector_matrix.json")
SUMMARY_PATH = Path("auto_onboarding_validation_v01.json")


def run(command, env):
    print(f"\n>>> {' '.join(command)}", flush=True)
    subprocess.run(command, env=env, check=True)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    matrix = load_json(MATRIX_PATH)
    if not matrix:
        raise RuntimeError("Discovery returned no collectible events.")

    base_env = os.environ.copy()
    league_dataset_ready = False
    results = []

    for event in matrix:
        event_id = str(event["id"])
        competition = event.get("competition") or ""
        env = base_env.copy()
        env.update(
            {
                "EVENT_ID": event_id,
                "EVENT_PROVIDER": event.get("provider") or "roboticket",
                "EVENT_HOME_TEAM": event["home_team"],
                "EVENT_AWAY_TEAM": event["away_team"],
                "EVENT_COMPETITION": competition,
                "EVENT_MATCH_DATE": event["match_date"],
                "EVENT_KICKOFF_AT": event["kickoff_at"],
                "EVENT_MAPPING_SOURCE": event["mapping_source"],
                "EVENT_MAPPING_CONFIDENCE": event["mapping_confidence"],
                "ROBOTICKET_URL": event["url"],
                "TOTAL_LEAGUE_MATCHES_PER_TEAM": "34",
                "STADIUM_CAPACITY": "43269",
                "TRANSIENT_JUMP_THRESHOLD": "500",
                "TRANSIENT_RETURN_TOLERANCE": "100",
                "EXPECTED_HOLDOUT_MAE": "5321.8",
                "HOLDOUT_MAE_TOLERANCE": "50.0",
                "VALIDATE_BASELINE": "true" if competition == "Ekstraklasa" else "false",
            }
        )

        print(
            f"\n=== {event_id} | {event['home_team']} vs {event['away_team']} | "
            f"{competition or 'competition unresolved'} ==="
        )

        run(["python", "collector.py"], env)
        run(["python", "build_live_ticket_features_v01.py"], env)

        if competition == "Ekstraklasa" and not league_dataset_ready:
            run(["python", "build_lech_demand_dataset_v11.py"], env)
            league_dataset_ready = True

        run(["python", "build_beyond_forecast_v01.py"], env)

        collector = load_json(
            f"collector_results_v01/event_{event_id}_collector_result_v01.json"
        )
        live = load_json(
            f"live_ticket_artifacts_v01/event_{event_id}_latest_live_ticket_features_v01.json"
        )
        forecast = load_json(
            f"beyond_forecast_artifacts_v01/event_{event_id}_beyond_forecast_v01.json"
        )

        latest_live = live["latest"]
        forecast_block = forecast["forecast"]
        historical = forecast["historical"]
        results.append(
            {
                "event_id": event_id,
                "ticket_event_id": collector["ticket_event_id"],
                "snapshot_id": collector["snapshot_id"],
                "away_team": collector["away_team"],
                "competition": collector.get("competition"),
                "kickoff_at": collector["kickoff_at"],
                "available_total": collector["available_total"],
                "sector_count": collector["sector_count"],
                "hours_to_kickoff": latest_live.get("hours_to_kickoff"),
                "signal_readiness": latest_live.get("signal_readiness"),
                "historical_status": historical.get("status"),
                "historical_model": historical.get("model"),
                "historical_p50": historical.get("p50"),
                "forecast_status": forecast_block.get("status"),
                "final_p50": forecast_block.get("p50"),
            }
        )

    payload = {
        "status": "success",
        "event_count": len(results),
        "events": results,
        "rules": {
            "discovery": "Roboticket normal first-team events joined to official Lech schedule",
            "snapshot_schedule": "kickoff frozen at capture time",
            "league_baseline": "historical model only when competition is Ekstraklasa",
            "forecast_ledger_write_during_validation": False,
        },
    }
    SUMMARY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nVALIDATION SUMMARY")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("SUCCESS")


if __name__ == "__main__":
    main()
