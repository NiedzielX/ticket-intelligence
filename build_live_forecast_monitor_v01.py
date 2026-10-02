#!/usr/bin/env python3

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request


SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
EVENT_ID = int(os.environ["EVENT_ID"])
EVENT_PROVIDER = os.getenv("EVENT_PROVIDER", "roboticket")
MODEL_VERSION = os.getenv("FORECAST_MODEL_VERSION", "beyond-forecast-v0.3")
OUTPUT_DIR = Path(os.getenv("LIVE_MONITOR_OUTPUT_DIR", "live_forecast_monitor_artifacts_v01"))
PAGE_SIZE = 1000
RECENT_TABLE_ROWS = int(os.getenv("LIVE_MONITOR_RECENT_ROWS", "18"))
CONTROLLED_BLEND_WEIGHT = float(os.getenv("LIVE_BLEND_WEIGHT", "0.20"))\nRISK_ON_BLEND_WEIGHT = float(os.getenv("RISK_ON_BLEND_WEIGHT", "0.35"))


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


def resolve_event():
    params = parse.urlencode(
        {
            "provider": f"eq.{EVENT_PROVIDER}",
            "external_event_id": f"eq.{EVENT_ID}",
            "select": (
                "id,provider,external_event_id,home_team,away_team,competition,"
                "match_date,kickoff_at"
            ),
            "limit": "2",
        }
    )
    rows = api_get_all(f"ticket_events?{params}")
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected one canonical event for {EVENT_PROVIDER}:{EVENT_ID}; found {len(rows)}."
        )
    return rows[0]


def load_observations(ticket_event_id):
    params = parse.urlencode(
        {
            "ticket_event_id": f"eq.{ticket_event_id}",
            "model_version": f"eq.{MODEL_VERSION}",
            "select": (
                "id,ticket_event_id,source_snapshot_id,forecast_generated_at,"
                "source_snapshot_captured_at,hours_to_kickoff,horizon,model_version,"
                "historical_p50,live_adjustment,final_p50,forecast_status,"
                "correction_status,signal_readiness,live_available_total,"
                "live_available_index,live_velocity_6h,live_velocity_24h,"
                "live_acceleration_6h_vs_24h"
            ),
            "order": "forecast_generated_at.asc",
        }
    )
    return api_get_all(f"forecast_observations?{params}")


def load_latest_payload(ticket_event_id):
    params = parse.urlencode(
        {
            "ticket_event_id": f"eq.{ticket_event_id}",
            "model_version": f"eq.{MODEL_VERSION}",
            "select": "id,payload",
            "order": "forecast_generated_at.desc",
            "limit": "1",
        }
    )
    rows = api_get_all(f"forecast_observations?{params}")
    return rows[0] if rows else None


def attach_latest_payload(observations, latest_payload_row):
    if not observations or not latest_payload_row:
        return observations
    if int(observations[-1]["id"]) != int(latest_payload_row["id"]):
        raise RuntimeError(
            "Latest monitor observation does not match latest payload observation."
        )
    observations[-1] = dict(observations[-1])
    observations[-1]["payload"] = latest_payload_row.get("payload") or {}
    return observations


def number(value, digits=None):
    if value is None:
        return None
    value = float(value)
    if digits is None:
        return int(round(value))
    return round(value, digits)


def selected_candidate_details(correction):
    selected_name = correction.get("selected_candidate")
    for candidate in correction.get("candidates") or []:
        if candidate.get("name") == selected_name:
            return candidate
    return None


