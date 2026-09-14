#!/usr/bin/env python3

import json
from datetime import datetime, timezone
from pathlib import Path


MATRIX_PATH = Path("roboticket_discovery_artifacts_v01/collector_matrix.json")
OVERRIDES_PATH = Path("roboticket_event_overrides_v01.json")

REQUIRED_FIELDS = (
    "id",
    "provider",
    "home_team",
    "away_team",
    "competition",
    "match_date",
    "kickoff_at",
    "mapping_source",
    "mapping_confidence",
    "url",
)


def parse_timestamp(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_override(event):
    missing = [field for field in REQUIRED_FIELDS if not event.get(field)]
    if missing:
        raise RuntimeError(
            f"Event override {event.get('id', '<unknown>')} is missing: "
            + ", ".join(missing)
        )

    kickoff = parse_timestamp(event["kickoff_at"])
    if kickoff is None or kickoff.tzinfo is None:
        raise RuntimeError(
            f"Event override {event['id']} must have an offset-aware kickoff_at."
        )

    return kickoff


def merge_event(discovered, override):
    if discovered is None:
        return dict(override)

    merged = dict(override)
    used_override_field = False

    for key, value in discovered.items():
        if value not in (None, ""):
            merged[key] = value
        elif override.get(key) not in (None, ""):
            used_override_field = True

    if used_override_field:
        discovered_source = discovered.get("mapping_source") or "live_discovery"
        merged["mapping_source"] = f"{discovered_source}+confirmed_event_override"
        merged["mapping_confidence"] = "confirmed"

    return merged


def main():
    if not MATRIX_PATH.exists():
        raise RuntimeError(f"Missing discovery matrix: {MATRIX_PATH}")

    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))

    by_id = {str(event["id"]): event for event in matrix}
    now = datetime.now(timezone.utc)
    applied = []
    expired = []

    for override in overrides:
        kickoff = validate_override(override)
        event_id = str(override["id"])

        if kickoff.astimezone(timezone.utc) <= now:
            expired.append(event_id)
            continue

        by_id[event_id] = merge_event(by_id.get(event_id), override)
        applied.append(event_id)

    merged_matrix = sorted(
        by_id.values(),
        key=lambda event: int(event["id"]),
    )
    MATRIX_PATH.write_text(
        json.dumps(merged_matrix, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    print(f"Discovery matrix before overrides: {len(matrix)}")
    print(f"Active confirmed overrides: {len(applied)}")
    print(f"Expired overrides ignored: {len(expired)}")
    print(f"Collector matrix after overrides: {len(merged_matrix)}")
    for event_id in applied:
        event = by_id[event_id]
        print(
            f"OVERRIDE {event_id} | {event['home_team']} vs {event['away_team']} | "
            f"{event['kickoff_at']} | {event['competition']}"
        )
    print(f"MATRIX_JSON={MATRIX_PATH.read_text(encoding='utf-8')}")
    print("SUCCESS")


if __name__ == "__main__":
    main()
