#!/usr/bin/env python3

import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
EVENT_ID = int(os.environ["EVENT_ID"])
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "live_ticket_artifacts_v01"))
PAGE_SIZE = 1000
SNAPSHOT_QUERY_CHUNK = 100
TRANSIENT_JUMP_THRESHOLD = int(os.getenv("TRANSIENT_JUMP_THRESHOLD", "500"))
TRANSIENT_RETURN_TOLERANCE = int(os.getenv("TRANSIENT_RETURN_TOLERANCE", "100"))


def api_get_all(path):
    rows = []
    offset = 0

    while True:
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Accept": "application/json",
            "Range": f"{offset}-{offset + PAGE_SIZE - 1}",
        }
        req = request.Request(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            method="GET",
        )

        try:
            with request.urlopen(req, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8") or "[]")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase {exc.code}: {detail}") from exc

        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def resolve_ticket_event():
    query = parse.urlencode(
        {
            "provider": f"eq.{EVENT_PROVIDER}",
            "external_event_id": f"eq.{EVENT_ID}",
            "select": "id,provider,external_event_id,home_team,away_team,competition,match_date,kickoff_at",
        }
    )
    rows = api_get_all(f"ticket_events?{query}")

    if len(rows) != 1:
        raise RuntimeError(
            "Expected exactly one ticket event for "
            f"{EVENT_PROVIDER}:{EVENT_ID}; found {len(rows)}."
        )

    return rows[0]


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_snapshot_context(ticket_event_id):
    event_query = parse.urlencode(
        {
            "id": f"eq.{ticket_event_id}",
            "select": "id,provider,external_event_id,home_team,away_team,competition,match_date,kickoff_at",
        }
    )
    event_rows = api_get_all(f"ticket_events?{event_query}")
    if len(event_rows) != 1:
        raise RuntimeError(
            f"Expected one ticket event for id={ticket_event_id}; found {len(event_rows)}."
        )
    event = event_rows[0]

    snapshot_query = parse.urlencode(
        {
            "ticket_event_id": f"eq.{ticket_event_id}",
            "select": (
                "id,captured_at,ticket_event_id,"
                "event_match_date_at_capture,event_kickoff_at_capture"
            ),
            "order": "captured_at.asc",
        }
    )
    snapshots = api_get_all(f"snapshots?{snapshot_query}")
    context = []

    for snapshot in snapshots:
        captured_at = parse_timestamp(snapshot.get("captured_at"))
        kickoff_value = snapshot.get("event_kickoff_at_capture") or event.get("kickoff_at")
        match_date = snapshot.get("event_match_date_at_capture") or event.get("match_date")
        kickoff_at = parse_timestamp(kickoff_value)

        if captured_at is None:
            continue

        hours_to_kickoff = None
        days_to_match = None
        if kickoff_at is not None:
            hours_to_kickoff = (kickoff_at - captured_at).total_seconds() / 3600.0
            days_to_match = hours_to_kickoff / 24.0

        context.append(
            {
                "snapshot_id": int(snapshot["id"]),
                "captured_at": snapshot["captured_at"],
                "ticket_event_id": int(snapshot["ticket_event_id"]),
                "provider": event["provider"],
                "external_event_id": event["external_event_id"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "competition": event.get("competition"),
                "match_date": match_date,
                "kickoff_at": kickoff_value,
                "hours_to_kickoff": hours_to_kickoff,
                "days_to_match": days_to_match,
            }
        )

    return context


def load_inventory(snapshot_ids):
    if not snapshot_ids:
        return []

    rows = []
    unique_ids = sorted(set(int(snapshot_id) for snapshot_id in snapshot_ids))

    for start in range(0, len(unique_ids), SNAPSHOT_QUERY_CHUNK):
        chunk = unique_ids[start : start + SNAPSHOT_QUERY_CHUNK]
        in_filter = f"in.({','.join(str(snapshot_id) for snapshot_id in chunk)})"
        query = parse.urlencode(
            {
                "snapshot_id": in_filter,
                "select": "snapshot_id,sector,available",
                "order": "snapshot_id.asc",
            }
        )
        rows.extend(api_get_all(f"sector_inventory?{query}"))

    return rows


def aggregate_available(inventory_rows):
    totals = {}
    sector_counts = {}

    for row in inventory_rows:
        snapshot_id = int(row["snapshot_id"])
        totals[snapshot_id] = totals.get(snapshot_id, 0) + int(row.get("available") or 0)
        sector_counts[snapshot_id] = sector_counts.get(snapshot_id, 0) + 1

    return totals, sector_counts


def round_or_none(value, digits=4):
    return None if value is None else round(value, digits)


def build_records(context_rows, totals, sector_counts):
    records = []

    for row in context_rows:
        snapshot_id = int(row["snapshot_id"])
        captured_at = parse_timestamp(row["captured_at"])

        if captured_at is None or snapshot_id not in totals:
            continue

        records.append(
            {
                **row,
                "snapshot_id": snapshot_id,
                "_captured_at": captured_at,
                "available_total": totals[snapshot_id],
                "sector_count": sector_counts.get(snapshot_id, 0),
            }
        )

    records.sort(key=lambda row: row["_captured_at"])

    if not records:
        raise RuntimeError("No linked snapshots with sector inventory were found.")

    return records


def detect_transient_spikes(records):
    excluded_ids = set()
    anomaly_rows = []

    for index in range(1, len(records) - 1):
        previous = records[index - 1]
        current = records[index]
        following = records[index + 1]

        jump_in = current["available_total"] - previous["available_total"]
        jump_out = following["available_total"] - current["available_total"]
        returned_close = (
            abs(following["available_total"] - previous["available_total"])
            <= TRANSIENT_RETURN_TOLERANCE
        )
        reversed_direction = jump_in * jump_out < 0

        if (
            abs(jump_in) >= TRANSIENT_JUMP_THRESHOLD
            and abs(jump_out) >= TRANSIENT_JUMP_THRESHOLD
            and reversed_direction
            and returned_close
        ):
            excluded_ids.add(current["snapshot_id"])
            anomaly_rows.append(
                {
                    "snapshot_id": current["snapshot_id"],
                    "captured_at": current["captured_at"],
                    "available_total": current["available_total"],
                    "previous_available_total": previous["available_total"],
                    "following_available_total": following["available_total"],
                    "jump_from_previous": jump_in,
                    "jump_to_following": jump_out,
                    "reason": "single_snapshot_transient_spike",
                }
            )

    return excluded_ids, anomaly_rows


def window_baseline(records, current_index, window_hours):
    current_time = records[current_index]["_captured_at"]
    cutoff = current_time - timedelta(hours=window_hours)

    for candidate_index in range(current_index - 1, -1, -1):
        if records[candidate_index]["_captured_at"] <= cutoff:
            return records[candidate_index]

    return None


def calculate_features(records):
    first_available = records[0]["available_total"]
    features = []

    for index, record in enumerate(records):
        previous = records[index - 1] if index > 0 else None

        if previous:
            elapsed_hours = (
                record["_captured_at"] - previous["_captured_at"]
            ).total_seconds() / 3600.0
            net_removed_previous = previous["available_total"] - record["available_total"]
            velocity_previous = (
                net_removed_previous / elapsed_hours if elapsed_hours > 0 else None
            )
        else:
            elapsed_hours = None
            net_removed_previous = None
            velocity_previous = None

        derived = {}

        for window_hours in (6, 24):
            baseline = window_baseline(records, index, window_hours)

            if baseline:
                actual_window = (
                    record["_captured_at"] - baseline["_captured_at"]
                ).total_seconds() / 3600.0
                net_removed = baseline["available_total"] - record["available_total"]
                velocity = net_removed / actual_window if actual_window > 0 else None
            else:
                actual_window = None
                net_removed = None
                velocity = None

            derived[f"window_{window_hours}h_actual_hours"] = round_or_none(actual_window)
            derived[f"net_removed_{window_hours}h"] = net_removed
            derived[f"inventory_velocity_{window_hours}h"] = round_or_none(velocity)

        velocity_6h = derived["inventory_velocity_6h"]
        velocity_24h = derived["inventory_velocity_24h"]
        acceleration = (
            velocity_6h - velocity_24h
            if velocity_6h is not None and velocity_24h is not None
            else None
        )

        available_index = (
            record["available_total"] / first_available if first_available > 0 else None
        )

        history_hours = (
            record["_captured_at"] - records[0]["_captured_at"]
        ).total_seconds() / 3600.0

        if history_hours >= 24:
            signal_readiness = "24h_ready"
        elif history_hours >= 6:
            signal_readiness = "6h_ready"
        elif index >= 1:
            signal_readiness = "short_history"
        else:
            signal_readiness = "first_snapshot"

        features.append(
            {
                "snapshot_id": record["snapshot_id"],
                "ticket_event_id": record["ticket_event_id"],
                "provider_event_id": record["external_event_id"],
                "captured_at": record["captured_at"],
                "home_team": record["home_team"],
                "away_team": record["away_team"],
                "competition": record["competition"],
                "match_date": record["match_date"],
                "kickoff_at": record["kickoff_at"],
                "days_to_match": record["days_to_match"],
                "hours_to_kickoff": record["hours_to_kickoff"],
                "signal_readiness": signal_readiness,
                "history_hours": round_or_none(history_hours),
                "sector_count": record["sector_count"],
                "available_total": record["available_total"],
                "first_available_total": first_available,
                "available_index": round_or_none(available_index, 6),
                "net_removed_since_first": first_available - record["available_total"],
                "elapsed_hours_since_previous": round_or_none(elapsed_hours),
                "net_removed_since_previous": net_removed_previous,
                "inventory_velocity_since_previous": round_or_none(velocity_previous),
                **derived,
                "inventory_acceleration_6h_vs_24h": round_or_none(acceleration),
            }
        )

    return features


def write_csv(path, rows):
    if not rows:
        return

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(ticket_event, raw_snapshot_count, features, anomaly_rows):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = OUTPUT_DIR / f"event_{EVENT_ID}_live_ticket_features_v01.csv"
    anomaly_path = OUTPUT_DIR / f"event_{EVENT_ID}_inventory_anomalies_v01.csv"
    latest_path = OUTPUT_DIR / f"event_{EVENT_ID}_latest_live_ticket_features_v01.json"

    write_csv(csv_path, features)
    write_csv(anomaly_path, anomaly_rows)

    payload = {
        "event": ticket_event,
        "raw_snapshot_count": raw_snapshot_count,
        "feature_snapshot_count": len(features),
        "excluded_anomaly_count": len(anomaly_rows),
        "excluded_snapshot_ids": [row["snapshot_id"] for row in anomaly_rows],
        "latest": features[-1],
        "quality_rule": {
            "transient_jump_threshold": TRANSIENT_JUMP_THRESHOLD,
            "transient_return_tolerance": TRANSIENT_RETURN_TOLERANCE,
            "description": (
                "Exclude a single snapshot when availability jumps by at least the threshold, "
                "reverses by at least the threshold in the next snapshot, and returns close to "
                "the pre-jump level."
            ),
        },
        "note": (
            "Inventory removal is a demand proxy, not a confirmed sale. "
            "Negative values are preserved when inventory reappears."
        ),
    }

    latest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return csv_path, anomaly_path, latest_path


def main():
    ticket_event = resolve_ticket_event()
    context_rows = load_snapshot_context(ticket_event["id"])
    snapshot_ids = [row["snapshot_id"] for row in context_rows]
    inventory_rows = load_inventory(snapshot_ids)
    totals, sector_counts = aggregate_available(inventory_rows)
    raw_records = build_records(context_rows, totals, sector_counts)
    excluded_ids, anomaly_rows = detect_transient_spikes(raw_records)
    clean_records = [
        record for record in raw_records if record["snapshot_id"] not in excluded_ids
    ]

    if not clean_records:
        raise RuntimeError("All linked snapshots were excluded by data-quality rules.")

    features = calculate_features(clean_records)
    csv_path, anomaly_path, latest_path = write_outputs(
        ticket_event,
        len(raw_records),
        features,
        anomaly_rows,
    )

    latest = features[-1]

    print(
        f"Event {EVENT_PROVIDER}:{EVENT_ID} — "
        f"{ticket_event['home_team']} vs {ticket_event['away_team']}"
    )
    print(f"Raw linked snapshots: {len(raw_records)}")
    print(f"Feature snapshots: {len(features)}")
    print(f"Excluded transient spikes: {len(anomaly_rows)}")
    print(f"Latest hours to kickoff: {latest['hours_to_kickoff']}")
    print(f"Latest available: {latest['available_total']}")
    print(f"Signal readiness: {latest['signal_readiness']}")
    print(f"6h inventory velocity: {latest['inventory_velocity_6h']}")
    print(f"24h inventory velocity: {latest['inventory_velocity_24h']}")
    print(f"6h vs 24h acceleration: {latest['inventory_acceleration_6h_vs_24h']}")
    print(f"CSV: {csv_path}")
    print(f"Anomalies: {anomaly_path}")
    print(f"Latest JSON: {latest_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
