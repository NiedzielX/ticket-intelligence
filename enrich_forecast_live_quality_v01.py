#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import build_live_ticket_features_v01 as live_v01


EVENT_ID = int(os.environ["EVENT_ID"])
ARTIFACT_DIR = Path(os.getenv("OUTPUT_DIR", "beyond_forecast_artifacts_v01"))
ARTIFACT_PATH = ARTIFACT_DIR / f"event_{EVENT_ID}_beyond_forecast_v01.json"

QUALITY_KEYS = (
    "data_gap_detected",
    "max_snapshot_gap_hours",
    "snapshot_gap_threshold_hours",
    "previous_gap_detected",
    "window_6h_actual_hours",
    "window_6h_max_snapshot_gap_hours",
    "window_6h_data_gap_detected",
    "window_6h_quality_status",
    "window_24h_actual_hours",
    "window_24h_max_snapshot_gap_hours",
    "window_24h_data_gap_detected",
    "window_24h_quality_status",
)


def enrich_payload(payload, feature_row):
    live = payload.setdefault("live", {})
    for key in QUALITY_KEYS:
        live[key] = feature_row.get(key)
    live["quality_metadata_persisted_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def feature_for_source_snapshot(payload):
    event = payload["event"]
    source_snapshot_id = payload.get("live", {}).get("snapshot_id")
    if source_snapshot_id is None:
        raise RuntimeError("Forecast payload has no live source snapshot id.")

    context = live_v01.load_snapshot_context(int(event["id"]))
    inventory = live_v01.load_inventory([row["snapshot_id"] for row in context])
    totals, counts = live_v01.aggregate_available(inventory)
    raw = live_v01.build_records(context, totals, counts)
    excluded_ids, _ = live_v01.detect_transient_spikes(raw)
    clean = [row for row in raw if row["snapshot_id"] not in excluded_ids]
    features = live_v01.calculate_features(clean)

    matches = [
        row for row in features if int(row["snapshot_id"]) == int(source_snapshot_id)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one live feature row for source snapshot {source_snapshot_id}; "
            f"found {len(matches)}."
        )
    return matches[0]


def main():
    if not ARTIFACT_PATH.exists():
        raise RuntimeError(f"Missing Beyond Forecast artifact: {ARTIFACT_PATH}")

    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    feature_row = feature_for_source_snapshot(payload)
    enrich_payload(payload, feature_row)
    ARTIFACT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        "Persisted live quality metadata in forecast payload: "
        f"snapshot={feature_row['snapshot_id']} "
        f"data_gap={feature_row.get('data_gap_detected')} "
        f"max_gap_hours={feature_row.get('max_snapshot_gap_hours')}"
    )
    print("SUCCESS")


if __name__ == "__main__":
    main()
