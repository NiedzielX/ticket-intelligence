#!/usr/bin/env python3

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
EVENT_ID = int(os.environ["EVENT_ID"])
ARTIFACT_DIR = Path(os.getenv("OUTPUT_DIR", "beyond_forecast_artifacts_v01"))
ARTIFACT_PATH = ARTIFACT_DIR / f"event_{EVENT_ID}_beyond_forecast_v01.json"
RESULT_PATH = Path(f"forecast_persist_result_{EVENT_ID}.json")


def nullable_int(value):
    if value is None:
        return None
    return int(round(float(value)))


def nullable_float(value):
    if value is None:
        return None
    return float(value)


def build_row(payload):
    event = payload["event"]
    live = payload["live"]
    historical = payload["historical"]
    correction = payload["correction"]
    forecast = payload["forecast"]

    snapshot_id = live.get("snapshot_id")
    captured_at = live.get("captured_at")

    if snapshot_id is None or not captured_at:
        raise RuntimeError(
            "Beyond Forecast payload has no source snapshot; "
            "forecast observation cannot be persisted."
        )

    return {
        "ticket_event_id": int(event["id"]),
        "source_snapshot_id": int(snapshot_id),
        "forecast_generated_at": payload["generated_at"],
        "source_snapshot_captured_at": captured_at,
        "hours_to_kickoff": nullable_float(live.get("hours_to_kickoff")),
        "days_to_match": nullable_float(live.get("days_to_match")),
        "horizon": payload.get("horizon"),
        "model_version": payload["script_version"],
        "historical_model": historical.get("model"),
        "historical_p10": nullable_int(historical.get("p10")),
        "historical_p50": nullable_int(historical.get("p50")),
        "historical_p90": nullable_int(historical.get("p90")),
        "live_adjustment": nullable_int(forecast.get("live_adjustment")) or 0,
        "final_p10": nullable_int(forecast.get("p10")),
        "final_p50": nullable_int(forecast.get("p50")),
        "final_p90": nullable_int(forecast.get("p90")),
        "forecast_status": forecast["status"],
        "correction_status": correction["status"],
        "signal_readiness": live.get("signal_readiness"),
        "live_available_total": nullable_int(live.get("available_total")),
        "live_first_available_total": nullable_int(live.get("first_available_total")),
        "live_available_index": nullable_float(live.get("available_index")),
        "live_net_removed_since_first": nullable_int(live.get("net_removed_since_first")),
        "live_net_removed_since_previous": nullable_int(live.get("net_removed_since_previous")),
        "live_velocity_since_previous": nullable_float(live.get("inventory_velocity_since_previous")),
        "live_net_removed_6h": nullable_int(live.get("net_removed_6h")),
        "live_velocity_6h": nullable_float(live.get("inventory_velocity_6h")),
        "live_net_removed_24h": nullable_int(live.get("net_removed_24h")),
        "live_velocity_24h": nullable_float(live.get("inventory_velocity_24h")),
        "live_acceleration_6h_vs_24h": nullable_float(
            live.get("inventory_acceleration_6h_vs_24h")
        ),
        "live_raw_snapshot_count": nullable_int(live.get("raw_snapshot_count")),
        "live_clean_snapshot_count": nullable_int(live.get("clean_snapshot_count")),
        "live_excluded_anomaly_count": nullable_int(live.get("excluded_anomaly_count")),
        "payload": payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def upsert(row):
    conflict = "ticket_event_id,source_snapshot_id,model_version"
    query = parse.urlencode({"on_conflict": conflict})
    url = f"{SUPABASE_URL}/rest/v1/forecast_observations?{query}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }

    req = request.Request(
        url,
        data=json.dumps(row).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw else []
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc

    if len(result) != 1:
        raise RuntimeError(
            f"Expected one persisted forecast observation, found {len(result)}."
        )

    return result[0]


def main():
    if not ARTIFACT_PATH.exists():
        raise RuntimeError(f"Missing Beyond Forecast artifact: {ARTIFACT_PATH}")

    payload = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    row = build_row(payload)
    persisted = upsert(row)

    verification = {
        "observation_id": int(persisted["id"]),
        "ticket_event_id": int(persisted["ticket_event_id"]),
        "source_snapshot_id": int(persisted["source_snapshot_id"]),
        "hours_to_kickoff": persisted.get("hours_to_kickoff"),
        "model_version": persisted.get("model_version"),
        "final_p50": persisted.get("final_p50"),
    }
    RESULT_PATH.write_text(
        json.dumps(verification, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(
        "Persisted forecast observation: "
        f"id={persisted['id']} event={persisted['ticket_event_id']} "
        f"snapshot={persisted['source_snapshot_id']} "
        f"hours_to_kickoff={persisted.get('hours_to_kickoff')}"
    )
    print("SUCCESS")


if __name__ == "__main__":
    main()