def normalize_observation(row):
    payload = row.get("payload") or {}
    correction = payload.get("correction") or {}
    live = payload.get("live") or {}
    selected = selected_candidate_details(correction)
    selected_name = correction.get("selected_candidate")
    correction_status = row.get("correction_status")
    historical_p50 = number(row.get("historical_p50"))
    production_adjustment = number(row.get("live_adjustment")) or 0

    candidate_adjustment = number(correction.get("candidate_adjustment"))
    candidate_p50 = number(correction.get("candidate_p50"))
    blend_weight = number(correction.get("blend_weight"), 4)

    reconstruction_blend = None
    if correction_status == "controlled_live_blend_v01":
        reconstruction_blend = CONTROLLED_BLEND_WEIGHT
    elif correction_status == "controlled_live_blend_risk_on_v01":
        reconstruction_blend = RISK_ON_BLEND_WEIGHT

    if (
        candidate_p50 is None
        and reconstruction_blend
        and historical_p50 is not None
    ):
        candidate_adjustment = int(
            round(float(production_adjustment) / reconstruction_blend)
        )
        candidate_p50 = historical_p50 + candidate_adjustment
        blend_weight = reconstruction_blend
        selected_name = selected_name or "controlled_live_candidate"

    guardrails = None
    if correction_status in {
        "controlled_live_blend_v01",
        "controlled_live_blend_risk_on_v01",
    }:
        guardrails = True
    elif selected_name:
        guardrails = bool((selected or {}).get("runtime_guardrails_pass"))
    elif correction_status in {
        "candidate_guardrails_not_met",
        "insufficient_completed_events",
    }:
        guardrails = False

    return {
        "observation_id": int(row["id"]),
        "source_snapshot_id": int(row["source_snapshot_id"]),
        "forecast_generated_at": row.get("forecast_generated_at"),
        "source_snapshot_captured_at": row.get("source_snapshot_captured_at"),
        "hours_to_kickoff": number(row.get("hours_to_kickoff"), 4),
        "horizon": row.get("horizon"),
        "historical_p50": historical_p50,
        "candidate_name": selected_name,
        "candidate_p50": candidate_p50,
        "candidate_adjustment": candidate_adjustment,
        "production_p50": number(row.get("final_p50")),
        "production_adjustment": production_adjustment,
        "blend_weight": blend_weight,
        "training_event_count": correction.get("training_event_count"),
        "training_event_ids": correction.get("training_event_ids") or [],
        "guardrails_pass": guardrails,
        "correction_status": row.get("correction_status"),
        "forecast_status": row.get("forecast_status"),
        "signal_readiness": row.get("signal_readiness"),
        "data_gap_detected": live.get("data_gap_detected"),
        "available_total": number(row.get("live_available_total")),
        "available_index": number(row.get("live_available_index"), 6),
        "velocity_6h": number(row.get("live_velocity_6h"), 4),
        "velocity_24h": number(row.get("live_velocity_24h"), 4),
        "acceleration_6h_vs_24h": number(
            row.get("live_acceleration_6h_vs_24h"), 4
        ),
        "candidate_training_historical_mae": number(
            (selected or {}).get("training_historical_mae"), 2
        ),
        "candidate_training_mae": number(
            (selected or {}).get("training_candidate_mae"), 2
        ),
        "candidate_training_mae_improvement": number(
            (selected or {}).get("training_mae_improvement"), 2
        ),
        "candidate_improved_event_count": (selected or {}).get(
            "training_improved_event_count"
        ),
        "candidate_worsened_event_count": (selected or {}).get(
            "training_worsened_event_count"
        ),
    }


def delta(current, previous, key, digits=None):
    if current is None or previous is None:
        return None
    if current.get(key) is None or previous.get(key) is None:
        return None
    value = float(current[key]) - float(previous[key])
    if digits is None:
        return int(round(value))
    return round(value, digits)


def build_monitor(event, raw_observations):
    observations = [normalize_observation(row) for row in raw_observations]
    latest = observations[-1] if observations else None
    first = observations[0] if observations else None
    previous = observations[-2] if len(observations) >= 2 else None

    summary = {
        "version": "live-forecast-monitor-v0.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "event": event,
        "observation_count": len(observations),
        "latest": latest,
        "trajectory": observations,
    }
    if latest:
        summary["changes"] = {
            "production_p50_since_previous": delta(
                latest, previous, "production_p50"
            ),
            "candidate_p50_since_previous": delta(
                latest, previous, "candidate_p50"
            ),
            "available_index_since_previous": delta(
                latest, previous, "available_index", 6
            ),
            "production_p50_since_first_v02": delta(
                latest, first, "production_p50"
            ),
            "candidate_p50_since_first_v02": delta(
                latest, first, "candidate_p50"
            ),
            "available_index_since_first_v02": delta(
                latest, first, "available_index", 6
            ),
        }
    else:
        summary["changes"] = {}
    return summary


