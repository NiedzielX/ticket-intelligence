#!/usr/bin/env python3

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_SECRET_KEY", "test-key")
os.environ.setdefault("EVENT_ID", "1")
os.environ.setdefault("SNAPSHOT_GAP_THRESHOLD_HOURS", "2.5")
os.environ.setdefault("WINDOW_MAX_OVERSHOOT_HOURS", "2.5")
os.environ.setdefault("LIVE_FEATURE_LOOKBACK_HOURS", "30")
os.environ.setdefault("LIVE_FEATURE_SAMPLE_MINUTES", "30")

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


def test_five_minute_inventory_is_downsampled_to_thirty_minutes():
    records = [
        record(index + 1, index / 12.0, 25000 - index)
        for index in range(72 * 12 + 1)
    ]
    selected = live.select_inventory_snapshot_ids(
        records,
        lookback_hours=30,
        sample_minutes=30,
    )

    # 30 hours / 30 minutes = 60 intervals, plus latest endpoint and
    # the full-event first baseline outside the recent window.
    assert len(selected) == 62
    assert 1 in selected
    assert records[-1]["snapshot_id"] in selected


def test_snapshot_context_downloads_first_plus_recent_only():
    event = {
        "id": 1,
        "provider": "roboticket",
        "external_event_id": "10069",
        "home_team": "Lech Poznań",
        "away_team": "Jagiellonia Białystok",
        "competition": "Ekstraklasa",
        "match_date": "2026-08-04",
        "kickoff_at": "2026-08-04T18:00:00+00:00",
    }
    first = {
        "id": 1,
        "captured_at": "2026-08-01T00:00:00+00:00",
        "ticket_event_id": 1,
        "event_match_date_at_capture": "2026-08-04",
        "event_kickoff_at_capture": "2026-08-04T18:00:00+00:00",
    }
    old_middle = {
        "id": 2,
        "captured_at": "2026-08-02T00:00:00+00:00",
        "ticket_event_id": 1,
        "event_match_date_at_capture": "2026-08-04",
        "event_kickoff_at_capture": "2026-08-04T18:00:00+00:00",
    }
    recent = {
        "id": 3,
        "captured_at": "2026-08-03T12:00:00+00:00",
        "ticket_event_id": 1,
        "event_match_date_at_capture": "2026-08-04",
        "event_kickoff_at_capture": "2026-08-04T18:00:00+00:00",
    }
    latest = {
        "id": 4,
        "captured_at": "2026-08-03T18:00:00+00:00",
        "ticket_event_id": 1,
        "event_match_date_at_capture": "2026-08-04",
        "event_kickoff_at_capture": "2026-08-04T18:00:00+00:00",
    }

    calls = []
    original_api = live.api_get_all
    try:
        def fake_api(path):
            calls.append(path)
            if path.startswith("ticket_events?"):
                return [event]
            if "order=captured_at.asc" in path and "limit=1" in path:
                return [first]
            if "order=captured_at.desc" in path and "limit=1" in path:
                return [latest]
            if "captured_at=gte." in path:
                return [recent, latest]
            raise AssertionError(f"Unexpected unbounded query: {path}")

        live.api_get_all = fake_api
        context = live.load_snapshot_context(1)
    finally:
        live.api_get_all = original_api

    assert [row["snapshot_id"] for row in context] == [1, 3, 4]
    assert all(row["snapshot_id"] != old_middle["id"] for row in context)
    snapshot_calls = [path for path in calls if path.startswith("snapshots?")]
    assert len(snapshot_calls) == 3
    assert any("captured_at=gte." in path for path in snapshot_calls)


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


def test_transient_spike_detection_does_not_cross_bounded_history_gap():
    bounded_records = [
        record(1, 0, 20000),
        record(43, 42, 19000),
        record(44, 43, 20000),
    ]
    excluded, anomalies = live.detect_transient_spikes(bounded_records)

    assert excluded == set()
    assert anomalies == []

    contiguous_records = [
        record(1, 0, 20000),
        record(2, 1, 19000),
        record(3, 2, 20000),
    ]
    excluded, anomalies = live.detect_transient_spikes(contiguous_records)

    assert excluded == {2}
    assert len(anomalies) == 1


def test_feature_loader_uses_bounded_inventory_selection():
    context = [record(hour + 1, hour, 20000 - 50 * hour) for hour in range(73)]
    available_by_id = {
        row["snapshot_id"]: row["available_total"]
        for row in context
    }
    loaded_ids = []

    original_context_loader = live.load_snapshot_context
    original_inventory_loader = live.load_inventory
    try:
        live.load_snapshot_context = lambda ticket_event_id: context

        def fake_load_inventory(snapshot_ids):
            loaded_ids.extend(snapshot_ids)
            return [
                {
                    "snapshot_id": snapshot_id,
                    "sector": "A",
                    "available": available_by_id[snapshot_id],
                }
                for snapshot_id in snapshot_ids
            ]

        live.load_inventory = fake_load_inventory
        loaded_context, snapshot_ids, raw_records = live.load_feature_records(1)
    finally:
        live.load_snapshot_context = original_context_loader
        live.load_inventory = original_inventory_loader

    expected_ids = live.select_inventory_snapshot_ids(context)
    assert loaded_context == context
    assert snapshot_ids == expected_ids
    assert loaded_ids == expected_ids
    assert [row["snapshot_id"] for row in raw_records] == expected_ids

def main():
    test_contiguous_history_is_ready()
    test_gap_invalidates_24h_but_keeps_clean_6h_window()
    test_oversized_window_is_not_interpolated()
    test_previous_velocity_is_invalid_after_large_gap()
    test_bounded_inventory_selection_keeps_first_and_recent_window()
    test_five_minute_inventory_is_downsampled_to_thirty_minutes()
    test_snapshot_context_downloads_first_plus_recent_only()
    test_bounded_history_preserves_latest_live_features()
    test_transient_spike_detection_does_not_cross_bounded_history_gap()
    test_feature_loader_uses_bounded_inventory_selection()
    print("SUCCESS")


if __name__ == "__main__":
    main()
