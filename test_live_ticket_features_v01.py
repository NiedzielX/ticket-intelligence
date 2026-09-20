#!/usr/bin/env python3

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-key")
os.environ.setdefault("EVENT_ID", "1")
os.environ.setdefault("SNAPSHOT_GAP_THRESHOLD_HOURS", "2.5")
os.environ.setdefault("WINDOW_MAX_OVERSHOOT_HOURS", "2.5")

import build_live_ticket_features_v01 as live


BASE = datetime(2026, 8, 1, tzinfo=timezone.utc)


def record(snapshot_id, hour, available):
    captured = BASE + timedelta(hours=hour)
    kickoff = BASE + timedelta(days=3)
    return {
        "snapshot_id": snapshot_id,
        "ticket_event_id": 1,
        "external_event_id": "10069",
        "captured_at": captured.isoformat(),
        "_captured_at": captured,
        "provider": "roboticket",
        "home_team": "Lech Poznań",
        "away_team": "Jagiellonia Białystok",
        "competition": "Ekstraklasa",
        "match_date": kickoff.date().isoformat(),
        "kickoff_at": kickoff.isoformat(),
        "hours_to_kickoff": (kickoff - captured).total_seconds() / 3600.0,
        "days_to_match": (kickoff - captured).total_seconds() / 86400.0,
        "available_total": available,
        "sector_count": 40,
    }


def test_contiguous_history_is_ready():
    records = [record(hour + 1, hour, 20000 - 100 * hour) for hour in range(25)]
    latest = live.calculate_features(records)[-1]

    assert latest["signal_readiness"] == "24h_ready"
    assert latest["data_gap_detected"] is False
    assert latest["window_6h_quality_status"] == "ready"
    assert latest["window_24h_quality_status"] == "ready"
    assert latest["inventory_velocity_6h"] == 100.0
    assert latest["inventory_velocity_24h"] == 100.0
    assert latest["max_snapshot_gap_hours"] == 1.0


def test_gap_invalidates_24h_but_keeps_clean_6h_window():
    hours = list(range(0, 6)) + list(range(12, 25))
    records = [record(index + 1, hour, 20000 - 100 * hour) for index, hour in enumerate(hours)]
    latest = live.calculate_features(records)[-1]

    assert latest["data_gap_detected"] is True
    assert latest["max_snapshot_gap_hours"] == 7.0
    assert latest["window_24h_quality_status"] == "gap_detected"
    assert latest["inventory_velocity_24h"] is None
    assert latest["net_removed_24h"] is None
    assert latest["window_6h_quality_status"] == "ready"
    assert latest["inventory_velocity_6h"] == 100.0
    assert latest["signal_readiness"] == "6h_ready"


def test_oversized_window_is_not_interpolated():
    records = [
        record(1, 0, 20000),
        record(2, 1, 19900),
        record(3, 10, 19000),
    ]
    latest = live.calculate_features(records)[-1]

    assert latest["window_6h_actual_hours"] == 9.0
    assert latest["window_6h_quality_status"] == "gap_detected"
    assert latest["inventory_velocity_6h"] is None
    assert latest["net_removed_6h"] is None
    assert latest["signal_readiness"] == "data_gap"


def test_previous_velocity_is_invalid_after_large_gap():
    records = [record(1, 0, 20000), record(2, 4, 19600)]
    latest = live.calculate_features(records)[-1]

    assert latest["previous_gap_detected"] is True
    assert latest["elapsed_hours_since_previous"] == 4.0
    assert latest["net_removed_since_previous"] == 400
    assert latest["inventory_velocity_since_previous"] is None
    assert latest["signal_readiness"] == "data_gap"



def test_bounded_inventory_selection_keeps_first_and_recent_window():
    records = [record(hour + 1, hour, 20000 - 50 * hour) for hour in range(73)]
    selected = live.select_inventory_snapshot_ids(records, lookback_hours=30)

    assert 1 in selected
    assert 43 in selected
    assert 42 not in selected
    assert 73 in selected
    assert len(selected) == 32


def test_bounded_history_preserves_latest_live_features():
    records = [record(hour + 1, hour, 20000 - 50 * hour) for hour in range(73)]
    full_latest = live.calculate_features(records)[-1]

    selected_ids = set(
        live.select_inventory_snapshot_ids(records, lookback_hours=30)
    )
    bounded_records = [
        row for row in records if row["snapshot_id"] in selected_ids
    ]
    bounded_latest = live.calculate_features(bounded_records)[-1]

    keys = [
        "history_hours",
        "available_total",
        "first_available_total",
        "available_index",
        "net_removed_since_first",
        "inventory_velocity_since_previous",
        "inventory_velocity_6h",
        "inventory_velocity_24h",
        "inventory_acceleration_6h_vs_24h",
        "window_6h_quality_status",
        "window_24h_quality_status",
        "data_gap_detected",
        "signal_readiness",
    ]
    for key in keys:
        assert bounded_latest[key] == full_latest[key]

def main():
    test_contiguous_history_is_ready()
    test_gap_invalidates_24h_but_keeps_clean_6h_window()
    test_oversized_window_is_not_interpolated()
    test_previous_velocity_is_invalid_after_large_gap()
    test_bounded_inventory_selection_keeps_first_and_recent_window()
    test_bounded_history_preserves_latest_live_features()
    print("SUCCESS")


if __name__ == "__main__":
    main()