def fmt_int(value):
    if value is None:
        return "—"
    return f"{int(round(float(value))):,}".replace(",", " ")


def fmt_float(value, digits=3):
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def fmt_delta(value):
    if value is None:
        return "—"
    value = int(round(float(value)))
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,}".replace(",", " ")


def fmt_guardrail(value):
    if value is True:
        return "PASS"
    if value is False:
        return "FAIL / FALLBACK"
    return "N/A"


def build_markdown(monitor):
    event = monitor["event"]
    latest = monitor.get("latest")
    changes = monitor.get("changes") or {}
    lines = [
        "# Beyond — Live Forecast Monitor",
        "",
        f"Model: **{monitor['model_version']}**  ",
        f"Event: **{event.get('home_team')} vs {event.get('away_team')}**  ",
        f"Competition: **{event.get('competition') or 'unknown'}**  ",
        f"Kickoff: **{event.get('kickoff_at') or event.get('match_date') or 'unknown'}**  ",
        f"Observations in monitor: **{monitor['observation_count']}**",
        "",
    ]
    if latest is None:
        lines.extend(
            [
                "No observations for this model version are available yet.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "## Current forecast",
            "",
            "| Metric | Current | Δ previous |",
            "|---|---:|---:|",
            f"| Historical P50 | {fmt_int(latest.get('historical_p50'))} | — |",
            f"| Live Candidate P50 | {fmt_int(latest.get('candidate_p50'))} | {fmt_delta(changes.get('candidate_p50_since_previous'))} |",
            f"| Production P50 | **{fmt_int(latest.get('production_p50'))}** | {fmt_delta(changes.get('production_p50_since_previous'))} |",
            f"| Production adjustment | {fmt_delta(latest.get('production_adjustment'))} | — |",
            f"| Available index | {fmt_float(latest.get('available_index'))} | {fmt_float(changes.get('available_index_since_previous'), 4)} |",
            "",
            "## Live correction",
            "",
            f"- Selected candidate: **{latest.get('candidate_name') or 'none'}**",
            f"- Candidate guardrails: **{fmt_guardrail(latest.get('guardrails_pass'))}**",
            f"- Correction status: **{latest.get('correction_status') or 'unknown'}**",
            f"- Production blend weight: **{fmt_float(latest.get('blend_weight'), 2)}**",
            f"- Training events: **{latest.get('training_event_count') or 0}** — {latest.get('training_event_ids') or []}",
            f"- Training MAE: historical **{fmt_int(latest.get('candidate_training_historical_mae'))}** → candidate **{fmt_int(latest.get('candidate_training_mae'))}**",
            f"- Training MAE improvement: **{fmt_int(latest.get('candidate_training_mae_improvement'))}**",
            f"- Prior events improved / worsened: **{latest.get('candidate_improved_event_count') or 0} / {latest.get('candidate_worsened_event_count') or 0}**",
            "",
            "## Live signal",
            "",
            f"- Hours to kickoff: **{fmt_float(latest.get('hours_to_kickoff'), 1)}**",
            f"- Readiness: **{latest.get('signal_readiness') or 'unknown'}**",
            f"- Data gap: **{latest.get('data_gap_detected')}**",
            f"- Available total: **{fmt_int(latest.get('available_total'))}**",
            f"- Available index: **{fmt_float(latest.get('available_index'))}**",
            f"- Velocity 6h: **{fmt_float(latest.get('velocity_6h'), 1)}**",
            f"- Velocity 24h: **{fmt_float(latest.get('velocity_24h'), 1)}**",
            f"- Acceleration 6h vs 24h: **{fmt_float(latest.get('acceleration_6h_vs_24h'), 1)}**",
            "",
            "## Since first v0.3 observation",
            "",
            f"- Candidate P50: **{fmt_delta(changes.get('candidate_p50_since_first_v02'))}**",
            f"- Production P50: **{fmt_delta(changes.get('production_p50_since_first_v02'))}**",
            f"- Available index: **{fmt_float(changes.get('available_index_since_first_v02'), 4)}**",
            "",
            "## Recent trajectory",
            "",
            "| Generated | H to KO | Historical | Candidate | Production | Adj. | Avail. index | Candidate | Guardrails | Readiness |",
            "|---|---:|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )

    recent = monitor["trajectory"][-RECENT_TABLE_ROWS:]
    for row in recent:
        generated = (row.get("forecast_generated_at") or "").replace("T", " ")[:16]
        lines.append(
            "| {generated} | {hours} | {historical} | {candidate_p50} | {production} | {adjustment} | {available_index} | {candidate_name} | {guardrails} | {readiness} |".format(
                generated=generated or "—",
                hours=fmt_float(row.get("hours_to_kickoff"), 1),
                historical=fmt_int(row.get("historical_p50")),
                candidate_p50=fmt_int(row.get("candidate_p50")),
                production=fmt_int(row.get("production_p50")),
                adjustment=fmt_delta(row.get("production_adjustment")),
                available_index=fmt_float(row.get("available_index")),
                candidate_name=row.get("candidate_name") or "—",
                guardrails=fmt_guardrail(row.get("guardrails_pass")),
                readiness=row.get("signal_readiness") or "—",
            )
        )

    lines.extend(
        [
            "",
            "Full v0.3 trajectory is available in the companion CSV artifact.",
            "",
        ]
    )
    return "\n".join(lines)


CSV_FIELDS = [
    "observation_id",
    "source_snapshot_id",
    "forecast_generated_at",
    "source_snapshot_captured_at",
    "hours_to_kickoff",
    "horizon",
    "historical_p50",
    "candidate_name",
    "candidate_p50",
    "candidate_adjustment",
    "production_p50",
    "production_adjustment",
    "blend_weight",
    "training_event_count",
    "training_event_ids",
    "guardrails_pass",
    "correction_status",
    "forecast_status",
    "signal_readiness",
    "data_gap_detected",
    "available_total",
    "available_index",
    "velocity_6h",
    "velocity_24h",
    "acceleration_6h_vs_24h",
    "candidate_training_historical_mae",
    "candidate_training_mae",
    "candidate_training_mae_improvement",
    "candidate_improved_event_count",
    "candidate_worsened_event_count",
]


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            output = {key: row.get(key) for key in CSV_FIELDS}
            output["training_event_ids"] = ",".join(
                str(value) for value in row.get("training_event_ids") or []
            )
            writer.writerow(output)


def write_monitor(monitor):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    event_external_id = monitor["event"]["external_event_id"]
    stem = f"event_{event_external_id}_live_forecast_monitor_v01"
    json_path = OUTPUT_DIR / f"{stem}.json"
    markdown_path = OUTPUT_DIR / f"{stem}.md"
    csv_path = OUTPUT_DIR / f"{stem}_trajectory.csv"

    json_path.write_text(
        json.dumps(monitor, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(build_markdown(monitor), encoding="utf-8")
    write_csv(csv_path, monitor["trajectory"])
    return json_path, markdown_path, csv_path


def main():
    event = resolve_event()
    observations = load_observations(int(event["id"]))
    observations = attach_latest_payload(
        observations,
        load_latest_payload(int(event["id"])),
    )
    monitor = build_monitor(event, observations)
    json_path, markdown_path, csv_path = write_monitor(monitor)

    latest = monitor.get("latest")
    print(
        f"Live monitor: {event['home_team']} vs {event['away_team']} | "
        f"model={MODEL_VERSION} observations={monitor['observation_count']}"
    )
    if latest:
        print(
            f"Historical={latest.get('historical_p50')} "
            f"Candidate={latest.get('candidate_p50')} "
            f"Production={latest.get('production_p50')} "
            f"Adjustment={latest.get('production_adjustment')} "
            f"CandidateType={latest.get('candidate_name')} "
            f"Guardrails={fmt_guardrail(latest.get('guardrails_pass'))}"
        )
    print(f"Markdown: {markdown_path}")
    print(f"JSON: {json_path}")
    print(f"CSV: {csv_path}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
