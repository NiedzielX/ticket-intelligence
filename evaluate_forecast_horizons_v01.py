#!/usr/bin/env python3

import csv
import json
import os
from collections import defaultdict
from pathlib import Path
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
EVENT_ID = os.getenv("EVENT_ID")
HORIZONS = tuple(
    int(value.strip())
    for value in os.getenv("EVALUATION_HORIZONS_HOURS", "336,168,72,48,24").split(",")
    if value.strip()
)
MAX_EARLY_GAP_HOURS = float(os.getenv("HORIZON_MAX_EARLY_GAP_HOURS", "2.5"))
MIN_CALIBRATION_EVENTS = int(os.getenv("MIN_CALIBRATION_EVENTS", "7"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "forecast_evaluation_artifacts_v01"))
PAGE_SIZE = 1000


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


def resolve_event_filter():
    if not EVENT_ID:
        return None
    query = parse.urlencode(
        {
            "provider": f"eq.{EVENT_PROVIDER}",
            "external_event_id": f"eq.{EVENT_ID}",
            "select": "id",
        }
    )
    rows = api_get_all(f"ticket_events?{query}")
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected exactly one event for {EVENT_PROVIDER}:{EVENT_ID}; found {len(rows)}."
        )
    return int(rows[0]["id"])


def load_outcomes(ticket_event_id=None):
    params = {
        "select": (
            "ticket_event_id,actual_attendance,attendance_definition,"
            "source_name,source_url,confirmed_at"
        ),
        "order": "ticket_event_id.asc",
    }
    if ticket_event_id is not None:
        params["ticket_event_id"] = f"eq.{ticket_event_id}"
    return api_get_all(f"ticket_event_outcomes?{parse.urlencode(params)}")


def load_events(ticket_event_ids):
    if not ticket_event_ids:
        return {}
    ids = ",".join(str(value) for value in sorted(set(ticket_event_ids)))
    params = parse.urlencode(
        {
            "id": f"in.({ids})",
            "select": (
                "id,provider,external_event_id,home_team,away_team,competition,"
                "match_date,kickoff_at"
            ),
        }
    )
    return {int(row["id"]): row for row in api_get_all(f"ticket_events?{params}")}


def load_observations(ticket_event_ids):
    if not ticket_event_ids:
        return []
    ids = ",".join(str(value) for value in sorted(set(ticket_event_ids)))
    params = parse.urlencode(
        {
            "ticket_event_id": f"in.({ids})",
            "select": (
                "id,ticket_event_id,source_snapshot_id,forecast_generated_at,"
                "source_snapshot_captured_at,hours_to_kickoff,horizon,model_version,"
                "historical_model,historical_p10,historical_p50,historical_p90,"
                "final_p10,final_p50,final_p90,forecast_status,correction_status,"
                "signal_readiness,live_available_total,live_first_available_total,"
                "live_available_index,live_net_removed_since_first,"
                "live_net_removed_since_previous,live_velocity_since_previous,"
                "live_net_removed_6h,live_velocity_6h,live_net_removed_24h,"
                "live_velocity_24h,live_acceleration_6h_vs_24h"
            ),
            "order": "hours_to_kickoff.desc",
        }
    )
    return api_get_all(f"forecast_observations?{params}")


def select_asof_horizon(observations, target_hours, max_early_gap_hours):
    candidates = []
    for row in observations:
        hours = row.get("hours_to_kickoff")
        if hours is None:
            continue
        hours = float(hours)
        # Strict as-of rule: a T-N evaluation may only use an observation that
        # already existed before the T-N boundary, never one collected after it.
        if hours >= target_hours:
            candidates.append((hours - target_hours, row))
    if not candidates:
        return None
    gap, selected = min(candidates, key=lambda item: item[0])
    if gap > max_early_gap_hours:
        return None
    return selected


