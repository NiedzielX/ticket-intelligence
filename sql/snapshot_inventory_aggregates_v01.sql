-- Snapshot-level inventory aggregates for low-egress live intelligence.
--
-- Goal:
--   Keep detailed sector_inventory for audit/debugging, but allow live feature
--   generation to read one aggregate row per snapshot from snapshots itself.
--
-- Safe to run repeatedly.

ALTER TABLE snapshots
    ADD COLUMN IF NOT EXISTS available_total INTEGER,
    ADD COLUMN IF NOT EXISTS sector_count INTEGER;

-- Backfill historical snapshots once from the detailed inventory table.
WITH inventory_aggregates AS (
    SELECT
        snapshot_id,
        SUM(COALESCE(available, 0))::INTEGER AS available_total,
        COUNT(*)::INTEGER AS sector_count
    FROM sector_inventory
    GROUP BY snapshot_id
)
UPDATE snapshots AS s
SET
    available_total = COALESCE(s.available_total, a.available_total),
    sector_count = COALESCE(s.sector_count, a.sector_count)
FROM inventory_aggregates AS a
WHERE s.id = a.snapshot_id
  AND (
      s.available_total IS NULL
      OR s.sector_count IS NULL
  );

ALTER TABLE snapshots
    DROP CONSTRAINT IF EXISTS snapshots_available_total_nonnegative;

ALTER TABLE snapshots
    ADD CONSTRAINT snapshots_available_total_nonnegative
    CHECK (available_total IS NULL OR available_total >= 0);

ALTER TABLE snapshots
    DROP CONSTRAINT IF EXISTS snapshots_sector_count_nonnegative;

ALTER TABLE snapshots
    ADD CONSTRAINT snapshots_sector_count_nonnegative
    CHECK (sector_count IS NULL OR sector_count >= 0);

-- Existing ticket-event/captured_at index remains the main lookup path.
-- No additional aggregate-specific index is needed because these columns are
-- selected from already-filtered snapshot rows.
