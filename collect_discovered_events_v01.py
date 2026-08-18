#!/usr/bin/env python3

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


MATRIX_PATH = Path("roboticket_discovery_artifacts_v01/collector_matrix.json")
SUMMARY_PATH = Path("collector_results_v01/collection_summary_v01.json")


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def main():
    if not MATRIX_PATH.exists():
        raise RuntimeError(f"Missing discovery matrix: {MATRIX_PATH}")

    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    if not matrix:
        print("No collectible Roboticket events found.")
        return

    results = []
    failures = []
    skipped = []
    now = datetime.now(timezone.utc)

    for event in matrix:
        event_id = str(event["id"])
        kickoff = parse_timestamp(event.get("kickoff_at"))

        if kickoff is None:
            failures.append(event_id)
            results.append(
                {
                    "event_id": event_id,
                    "status": "failed",
                    "reason": "missing_or_invalid_kickoff",
                }
            )
            continue

        if kickoff.astimezone(timezone.utc) <= now:
            skipped.append(event_id)
            results.append(
                {
                    "event_id": event_id,
                    "status": "skipped",
                    "reason": "kickoff_reached",
                    "kickoff_at": event["kickoff_at"],
                }
            )
            print(
                f"Skipping {event_id}: kickoff already reached "
                f"({event['kickoff_at']})."
            )
            continue

        env = os.environ.copy()
        env.update(
            {
                "EVENT_ID": event_id,
                "EVENT_PROVIDER": event.get("provider") or "roboticket",
                "EVENT_HOME_TEAM": event["home_team"],
                "EVENT_AWAY_TEAM": event["away_team"],
                "EVENT_COMPETITION": event.get("competition") or "",
                "EVENT_MATCH_DATE": event["match_date"],
                "EVENT_KICKOFF_AT": event["kickoff_at"],
                "EVENT_MAPPING_SOURCE": event["mapping_source"],
                "EVENT_MAPPING_CONFIDENCE": event["mapping_confidence"],
                "ROBOTICKET_URL": event["url"],
            }
        )

        print(
            f"\n=== Collecting {event_id}: "
            f"{event['home_team']} vs {event['away_team']} ===",
            flush=True,
        )
        completed = subprocess.run(["python", "collector.py"], env=env)

        result_path = Path(
            f"collector_results_v01/event_{event_id}_collector_result_v01.json"
        )
        if completed.returncode == 0 and result_path.exists():
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            results.append({"event_id": event_id, "status": "success", **payload})
        else:
            failures.append(event_id)
            results.append(
                {
                    "event_id": event_id,
                    "status": "failed",
                    "returncode": completed.returncode,
                }
            )

    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "discovered_event_count": len(matrix),
        "successful_event_count": sum(row["status"] == "success" for row in results),
        "skipped_event_count": len(skipped),
        "failed_event_count": len(failures),
        "events": results,
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nCOLLECTION SUMMARY")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if failures:
        raise RuntimeError(
            "Collection failed for event(s): " + ", ".join(failures)
        )

    print("SUCCESS")


if __name__ == "__main__":
    main()