def make_evaluation_row(event, outcome, observation, target_hours):
    actual = int(outcome["actual_attendance"])
    historical_p50 = observation.get("historical_p50")
    final_p50 = observation.get("final_p50")
    historical_error = None
    historical_abs_error = None
    final_error = None
    final_abs_error = None

    if historical_p50 is not None:
        historical_error = actual - int(historical_p50)
        historical_abs_error = abs(historical_error)
    if final_p50 is not None:
        final_error = actual - int(final_p50)
        final_abs_error = abs(final_error)

    return {
        "ticket_event_id": int(event["id"]),
        "provider_event_id": event["external_event_id"],
        "competition": event.get("competition"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "kickoff_at": event.get("kickoff_at"),
        "target_horizon_hours": target_hours,
        "selected_hours_to_kickoff": round(float(observation["hours_to_kickoff"]), 4),
        "horizon_early_gap_hours": round(
            float(observation["hours_to_kickoff"]) - target_hours, 4
        ),
        "observation_id": int(observation["id"]),
        "source_snapshot_id": int(observation["source_snapshot_id"]),
        "actual_attendance": actual,
        "attendance_definition": outcome["attendance_definition"],
        "outcome_source_name": outcome["source_name"],
        "historical_model": observation.get("historical_model"),
        "historical_p50": historical_p50,
        "historical_error": historical_error,
        "historical_abs_error": historical_abs_error,
        "final_p50": final_p50,
        "final_error": final_error,
        "final_abs_error": final_abs_error,
        "forecast_status": observation.get("forecast_status"),
        "correction_status": observation.get("correction_status"),
        "signal_readiness": observation.get("signal_readiness"),
        "live_available_total": observation.get("live_available_total"),
        "live_first_available_total": observation.get("live_first_available_total"),
        "live_available_index": observation.get("live_available_index"),
        "live_net_removed_since_first": observation.get("live_net_removed_since_first"),
        "live_net_removed_since_previous": observation.get("live_net_removed_since_previous"),
        "live_velocity_since_previous": observation.get("live_velocity_since_previous"),
        "live_net_removed_6h": observation.get("live_net_removed_6h"),
        "live_velocity_6h": observation.get("live_velocity_6h"),
        "live_net_removed_24h": observation.get("live_net_removed_24h"),
        "live_velocity_24h": observation.get("live_velocity_24h"),
        "live_acceleration_6h_vs_24h": observation.get(
            "live_acceleration_6h_vs_24h"
        ),
        # This is the supervised target for a future live correction model.
        "historical_residual_target": historical_error,
    }


def build_evaluations(events, outcomes, observations):
    by_event = defaultdict(list)
    for row in observations:
        by_event[int(row["ticket_event_id"])].append(row)

    evaluation_rows = []
    missing_horizons = []
    for outcome in outcomes:
        event_id = int(outcome["ticket_event_id"])
        event = events.get(event_id)
        if event is None:
            continue
        event_observations = by_event.get(event_id, [])
        for target in HORIZONS:
            selected = select_asof_horizon(
                event_observations,
                target,
                MAX_EARLY_GAP_HOURS,
            )
            if selected is None:
                missing_horizons.append(
                    {
                        "ticket_event_id": event_id,
                        "provider_event_id": event.get("external_event_id"),
                        "target_horizon_hours": target,
                        "reason": "no_asof_observation_within_tolerance",
                    }
                )
                continue
            evaluation_rows.append(
                make_evaluation_row(event, outcome, selected, target)
            )
    return evaluation_rows, missing_horizons


def calibration_status(evaluation_rows):
    eligible_event_ids = {
        int(row["ticket_event_id"])
        for row in evaluation_rows
        if row.get("competition") == "Ekstraklasa"
        and row.get("historical_p50") is not None
        and row.get("historical_residual_target") is not None
    }
    count = len(eligible_event_ids)
    return {
        "eligible_completed_league_events": count,
        "minimum_required_events": MIN_CALIBRATION_EVENTS,
        "ready_for_candidate_fit": count >= MIN_CALIBRATION_EVENTS,
        "rule": (
            "Do not fit or activate a live correction model until the minimum number "
            "of distinct completed league events is available. Validation must split by event."
        ),
    }


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    event_filter = resolve_event_filter()
    outcomes = load_outcomes(event_filter)
    if not outcomes:
        print("No confirmed ticket event outcomes available. Nothing to evaluate.")
        return

    event_ids = [int(row["ticket_event_id"]) for row in outcomes]
    events = load_events(event_ids)
    observations = load_observations(event_ids)
    evaluation_rows, missing_horizons = build_evaluations(
        events,
        outcomes,
        observations,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    evaluation_path = OUTPUT_DIR / "forecast_horizon_evaluation_v01.csv"
    missing_path = OUTPUT_DIR / "forecast_horizon_missing_v01.csv"
    summary_path = OUTPUT_DIR / "forecast_evaluation_summary_v01.json"
    write_csv(evaluation_path, evaluation_rows)
    write_csv(missing_path, missing_horizons)

    summary = {
        "outcome_event_count": len(outcomes),
        "evaluation_row_count": len(evaluation_rows),
        "missing_horizon_count": len(missing_horizons),
        "horizons_hours": list(HORIZONS),
        "max_early_gap_hours": MAX_EARLY_GAP_HOURS,
        "selection_rule": (
            "For T-N, select the nearest observation with hours_to_kickoff >= N. "
            "Never select an observation captured after the T-N boundary."
        ),
        "calibration": calibration_status(evaluation_rows),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Confirmed outcomes: {len(outcomes)}")
    print(f"Evaluation rows: {len(evaluation_rows)}")
    print(f"Missing horizons: {len(missing_horizons)}")
    print(
        "Calibration events: "
        f"{summary['calibration']['eligible_completed_league_events']}/"
        f"{MIN_CALIBRATION_EVENTS}"
    )
    print(f"Evaluation CSV: {evaluation_path}")
    print(f"Summary JSON: {summary_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
