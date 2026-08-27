#!/usr/bin/env python3

import os

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-key")
os.environ.setdefault("EVENT_ID", "1")

import enrich_forecast_live_quality_v01 as quality


def main():
    payload = {"event": {"id": 1}, "live": {"snapshot_id": 123, "signal_readiness": "data_gap"}}
    feature = {
        "data_gap_detected": True,
        "max_snapshot_gap_hours": 25.9362,
        "snapshot_gap_threshold_hours": 2.5,
        "previous_gap_detected": False,
        "window_6h_actual_hours": 6.1,
        "window_6h_max_snapshot_gap_hours": 25.9362,
        "window_6h_data_gap_detected": True,
        "window_6h_quality_status": "gap_detected",
        "window_24h_actual_hours": 24.2,
        "window_24h_max_snapshot_gap_hours": 25.9362,
        "window_24h_data_gap_detected": True,
        "window_24h_quality_status": "gap_detected",
    }

    enriched = quality.enrich_payload(payload, feature)
    live = enriched["live"]

    assert live["snapshot_id"] == 123
    assert live["signal_readiness"] == "data_gap"
    assert live["data_gap_detected"] is True
    assert live["max_snapshot_gap_hours"] == 25.9362
    assert live["window_6h_quality_status"] == "gap_detected"
    assert live["window_24h_quality_status"] == "gap_detected"
    assert live["quality_metadata_persisted_at"]
    print("SUCCESS")


if __name__ == "__main__":
    main()
