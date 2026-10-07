-- Minimal migration from the restricted old Supabase project to a fresh project.
-- Run each numbered SELECT separately in the OLD project's SQL Editor and
-- download its result as CSV. Import into the NEW project in the same order.
--
-- We intentionally DO NOT export sector_inventory. Snapshot totals are
-- aggregated server-side into lightweight snapshot rows.

-- ============================================================
-- 1) ticket_events.csv
-- Completed events with outcomes + all future Lech home events.
-- ============================================================
SELECT
    e.id,
    e.provider,
    e.external_event_id,
    e.home_team,
    e.away_team,
    e.competition,
    e.match_date,
    e.kickoff_at,
    e.source_url,
    e.mapping_source,
    e.mapping_confidence,
    e.created_at,
    e.updated_at
FROM ticket_events e
WHERE e.id IN (SELECT ticket_event_id FROM ticket_event_outcomes)
   OR (
        e.home_team = 'Lech Poznań'
        AND e.kickoff_at > NOW()
   )
ORDER BY e.id;


-- ============================================================
-- 2) snapshots.csv
-- Only snapshots for the events above. available_total / sector_count are
-- calculated INSIDE Postgres, so no sector-level data leaves the old project.
-- If the old snapshots table already has aggregate columns, COALESCE preserves
-- them and computes only missing values.
-- ============================================================
WITH selected_events AS (
    SELECT e.id
    FROM ticket_events e
    WHERE e.id IN (SELECT ticket_event_id FROM ticket_event_outcomes)
       OR (
            e.home_team = 'Lech Poznań'
            AND e.kickoff_at > NOW()
       )
),
sector_totals AS (
    SELECT
        si.snapshot_id,
        SUM(COALESCE(si.available, 0))::INTEGER AS available_total,
        COUNT(*)::INTEGER AS sector_count
    FROM sector_inventory si
    JOIN snapshots s ON s.id = si.snapshot_id
    WHERE s.ticket_event_id IN (SELECT id FROM selected_events)
    GROUP BY si.snapshot_id
)
SELECT
    s.id,
    s.event_id,
    s.source,
    s.captured_at,
    s.ticket_event_id,
    s.event_match_date_at_capture,
    s.event_kickoff_at_capture,
    st.available_total,
    st.sector_count
FROM snapshots s
LEFT JOIN sector_totals st ON st.snapshot_id = s.id
WHERE s.ticket_event_id IN (SELECT id FROM selected_events)
ORDER BY s.id;


-- ============================================================
-- 3) ticket_event_outcomes.csv
-- Small table; preserves the completed-event labels used for calibration.
-- ============================================================
SELECT
    o.ticket_event_id,
    o.actual_attendance,
    o.attendance_definition,
    o.source_name,
    o.source_url,
    o.confirmed_at,
    o.notes,
    o.created_at,
    o.updated_at
FROM ticket_event_outcomes o
ORDER BY o.ticket_event_id;


-- ============================================================
-- 4) forecast_observations.csv
-- Preserve calibration/live observations but discard large historical payload
-- blobs. Current calibration reads scalar columns only; '{}' satisfies the new
-- schema while avoiding migration of unnecessary JSON.
-- ============================================================
WITH selected_events AS (
    SELECT e.id
    FROM ticket_events e
    WHERE e.id IN (SELECT ticket_event_id FROM ticket_event_outcomes)
       OR (
            e.home_team = 'Lech Poznań'
            AND e.kickoff_at > NOW()
       )
)
SELECT
    f.id,
    f.ticket_event_id,
    f.source_snapshot_id,
    f.forecast_generated_at,
    f.source_snapshot_captured_at,
    f.hours_to_kickoff,
    f.days_to_match,
    f.horizon,
    f.model_version,
    f.historical_model,
    f.historical_p10,
    f.historical_p50,
    f.historical_p90,
    f.live_adjustment,
    f.final_p10,
    f.final_p50,
    f.final_p90,
    f.forecast_status,
    f.correction_status,
    f.signal_readiness,
    f.live_available_total,
    f.live_first_available_total,
    f.live_available_index,
    f.live_net_removed_since_first,
    f.live_net_removed_since_previous,
    f.live_velocity_since_previous,
    f.live_net_removed_6h,
    f.live_velocity_6h,
    f.live_net_removed_24h,
    f.live_velocity_24h,
    f.live_acceleration_6h_vs_24h,
    f.live_raw_snapshot_count,
    f.live_clean_snapshot_count,
    f.live_excluded_anomaly_count,
    '{}'::jsonb AS payload,
    f.created_at,
    f.updated_at
FROM forecast_observations f
WHERE f.ticket_event_id IN (SELECT id FROM selected_events)
ORDER BY f.id;


-- ============================================================
-- Run AFTER all CSV imports in the NEW project.
-- Keeps future identity-generated IDs above the imported IDs.
-- ============================================================
-- SELECT setval(pg_get_serial_sequence('ticket_events','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM ticket_events;
-- SELECT setval(pg_get_serial_sequence('snapshots','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM snapshots;
-- SELECT setval(pg_get_serial_sequence('forecast_observations','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM forecast_observations;
-- SELECT setval(pg_get_serial_sequence('sector_inventory','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM sector_inventory;
